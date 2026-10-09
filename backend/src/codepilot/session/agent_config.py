from __future__ import annotations

"""Agent 配置中心的持久化、校验和脱敏视图。"""

import hashlib
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from codepilot.config.settings import AppSettings, resolve_thinking_value
from codepilot.session.agents import AgentAssembly, AgentProfile, AgentProfileError, BUILTIN_AGENT_NAMES, parse_agent_markdown

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_SAVED_BYTES = 256 * 1024
MAX_PROMPT_CHARS = 100_000
_NAMESPACE = uuid.UUID("e579d464-8822-4ea4-bb19-d7c0c0c15b1f")


class AgentConfigError(ValueError):
    def __init__(self, message: str, *, code: str = "agent_config_invalid", status: int = 422, issues: list[AgentIssue] | None = None) -> None:
        super().__init__(message)
        self.code, self.status = code, status
        self.issues = [asdict(issue) for issue in issues or []]


@dataclass(slots=True)
class AgentIssue:
    code: str
    field: str | None
    message: str
    suggestion: str = "请在 Agent 配置中检查该项，修正后保存。"


@dataclass(slots=True)
class AgentRecord:
    profile: AgentProfile | None
    agent_id: str
    revision_id: str
    source: Literal["builtin", "custom"]
    archived: bool
    path: Path | None
    metadata: dict[str, Any] = field(default_factory=dict)
    issues: list[AgentIssue] = field(default_factory=list)
    visibility: Literal["builtin", "shared", "private"] = "private"
    owner_user_id: str | None = None

    @property
    def name(self) -> str:
        return self.profile.name if self.profile else str(self.metadata.get("name") or "invalid-agent")

    @property
    def status(self) -> str:
        if self.profile is None:
            return "invalid"
        return "legacy_warning" if self.issues else "valid"


class AgentConfigService:
    """主进程唯一的 Agent 配置写入口；所有写入保持 Markdown 兼容。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        root: Path,
        agent_profiles: dict[str, AgentProfile],
        tool_registry: Any,
        mcp_manager: Any,
        visibility: Literal["shared", "private"] = "private",
        owner_user_id: str | None = None,
    ) -> None:
        self.settings, self.root, self.agent_profiles = settings, root.resolve(), agent_profiles
        self.tool_registry, self.mcp_manager = tool_registry, mcp_manager
        self.visibility = visibility
        self.owner_user_id = owner_user_id
        self._lock = threading.RLock()
        self._records: dict[str, AgentRecord] = {}
        self._load_records()

    def list(self, status: str = "active") -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records.values())
            if status == "active": records = [r for r in records if not r.archived]
            elif status == "archived": records = [r for r in records if r.archived]
            return [self._view(record, detail=False) for record in sorted(records, key=lambda r: (r.archived, r.name))]

    def get(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(agent_id)
            if not record:
                raise AgentConfigError("Agent 不存在", code="agent_not_found", status=404)
            return self._view(record, detail=True)

    def get_active_profile_snapshot(self, agent_id: str) -> AgentProfile:
        """返回用于启动 Run 的配置快照，归档或损坏记录不能执行。"""
        with self._lock:
            record = self._records.get(agent_id)
            if record is None:
                raise AgentConfigError("Agent 不存在", code="agent_not_found", status=404)
            if record.archived:
                raise AgentConfigError("归档 Agent 不能启动新 Run", code="agent_archived", status=409)
            if record.profile is None:
                raise AgentConfigError("Agent 配置无效", code="agent_invalid", status=422, issues=record.issues)
            issues = self._dependency_issues(record.profile)
            if issues:
                raise AgentConfigError("Agent 依赖不可用，请先完成配置", code="agent_dependencies_missing", status=409, issues=issues)
            return record.profile.model_copy(deep=True)

    def get_profile_revision_snapshot(self, agent_id: str, revision_id: str) -> AgentProfile:
        """按确定路径读取不可变 revision；更新不失效，归档立即失效。"""
        with self._lock:
            record = self._records.get(agent_id)
            if record is None:
                raise AgentConfigError("Agent 不存在", code="agent_not_found", status=404)
            if record.archived:
                raise AgentConfigError("归档 Agent 不能启动新 Run", code="agent_archived", status=409)
            if record.profile is None:
                raise AgentConfigError("Agent 配置无效", code="agent_invalid", status=422)
            if not re.fullmatch(r"[0-9a-f]{64}", revision_id):
                raise AgentConfigError("Agent revision 格式无效", code="agent_revision_invalid", status=422)
            if record.source != "custom":
                if record.revision_id == revision_id:
                    return record.profile.model_copy(deep=True)
                raise AgentConfigError("Agent revision 不存在", code="agent_revision_not_found", status=404)

            unresolved_revision_dir = self._revision_dir(record.agent_id)
            revision_dir = unresolved_revision_dir.resolve()
            root = self.root.resolve()
            path = unresolved_revision_dir / f"{revision_id}.md"
            try:
                if (
                    (self.root / ".revisions").is_symlink()
                    or unresolved_revision_dir.is_symlink()
                    or not revision_dir.is_relative_to(root)
                ):
                    raise OSError
                resolved = path.resolve(strict=True)
                if path.is_symlink() or not resolved.is_relative_to(revision_dir):
                    raise OSError
                stat = resolved.stat()
                if not resolved.is_file() or stat.st_size > MAX_DOCUMENT_BYTES:
                    raise OSError
                raw = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise AgentConfigError("Agent revision 不可读取", code="agent_revision_not_found", status=404) from exc
            if self._revision(raw) != revision_id:
                raise AgentConfigError("Agent revision 内容摘要不匹配", code="agent_revision_corrupt", status=422)
            try:
                profile = parse_agent_markdown(
                    resolved,
                    max_iterations=self.settings.agent.max_loop_iterations,
                    subagent_max_iterations=self.settings.agent.subagent_max_loop_iterations,
                )
            except Exception as exc:  # noqa: BLE001
                raise AgentConfigError("Agent revision 配置无效", code="agent_revision_corrupt", status=422) from exc
            if profile.agent_id != record.agent_id or profile.revision_id != revision_id:
                raise AgentConfigError("Agent revision 身份不匹配", code="agent_revision_corrupt", status=422)
            return profile.model_copy(update={"source": "custom", "visibility": self.visibility}, deep=True)

    def get_record_snapshot(self, agent_id: str) -> dict[str, Any]:
        """返回包含归档状态的脱敏记录快照，供运行时查询和关闭使用。"""
        with self._lock:
            record = self._records.get(agent_id)
            if record is None:
                raise AgentConfigError("Agent 不存在", code="agent_not_found", status=404)
            return {
                "agent_id": record.agent_id,
                "name": record.name,
                "archived": record.archived,
                "validation_status": record.status,
                "profile": record.profile.model_copy(deep=True) if record.profile is not None else None,
            }

    def list_active_profile_snapshots(self) -> list[AgentProfile]:
        """原子复制全部可运行主 Agent，禁止 Manager 扫描共享可变字典。"""
        with self._lock:
            return [
                record.profile.model_copy(deep=True)
                for record in self._records.values()
                if not record.archived and record.profile is not None and record.profile.supports_direct
            ]

    def list_active_subagent_profile_snapshots(self) -> list[AgentProfile]:
        """兼容旧调用方，返回所有允许被委派的 Agent。"""
        return self.list_active_delegated_profile_snapshots()

    def list_active_delegated_profile_snapshots(self) -> list[AgentProfile]:
        """原子复制所有允许被 task 委派的 Agent。"""
        with self._lock:
            return [
                record.profile.model_copy(deep=True)
                for record in self._records.values()
                if not record.archived and record.profile is not None and record.profile.supports_delegated
            ]

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            profile, metadata = self._validate_payload(payload, name_required=True)
            existing = self._by_name(profile.name)
            if existing:
                candidate = self._render(profile.model_copy(update={"agent_id": existing.agent_id, "source": "custom"}), metadata)
                if existing.revision_id == self._revision(candidate):
                    return self._view(existing, detail=True)
                raise AgentConfigError("Agent 名称已存在", code="agent_name_conflict", status=409)
            profile = profile.model_copy(update={"agent_id": str(uuid.uuid4()), "source": "custom", "visibility": self.visibility})
            rendered = self._render(profile, metadata)
            revision = self._revision(rendered)
            profile = profile.model_copy(update={"revision_id": revision})
            rendered = self._render(profile, metadata)
            path = self.root / f"{profile.name}.md"
            self._persist(profile, metadata, rendered, path)
            record = AgentRecord(
                profile,
                profile.agent_id,
                revision,
                "custom",
                False,
                path,
                metadata,
                visibility= self.visibility,
                owner_user_id=self.owner_user_id,
            )
            self._records[record.agent_id] = record
            self.agent_profiles[profile.name] = profile
            return self._view(record, detail=True)

    def update(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = self._require_custom(agent_id)
            if record.profile is None:
                raise AgentConfigError("损坏的配置不能直接编辑，请复制为新 Agent", code="agent_invalid", status=422)
            if not NAME_RE.fullmatch(record.profile.name):
                raise AgentConfigError("旧 Agent 名称不符合新规则，请复制为新 Agent", code="legacy_name_readonly", status=422)
            expected = str(payload.get("expected_revision_id") or "")
            # 旧表单不认识新增字段；只合并实际提交的键，显式空数组仍能清空配置。
            current = self._view(record, detail=True)
            payload = {**current, **payload}
            profile, metadata = self._validate_payload(payload, name_required=False, fixed_name=record.profile.name, base_metadata=record.metadata)
            profile = profile.model_copy(update={"agent_id": record.agent_id, "source": "custom", "visibility": self.visibility})
            candidate = self._render(profile, metadata)
            revision = self._revision(candidate)
            if expected != record.revision_id and revision != record.revision_id:
                raise AgentConfigError("配置已被其他请求更新，请重新加载", code="revision_conflict", status=409)
            if revision == record.revision_id:
                return self._view(record, detail=True)
            profile = profile.model_copy(update={"revision_id": revision})
            rendered = self._render(profile, metadata)
            path = record.path or self._path_for(profile.name, record.archived)
            self._persist(profile, metadata, rendered, path)
            record.profile, record.revision_id, record.metadata = profile, revision, metadata
            if not record.archived: self.agent_profiles[profile.name] = profile
            return self._view(record, detail=True)

    def archive(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require_custom(agent_id)
            if record.archived: return self._view(record, detail=True)
            if record.path is None: raise AgentConfigError("配置文件不可用", code="agent_storage_error", status=409)
            target = self._path_for(record.name, True)
            self._move(record.path, target)
            record.archived, record.path = True, target
            self.agent_profiles.pop(record.name, None)
            return self._view(record, detail=True)

    def restore(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require_custom(agent_id)
            if not record.archived: return self._view(record, detail=True)
            if record.profile is None: raise AgentConfigError("损坏的配置无法恢复", code="agent_invalid", status=422)
            self._validate_profile(record.profile)
            target = self._path_for(record.name, False)
            if target.exists(): raise AgentConfigError("活动目录已有同名 Agent", code="agent_name_conflict", status=409)
            self._move(record.path, target)
            record.archived, record.path = False, target
            self.agent_profiles[record.name] = record.profile
            return self._view(record, detail=True)

    def capabilities(self) -> dict[str, Any]:
        tools = []
        for name, tool in sorted(self.tool_registry._tools.items()):
            if getattr(tool, "mcp_server_name", None): continue
            spec = tool.spec
            tools.append({"name": name, "description": spec.description[:240], "requires_approval": spec.requires_approval,
                          "side_effect": getattr(spec, "side_effect", "runtime_mutation"),
                          "assignable": getattr(spec, "assignable_to_custom_agents", True),
                          "reason": getattr(spec, "assignment_reason", None)})
        mcp = self.mcp_manager.list_server_capabilities() if hasattr(self.mcp_manager, "list_server_capabilities") else []
        if self.visibility == "private":
            mcp = [
                {**item, "status": item["status"] if item.get("assignable_to_private_agents") else "disabled"}
                for item in mcp
            ]
        providers = [{"provider": p.provider, "label": p.label, "models": p.models,
                      "model_capabilities": {m: {"thinking": v.thinking.model_dump() if v.thinking else None} for m, v in p.model_settings.items()}}
                     for p in self.settings.llm_runtime.activated_providers.values()]
        hooks = [{"hook_id": item.hook_id, "hook_type": item.hook_type, "plugin_type": item.plugin_type,
                  "parameters": {key: value.model_dump() for key, value in item.parameters.items()},
                  "available": item.enabled and item.plugin_type in {"prompt", "command", "http"}}
                 for item in self.settings.hooks.plugins]
        return {"providers": providers, "tools": tools, "mcp_servers": mcp, "hooks": hooks}

    def publish_markdown(self, source_path: Path) -> dict[str, Any]:
        """从管理员指定文件发布共享 Agent，并固化不可变 revision。"""
        with self._lock:
            source = source_path.expanduser().resolve()
            if not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_DOCUMENT_BYTES:
                raise AgentConfigError("共享 Agent 文件不可读取", code="agent_document_invalid")
            profile = parse_agent_markdown(
                source,
                max_iterations=self.settings.agent.max_loop_iterations,
                subagent_max_iterations=self.settings.agent.subagent_max_loop_iterations,
            )
            self._validate_profile(profile)
            raw = source.read_text(encoding="utf-8")
            metadata = self._metadata(raw)
            existing = self._records.get(profile.agent_id) if profile.agent_id else self._by_name(profile.name)
            if existing is not None and existing.source == "builtin":
                raise AgentConfigError("共享 Agent 不能覆盖内置 Agent", code="builtin_readonly", status=409)
            agent_id = profile.agent_id or (existing.agent_id if existing else str(uuid.uuid4()))
            if existing is not None and existing.agent_id != agent_id:
                raise AgentConfigError("Agent ID 与现有记录冲突", code="agent_id_conflict", status=409)
            profile = profile.model_copy(update={"agent_id": agent_id, "source": "custom", "visibility": self.visibility})
            candidate = self._render(profile, metadata)
            revision = self._revision(candidate)
            profile = profile.model_copy(update={"revision_id": revision})
            rendered = self._render(profile, metadata)
            target = self.root / f"{profile.name}.md"
            self._persist(profile, metadata, rendered, target)
            record = AgentRecord(
                profile=profile,
                agent_id=agent_id,
                revision_id=revision,
                source="custom",
                archived=False,
                path=target,
                metadata=metadata,
                visibility=self.visibility,
                owner_user_id=self.owner_user_id,
            )
            self._records[agent_id] = record
            self.agent_profiles[profile.name] = profile
            return self._view(record, detail=True)

    def _load_records(self) -> None:
        for name, profile in self.agent_profiles.items():
            source: Literal["builtin", "custom"] = "builtin" if name in BUILTIN_AGENT_NAMES else "custom"
            visibility: Literal["builtin", "shared", "private"] = "builtin" if source == "builtin" else self.visibility
            profile = profile.model_copy(update={"agent_id": profile.agent_id or self._derived_id(source, name), "source": source, "visibility": visibility})
            raw = self._render(profile, {})
            revision = profile.revision_id or self._revision(raw)
            profile = profile.model_copy(update={"revision_id": revision})
            self.agent_profiles[name] = profile
            path = None if source == "builtin" else self.root / f"{name}.md"
            owner = None if visibility != "private" else self.owner_user_id
            self._records[profile.agent_id] = AgentRecord(
                profile,
                profile.agent_id,
                revision,
                source,
                False,
                path,
                visibility=visibility,
                owner_user_id=owner,
            )
        if self.root.is_dir():
            known_paths = {record.path for record in self._records.values() if record.path is not None}
            for active in sorted(self.root.glob("*.md")):
                if active not in known_paths:
                    self._load_custom_file(active, archived=False)
        for archived in self._archive_dir().glob("*.md") if self._archive_dir().exists() else []:
            self._load_custom_file(archived, archived=True)

    def _load_custom_file(self, path: Path, *, archived: bool) -> None:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise AgentProfileError("配置文件不允许为符号链接或超过大小限制", code="agent_file_unreadable")
            profile = parse_agent_markdown(path, max_iterations=self.settings.agent.max_loop_iterations, subagent_max_iterations=self.settings.agent.subagent_max_loop_iterations)
            raw = path.read_text(encoding="utf-8")
            metadata = self._metadata(raw)
            agent_id = profile.agent_id or self._derived_id("custom", profile.name)
            declared_revision = profile.revision_id
            candidate = self._render(profile.model_copy(update={"agent_id": agent_id}), metadata)
            revision = declared_revision or self._revision(candidate)
            # 已声明 revision 的旧配置必须按原始文件验签；字段归一化只影响下次保存的新 revision。
            if declared_revision and declared_revision != self._revision(raw):
                raise AgentProfileError("配置版本校验失败：文件内容与保存的版本标识不一致", code="agent_revision_mismatch", field="revision_id")
            profile = profile.model_copy(update={"agent_id": agent_id, "revision_id": revision, "source": "custom", "visibility": self.visibility})
            if agent_id not in self._records:
                self._records[agent_id] = AgentRecord(
                    profile,
                    agent_id,
                    revision,
                    "custom",
                    archived,
                    path,
                    metadata,
                    visibility=self.visibility,
                    owner_user_id=self.owner_user_id,
                )
                if not archived and profile.name not in self.agent_profiles:
                    self.agent_profiles[profile.name] = profile
                # 老配置首次加载时补齐 revision 文件，后续更新仍可按固化 revision 执行。
                revision_path = self._revision_dir(agent_id) / f"{revision}.md"
                if not declared_revision and not revision_path.exists():
                    self._atomic_write(revision_path, self._render(profile, metadata))
        except Exception as exc:
            # 只输出明确分类的安全文案，不能把 YAML 片段、路径或输入值带到 API。
            issue = AgentIssue("agent_parse_failed", None, "配置文件无法解析", "请检查配置文件格式；修复后由管理员重新加载配置。")
            if isinstance(exc, AgentProfileError) and getattr(exc, "code", None):
                issue = AgentIssue(exc.code, exc.field, str(exc), "请修复配置文件并通过配置发布流程保存版本，再重新加载。")
            elif isinstance(exc, (OSError, UnicodeError)):
                issue = AgentIssue("agent_file_unreadable", None, "配置文件无法读取", "请管理员检查文件权限、编码和文件是否存在。")
            elif isinstance(exc, ValidationError):
                issue = AgentIssue("agent_field_invalid", None, "配置字段类型或取值不符合要求", "请检查技能、钩子、连接及运行参数的格式。")
            agent_id = self._derived_id("invalid", path.name)
            self._records[agent_id] = AgentRecord(
                None,
                agent_id,
                "",
                "custom",
                archived,
                path,
                {"name": path.stem},
                [issue],
                visibility=self.visibility,
                owner_user_id=self.owner_user_id,
            )

    def _validate_payload(self, payload: dict[str, Any], *, name_required: bool, fixed_name: str | None = None, base_metadata: dict[str, Any] | None = None) -> tuple[AgentProfile, dict[str, Any]]:
        name = fixed_name or str(payload.get("name") or "").strip()
        if name_required and not NAME_RE.fullmatch(name): raise AgentConfigError("Agent 名称格式非法", code="agent_name_invalid")
        if fixed_name and payload.get("name") not in {None, "", fixed_name}: raise AgentConfigError("Agent 名称创建后不可修改", code="agent_name_immutable")
        description, prompt = str(payload.get("description") or "").strip(), str(payload.get("system_prompt") or "").strip()
        if not 1 <= len(description) <= 500: raise AgentConfigError("描述长度必须为 1 到 500", code="agent_description_invalid")
        if not 1 <= len(prompt) <= MAX_PROMPT_CHARS: raise AgentConfigError("Prompt 长度不合法", code="agent_prompt_invalid")
        tool_names = [str(x) for x in payload.get("tool_names") or []]
        mcp_names = [str(x) for x in payload.get("mcp_server_names") or []]
        tools = tool_names + [f"mcp:{name}" for name in mcp_names]
        legacy_kind = payload.get("kind")
        launch_modes = payload.get("launch_modes")
        if launch_modes is None:
            launch_modes = ["delegated" if legacy_kind == "subagent" else "direct"]
        if not isinstance(launch_modes, list) or not launch_modes or any(
            item not in {"direct", "delegated"} for item in launch_modes
        ):
            raise AgentConfigError("Agent 启动模式无效", code="agent_launch_modes_invalid")
        launch_modes = list(dict.fromkeys(launch_modes))
        delegated_only = launch_modes == ["delegated"]
        iterations = payload.get("max_iterations", self.settings.agent.subagent_max_loop_iterations if delegated_only else self.settings.agent.max_loop_iterations)
        if not isinstance(iterations, int) or isinstance(iterations, bool) or not 1 <= iterations <= 10000:
            raise AgentConfigError("轮数必须为 1 到 10000 的整数", code="agent_iterations_invalid")
        can_delegate = payload.get("can_delegate")
        if can_delegate is None:
            can_delegate = payload.get("can_call_subagent", "task" in tool_names)
        if not isinstance(can_delegate, bool):
            raise AgentConfigError("委派开关必须是布尔值", code="agent_can_delegate_invalid")
        if can_delegate and "task" not in tool_names:
            raise AgentConfigError("启用委派时必须选择 task 工具", code="agent_delegate_tool_required")
        assembly_payload = {key: payload[key] for key in AgentAssembly.model_fields if key in payload}
        if "delegate_agent_ids" not in assembly_payload:
            if "subagent_ids" in payload:
                assembly_payload["delegate_agent_ids"] = payload["subagent_ids"]
            elif "allowed_subagent_ids" in payload:
                assembly_payload["delegate_agent_ids"] = payload["allowed_subagent_ids"]
        try:
            assembly = AgentAssembly.model_validate(assembly_payload)
        except ValidationError as exc:
            issues = [AgentIssue("agent_field_invalid", str(item["loc"][0]) if item["loc"] else None,
                                 f"配置字段 {item['loc'][0] if item['loc'] else '组装参数'} 的类型或取值不符合要求")
                      for item in exc.errors(include_input=False, include_context=False)]
            raise AgentConfigError("Agent 组装字段格式无效", code="agent_assembly_invalid", issues=issues) from exc
        profile = AgentProfile(name=name, description=description, system_prompt=prompt, launch_modes=launch_modes, allowed_tools=tools,
                               max_iterations=iterations, **assembly.model_dump(),
                               readonly=bool(payload.get("readonly", False)), can_delegate=can_delegate,
                               default_provider=str(payload.get("default_provider") or "").strip() or None,
                               default_model=str(payload.get("default_model") or "").strip() or None,
                               default_thinking_value=str(payload.get("default_thinking_value") or "").strip() or None)
        from codepilot.hooks.manager import validate_parameters
        for plugin in self.settings.hooks.plugins:
            if plugin.hook_id in profile.hook_parameters:
                try:
                    validate_parameters(profile.hook_parameters[plugin.hook_id], {key: value.model_dump() for key, value in plugin.parameters.items()}, allow_missing=True)
                except ValueError as exc:
                    raise AgentConfigError(str(exc), code="hook_parameters_invalid") from exc
        legacy_inheritance = not name_required and delegated_only and not profile.default_provider and not profile.default_model
        self._validate_profile(profile, allow_missing=True, allow_inherited_model=legacy_inheritance)
        metadata = dict(base_metadata or {})
        metadata.update({"format_version": 3, "name": name, "launch_modes": launch_modes, "max_iterations": iterations,
                         **assembly.model_dump(), "description": description, "tools": tools, "readonly": profile.readonly,
                         "can_delegate": profile.can_delegate, "default_provider": profile.default_provider,
                         "default_model": profile.default_model, "default_thinking_value": profile.default_thinking_value})
        for legacy_key in ("kind", "can_call_subagent", "subagent_ids", "allowed_subagent_ids"):
            metadata.pop(legacy_key, None)
        return profile, {key: value for key, value in metadata.items() if value is not None}

    def _validate_profile(self, profile: AgentProfile, *, allow_missing: bool = False, allow_inherited_model: bool = False) -> None:
        if not allow_inherited_model and (not profile.default_provider or not profile.default_model): raise AgentConfigError("必须选择已激活的 Provider 和 Model", code="llm_selection_required")
        provider = self.settings.llm_runtime.activated_providers.get(profile.default_provider)
        if not allow_missing and (not provider or profile.default_model not in provider.models): raise AgentConfigError("Provider 或 Model 不可用", code="llm_selection_invalid")
        if provider and profile.default_model in provider.models and profile.default_thinking_value:
            resolve_thinking_value(self.settings, profile.default_provider, profile.default_model, {"thinking_value": profile.default_thinking_value})
        for tool in profile.allowed_tools:
            if tool.startswith("mcp:"):
                name = tool[4:]; config = self.settings.mcp.servers.get(name)
                if not config:
                    if allow_missing: continue
                    raise AgentConfigError("MCP 服务不存在", code="mcp_unknown")
                if self.visibility == "private" and not config.assignable_to_private_agents:
                    raise AgentConfigError("MCP 服务未开放给私有 Agent", code="mcp_not_assignable", status=403)
                continue
            item = self.tool_registry.get(tool)
            if item is None:
                if allow_missing: continue
                raise AgentConfigError("Tool 不存在", code="tool_unknown")
            if not getattr(item.spec, "assignable_to_custom_agents", True):
                allowed = getattr(item.spec, "allowed_agent_names", [])
                if profile.name not in allowed: raise AgentConfigError("Tool 不能分配给当前 Agent", code="tool_restricted")

    def _dependency_issues(self, profile: AgentProfile) -> list[AgentIssue]:
        issues = []
        if profile.default_provider and profile.default_model:
            provider = self.settings.llm_runtime.activated_providers.get(profile.default_provider)
            if provider is None or profile.default_model not in provider.models:
                issues.append(AgentIssue("model_unavailable", "default_model", f"模型不可用：{profile.default_provider} / {profile.default_model}"))
        # 内置工具目录在部分管理脚本中不完整，内置定义仍由运行时装配保证。
        if profile.source != "builtin":
            for name in profile.allowed_tools:
                if name.startswith("mcp:"):
                    server = self.settings.mcp.servers.get(name[4:])
                    if server is None or not server.enabled:
                        issues.append(AgentIssue("mcp_unavailable", "mcp_server_names", f"MCP 服务不可用：{name[4:]}"))
                elif self.tool_registry.get(name) is None:
                    issues.append(AgentIssue("tool_unavailable", "tool_names", f"工具不可用：{name}"))
        plugins = {item.hook_id: item for item in self.settings.hooks.plugins}
        selected = profile.hook_ids if profile.hook_ids is not None else [item.hook_id for item in plugins.values() if item.enabled]
        for hook_id in selected:
            if hook_id.startswith(("personal:", "public:")):
                # 用户归属和不可变版本由多用户资源解析层校验。
                continue
            hook = plugins.get(hook_id)
            if hook is None or not hook.enabled or hook.plugin_type not in {"prompt", "command", "http"}:
                issues.append(AgentIssue("hook_unavailable", "hook_ids", f"Hook 不可用：{hook_id}"))
                continue
            from codepilot.hooks.manager import validate_parameters
            try:
                validate_parameters(profile.hook_parameters.get(hook_id, {}), {key: value.model_dump() for key, value in hook.parameters.items()})
                if hook.plugin_type == "command" and (not isinstance(hook.config.get("argv"), list) or not hook.config["argv"]):
                    raise ValueError
                if hook.plugin_type == "http" and not str(hook.config.get("url", "")).startswith(("https://", "http://")):
                    raise ValueError
            except ValueError:
                issues.append(AgentIssue("hook_configuration_missing", "hook_parameters", f"Hook 参数或管理员配置不完整：{hook_id}"))
        return issues

    def _persist(self, profile: AgentProfile, metadata: dict[str, Any], content: str, current: Path) -> None:
        if len(content.encode("utf-8")) > MAX_SAVED_BYTES: raise AgentConfigError("配置文件过大", code="agent_document_too_large")
        revision_path = self._revision_dir(profile.agent_id) / f"{profile.revision_id}.md"
        self._atomic_write(revision_path, content)
        self._atomic_write(current, content)

    def _render(self, profile: AgentProfile, metadata: dict[str, Any]) -> str:
        data = dict(metadata)
        for legacy_key in ("kind", "can_call_subagent", "subagent_ids", "allowed_subagent_ids"):
            data.pop(legacy_key, None)
        data.update({"name": profile.name, "agent_id": profile.agent_id or None, "revision_id": profile.revision_id or None,
                     "launch_modes": profile.launch_modes, "description": profile.description, "tools": profile.allowed_tools, "readonly": profile.readonly,
                     "can_delegate": profile.can_delegate, "default_provider": profile.default_provider,
                     "default_model": profile.default_model, "default_thinking_value": profile.default_thinking_value})
        data = {k: v for k, v in data.items() if v is not None}
        return f"---\n{yaml.safe_dump(data, allow_unicode=True, sort_keys=True).strip()}\n---\n{profile.system_prompt.strip()}\n"

    def _revision(self, content: str) -> str:
        # revision_id 本身不能参与摘要，否则写入该字段会造成自引用循环。
        data = re.sub(r"^revision_id:.*\n", "", content.replace("\r\n", "\n"), flags=re.MULTILINE)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _metadata(self, raw: str) -> dict[str, Any]:
        if not raw.startswith("---\n"): return {}
        end = raw.find("\n---\n", 4)
        value = yaml.safe_load(raw[4:end]) if end > 0 else {}
        return value if isinstance(value, dict) else {}

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() and path.read_text(encoding="utf-8") == content: return
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(content); file.flush(); os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _move(self, source: Path | None, target: Path) -> None:
        if source is None or not source.exists(): raise AgentConfigError("配置文件不存在", code="agent_storage_error", status=409)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(source, target)

    def _require_custom(self, agent_id: str) -> AgentRecord:
        record = self._records.get(agent_id)
        if not record: raise AgentConfigError("Agent 不存在", code="agent_not_found", status=404)
        if record.source == "builtin": raise AgentConfigError("内置 Agent 只读", code="builtin_readonly", status=403)
        return record

    def _by_name(self, name: str) -> AgentRecord | None:
        return next((r for r in self._records.values() if r.name == name), None)

    def _view(self, record: AgentRecord, *, detail: bool) -> dict[str, Any]:
        profile = record.profile
        result = {"agent_id": record.agent_id, "revision_id": record.revision_id, "name": record.name, "source": record.source,
                  "visibility": record.visibility, "owner_user_id": record.owner_user_id,
                  "archived": record.archived, "validation_status": record.status,
                  "validation_issues": [asdict(i) for i in record.issues]}
        if profile:
            dependencies = self._dependency_issues(profile)
            if dependencies:
                result["validation_status"] = "needs_configuration"
                result["validation_issues"].extend(asdict(issue) for issue in dependencies)
            result.update({
                "description": profile.description,
                "readonly": profile.readonly,
                "kind": profile.kind,
                "launch_modes": profile.launch_modes,
                "can_delegate": profile.can_delegate,
                # 列表页需要直接展示默认模型，但不能为此提前返回 Prompt。
                "default_provider": profile.default_provider,
                "default_model": profile.default_model,
                "default_thinking_value": profile.default_thinking_value,
            })
        if detail and profile:
            result.update({key: getattr(profile, key) for key in AgentAssembly.model_fields})
            result["max_iterations"] = profile.max_iterations
            result.update({"system_prompt": profile.system_prompt, "default_provider": profile.default_provider, "default_model": profile.default_model,
                           "default_thinking_value": profile.default_thinking_value, "tool_names": [x for x in profile.allowed_tools if not x.startswith("mcp:")],
                           "mcp_server_names": [x[4:] for x in profile.allowed_tools if x.startswith("mcp:")]})
        return result

    def _derived_id(self, source: str, value: str) -> str:
        scope = self.owner_user_id or self.visibility
        return str(uuid.uuid5(_NAMESPACE, f"{scope}:{source}:{value}"))
    def _archive_dir(self) -> Path: return self.root / ".archived"
    def _revision_dir(self, agent_id: str) -> Path: return self.root / ".revisions" / agent_id
    def _path_for(self, name: str, archived: bool) -> Path: return (self._archive_dir() if archived else self.root) / f"{name}.md"


class MultiUserAgentConfigService:
    """把内置/共享 Agent 与当前用户私有 Agent 合并为稳定 ID 目录。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        shared_root: Path,
        users_root: Path,
        builtin_profiles: dict[str, AgentProfile],
        tool_registry: Any,
        mcp_manager: Any,
        workspace_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.workspace_path = workspace_path
        self.users_root = users_root.resolve()
        self.tool_registry = tool_registry
        self.mcp_manager = mcp_manager
        self.shared = AgentConfigService(
            settings=settings,
            root=shared_root,
            agent_profiles={name: profile.model_copy(deep=True) for name, profile in builtin_profiles.items()},
            tool_registry=tool_registry,
            mcp_manager=mcp_manager,
            visibility="shared",
        )
        self._private: dict[str, AgentConfigService] = {}
        self._lock = threading.RLock()
        from codepilot.session.publications import PublicationStore
        self.publications = PublicationStore(self.users_root.parent)

    def list(self, user_id: str, status: str = "active") -> list[dict[str, Any]]:
        records = self.shared.list(status) + self._private_service(user_id).list(status)
        with self.publications.db() as db:
            used = db.execute("SELECT publication FROM settings WHERE owner=?", (user_id,)).fetchall()
        for row in used:
            item = self.publications.agent_view(user_id, row[0])
            if status == "all" or item["archived"] == (status == "archived"):
                records.append(item)
        records = [self._with_dependency_issues(user_id, item) for item in records]
        return sorted(records, key=lambda item: (bool(item.get("archived")), str(item.get("name")), str(item.get("agent_id"))))

    def get(self, user_id: str, agent_id: str) -> dict[str, Any]:
        try:
            self.publications.get(agent_id)
        except AgentConfigError as exc:
            if exc.status != 404:
                raise
        else:
            return self._with_dependency_issues(user_id, self.publications.agent_view(user_id, agent_id, detail=True))
        private = self._try_get(self._private_service(user_id), agent_id)
        return self._with_dependency_issues(user_id, private if private is not None else self.shared.get(agent_id))

    def create(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._with_dependency_issues(user_id, self._private_service(user_id).create(payload))

    def update(self, user_id: str, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._with_dependency_issues(user_id, self._private_service(user_id).update(agent_id, payload))

    def _with_dependency_issues(self, user_id: str, record: dict[str, Any]) -> dict[str, Any]:
        if record["archived"] or record["validation_status"] in {"invalid", "needs_configuration"}:
            return record
        try:
            self.get_active_profile_snapshot(user_id, record["agent_id"])
        except AgentConfigError as exc:
            record["validation_status"] = "needs_configuration"
            field = "tool_ids" if exc.code.startswith("tool") else "hook_ids" if exc.code.startswith("hook") else "skill_ids" if exc.code.startswith("skill") else "connection_ids" if exc.code.startswith("connection") else "working_directory" if exc.code.startswith("working_directory") else "subagent_ids"
            record["validation_issues"].extend(exc.issues or [{"code": exc.code, "field": field, "message": str(exc), "suggestion": "请检查对应资源是否可用及配置是否完整。"}])
        return record

    def archive(self, user_id: str, agent_id: str) -> dict[str, Any]:
        return self._private_service(user_id).archive(agent_id)

    def restore(self, user_id: str, agent_id: str) -> dict[str, Any]:
        return self._private_service(user_id).restore(agent_id)

    def capabilities(self, user_id: str) -> dict[str, Any]:
        result = self._private_service(user_id).capabilities()
        from codepilot.hooks.store import HookStore
        store = HookStore(self.users_root.parent)
        for record in store.list(user_id):
            if not record["archived"]:
                definition = store.load(user_id, record["hook_id"])["definition"]
                result["hooks"].append({**{key: record[key] for key in ("hook_id", "hook_type", "plugin_type")},
                                        "name": record["name"], "available": True,
                                        "parameters": definition["parameters"]})
        delegates = [{"agent_id": profile.agent_id, "name": profile.name, "description": profile.description,
                      "launch_modes": profile.launch_modes}
                     for profile in self.list_active_delegated_profile_snapshots(user_id)]
        result["delegates"] = delegates
        result["subagents"] = delegates
        return result

    def _resolve_resource_versions(self, user_id: str, profile: AgentProfile) -> AgentProfile:
        from codepilot.tools.code_store import ToolStore, resolved_call_name
        tool_store = ToolStore(self.users_root.parent)
        tool_versions = {}
        tool_names = set(self.tool_registry._tools)
        if profile.tool_ids and profile.readonly:
            raise AgentConfigError("只读 Agent 不允许执行代码工具", code="tool_readonly_forbidden", status=409)
        for identity in profile.tool_ids:
            bundle = tool_store.resolve(user_id, identity, profile.resolved_tool_versions.get(identity))
            name = resolved_call_name(identity, bundle['definition'])
            if name in tool_names:
                raise AgentConfigError(f"工具调用名称 {name} 冲突，请修改代码工具的调用名称", code="tool_name_conflict", status=409)
            tool_names.add(name)
            tool_versions[identity] = bundle["version"]
        profile = profile.model_copy(update={"resolved_tool_versions": tool_versions})
        from codepilot.session.publications import split_identity
        for identity in profile.skill_ids or []:
            if identity.startswith("public:") and split_identity(identity)[0] != profile.publication_id:
                raise AgentConfigError("公共 Skill 不属于当前 Agent", status=403)
        from codepilot.hooks.store import HookStore
        from codepilot.hooks.manager import validate_parameters
        hook_store = HookStore(self.users_root.parent)
        platform_versions = {plugin.hook_id: hashlib.sha256(plugin.model_dump_json().encode()).hexdigest()
                             for plugin in self.settings.hooks.plugins
                             if plugin.enabled and (profile.hook_ids is None or plugin.hook_id in profile.hook_ids)}
        if profile.resolved_platform_hook_versions is not None and platform_versions != profile.resolved_platform_hook_versions:
            raise AgentConfigError("平台 Hook 配置已变化，执行快照不可用", code="hook_version_changed", status=409)
        profile = profile.model_copy(update={"resolved_platform_hook_versions": platform_versions})
        hook_versions, private_hooks = {}, []
        for identity in profile.hook_ids or []:
            if not identity.startswith(("personal:", "public:")):
                continue
            selected_store, selected_id = hook_store, identity
            if identity.startswith("public:"):
                from codepilot.session.publications import split_identity
                publication, resource = split_identity(identity)
                if publication != profile.publication_id:
                    raise AgentConfigError("公共 Hook 不属于当前 Agent", status=403)
                selected_store, selected_id = HookStore(self.users_root.parent, publication_id=publication), "personal:" + resource
            if selected_store.get(user_id, selected_id)["archived"]:
                raise AgentConfigError("Hook 已归档", code="hook_archived", status=409)
            hook = selected_store.load(user_id, selected_id, profile.resolved_hook_versions.get(identity))
            hook["hook_id"] = identity
            hook_store.check_command(hook["definition"])
            try:
                validate_parameters(profile.hook_parameters.get(identity, {}), hook["definition"]["parameters"])
            except ValueError as exc:
                raise AgentConfigError(str(exc), code="hook_parameters_invalid", status=409) from exc
            hook_versions[identity] = hook["version"]
            private_hooks.append(hook)
        profile = profile.model_copy(update={"resolved_hook_versions": hook_versions})
        targets = set()
        if profile.supports_direct and profile.working_directory and self.workspace_path is not None:
            from codepilot.session.working_directory import resolve_working_directory
            resolve_working_directory(self.settings, self.workspace_path, profile.working_directory)
        if profile.connection_ids is not None:
            from codepilot.session.connections import ConnectionStore
            store = ConnectionStore(self.users_root.parent, self.settings)
            versions = {}
            targets = set()
            for identity in profile.connection_ids:
                record, _ = store.resolve(user_id, identity, profile.resolved_connection_versions.get(identity))
                if record["target"] in targets:
                    raise AgentConfigError("同一服务只能选择一个连接身份", code="connection_conflict", status=409)
                targets.add(record["target"])
                versions[identity] = record["revision"]
            if any(permission.startswith("mcp:") and permission not in targets for permission in profile.allowed_tools):
                raise AgentConfigError("MCP 服务尚未选择连接身份", code="connection_missing", status=409)
            hooks = [hook for hook in self.settings.hooks.plugins if hook.enabled and (profile.hook_ids is None or hook.hook_id in profile.hook_ids)]
            if any(hook.plugin_type == "http" and hook.config.get("headers_from_env") and f"hook:{hook.hook_id}" not in targets for hook in hooks):
                raise AgentConfigError("HTTP Hook 尚未选择连接身份", code="connection_missing", status=409)
            profile = profile.model_copy(update={"resolved_connection_versions": versions})
        if any(hook["definition"]["credential_headers"] and f"hook:{hook['hook_id']}" not in targets for hook in private_hooks):
            raise AgentConfigError("个人 HTTP Hook 尚未选择连接身份", code="connection_missing", status=409)
        for identity in profile.tool_ids:
            bundle = tool_store.load(user_id, identity, tool_versions[identity])
            if bundle["definition"]["credential_fields"] and "tool:" + identity not in targets:
                raise AgentConfigError("代码工具尚未选择个人凭证", code="connection_missing", status=409)
        if profile.skill_ids == []:
            return profile
        from codepilot.skills.store import SkillStore
        from codepilot.skills import SkillRegistry

        registry = SkillRegistry(self.users_root.parent / "skills")
        if not profile.publication_id:
            registry.discover()
        SkillStore(self.users_root.parent).resolve(user_id, profile.skill_ids, registry)
        return profile

    def get_active_profile_snapshot(self, user_id: str, agent_id: str) -> AgentProfile:
        try:
            self.publications.get(agent_id)
        except AgentConfigError as exc:
            if exc.status != 404:
                raise
        else:
            return self.resolve_execution_profile(user_id, self.publications.profile(user_id, agent_id))
        private = self._try_profile(self._private_service(user_id), agent_id, active=True)
        profile = private if private is not None else self.shared.get_active_profile_snapshot(agent_id)
        try:
            return self.resolve_execution_profile(user_id, profile)
        except AgentConfigError as exc:
            if not exc.issues:
                field = "tool_ids" if exc.code.startswith("tool") else "hook_ids" if exc.code.startswith("hook") else "skill_ids" if exc.code.startswith("skill") else "connection_ids" if exc.code.startswith("connection") else "working_directory" if exc.code.startswith("working_directory") else None
                exc.issues = [asdict(AgentIssue(exc.code, field, str(exc), "请检查对应资源是否存在、未归档且配置完整。"))]
            raise

    def resolve_execution_profile(self, user_id: str, profile: AgentProfile) -> AgentProfile:
        """主进程和 worker 共用依赖解析；已提供的资源版本只校验、不替换。"""
        if profile.publication_id:
            issues = self.shared._dependency_issues(profile)
            if issues:
                raise AgentConfigError("公共 Agent 依赖待配置", code="agent_dependencies_missing", status=409, issues=issues)
            children = {}
            for identity, value in profile.publication_children.items():
                child = AgentProfile.model_validate(value)
                issues = self.shared._dependency_issues(child)
                if issues:
                    raise AgentConfigError("公共子 Agent 依赖待配置", code="agent_dependencies_missing", status=409, issues=issues)
                children[identity] = self._resolve_resource_versions(user_id, child).model_dump()
            return self._resolve_resource_versions(user_id, profile.model_copy(update={"publication_children": children}))
        for child_id in profile.delegate_agent_ids or []:
            if child_id == profile.agent_id:
                raise AgentConfigError("委派目标必须是子 Agent", code="subagent_dependency_invalid", status=409)
            try:
                child = self._try_profile(self._private_service(user_id), child_id, active=True)
                if child is None:
                    child = self.shared.get_active_profile_snapshot(child_id)
                if not child.supports_delegated:
                    raise AgentConfigError("委派目标未启用 delegated 模式", code="delegate_target_forbidden", status=409)
                self._resolve_resource_versions(user_id, child)
            except AgentConfigError as exc:
                # 仅使用当前用户可见的委派身份，不查询其他用户的资源名称。
                issues = [AgentIssue(item["code"], "delegate_agent_ids", f"委派 Agent {child_id}：{item['message']}", item.get("suggestion", "请检查委派 Agent 配置。")) for item in exc.issues]
                raise AgentConfigError("委派 Agent 依赖不可用", code=exc.code, status=exc.status,
                                       issues=issues or [AgentIssue(exc.code, "delegate_agent_ids", f"委派 Agent {child_id}：{exc}", "请检查委派清单及对应 Agent 配置。")]) from exc
        return self._resolve_resource_versions(user_id, profile)

    def get_profile_revision_snapshot(self, user_id: str, agent_id: str, revision_id: str, *, resolve_resources: bool = True) -> AgentProfile:
        try:
            self.publications.get(agent_id)
        except AgentConfigError as exc:
            if exc.status != 404:
                raise
        else:
            profile = self.publications.profile(user_id, agent_id, revision_id)
            return self.resolve_execution_profile(user_id, profile) if resolve_resources else profile
        private = self._try_revision_profile(self._private_service(user_id), agent_id, revision_id)
        profile = private if private is not None else self.shared.get_profile_revision_snapshot(agent_id, revision_id)
        issues = self._private_service(user_id)._dependency_issues(profile)
        if issues:
            raise AgentConfigError("历史配置的执行依赖不可用", code="agent_dependencies_missing", status=409, issues=issues)
        return self.resolve_execution_profile(user_id, profile) if resolve_resources else profile

    def get_record_snapshot(self, user_id: str, agent_id: str) -> dict[str, Any]:
        try:
            record = self.publications.get(agent_id)
        except AgentConfigError as exc:
            if exc.status != 404:
                raise
        else:
            self.publications.check_history_access(record, user_id)
            return {"agent_id": agent_id, "name": record["name"], "archived": record["status"] != "published",
                    "validation_status": "valid", "profile": self.publications.profile(user_id, agent_id, active=False)}
        private = self._try_record(self._private_service(user_id), agent_id)
        return private if private is not None else self.shared.get_record_snapshot(agent_id)

    def list_active_profile_snapshots(self, user_id: str) -> list[AgentProfile]:
        return self.shared.list_active_profile_snapshots() + self._private_service(user_id).list_active_profile_snapshots()

    def list_active_subagent_profile_snapshots(self, user_id: str) -> list[AgentProfile]:
        return self.list_active_delegated_profile_snapshots(user_id)

    def list_active_delegated_profile_snapshots(self, user_id: str) -> list[AgentProfile]:
        return (
            self.shared.list_active_delegated_profile_snapshots()
            + self._private_service(user_id).list_active_delegated_profile_snapshots()
        )

    def _private_service(self, user_id: str) -> AgentConfigService:
        with self._lock:
            service = self._private.get(user_id)
            if service is not None:
                return service
            root = (self.users_root / user_id / "agents").resolve()
            if not root.is_relative_to(self.users_root):
                raise AgentConfigError("用户 Agent 目录越界", code="agent_storage_error", status=500)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            service = AgentConfigService(
                settings=self.settings,
                root=root,
                agent_profiles={},
                tool_registry=self.tool_registry,
                mcp_manager=self.mcp_manager,
                visibility="private",
                owner_user_id=user_id,
            )
            self._private[user_id] = service
            return service

    @staticmethod
    def _try_get(service: AgentConfigService, agent_id: str) -> dict[str, Any] | None:
        try:
            return service.get(agent_id)
        except AgentConfigError as exc:
            if exc.code == "agent_not_found":
                return None
            raise

    @staticmethod
    def _try_profile(service: AgentConfigService, agent_id: str, *, active: bool) -> AgentProfile | None:
        try:
            return service.get_active_profile_snapshot(agent_id) if active else None
        except AgentConfigError as exc:
            if exc.code == "agent_not_found":
                return None
            raise

    @staticmethod
    def _try_record(service: AgentConfigService, agent_id: str) -> dict[str, Any] | None:
        try:
            return service.get_record_snapshot(agent_id)
        except AgentConfigError as exc:
            if exc.code == "agent_not_found":
                return None
            raise

    @staticmethod
    def _try_revision_profile(service: AgentConfigService, agent_id: str, revision_id: str) -> AgentProfile | None:
        try:
            return service.get_profile_revision_snapshot(agent_id, revision_id)
        except AgentConfigError as exc:
            if exc.code == "agent_not_found":
                return None
            raise
