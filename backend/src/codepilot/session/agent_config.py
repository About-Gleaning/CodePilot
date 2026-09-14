from __future__ import annotations

"""Agent 配置中心的持久化、校验和脱敏视图。"""

import hashlib
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from codepilot.config.settings import AppSettings, resolve_thinking_value
from codepilot.session.agents import AgentProfile, AgentProfileError, BUILTIN_AGENT_NAMES, parse_agent_markdown

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_SAVED_BYTES = 256 * 1024
MAX_PROMPT_CHARS = 100_000
_NAMESPACE = uuid.UUID("e579d464-8822-4ea4-bb19-d7c0c0c15b1f")


class AgentConfigError(ValueError):
    def __init__(self, message: str, *, code: str = "agent_config_invalid", status: int = 422) -> None:
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(slots=True)
class AgentIssue:
    code: str
    field: str | None
    message: str


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
                raise AgentConfigError("Agent 配置无效", code="agent_invalid", status=422)
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
                if not record.archived and record.profile is not None and record.profile.kind == "agent"
            ]

    def list_active_subagent_profile_snapshots(self) -> list[AgentProfile]:
        """原子复制可由 task 分派的 subagent，不混入可直接启动的主 Agent。"""
        with self._lock:
            return [
                record.profile.model_copy(deep=True)
                for record in self._records.values()
                if not record.archived and record.profile is not None and record.profile.kind == "subagent"
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
        return {"providers": providers, "tools": tools, "mcp_servers": mcp}

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
            if path.is_symlink() or path.stat().st_size > MAX_DOCUMENT_BYTES: raise AgentProfileError("配置文件不可读取")
            profile = parse_agent_markdown(path, max_iterations=self.settings.agent.max_loop_iterations, subagent_max_iterations=self.settings.agent.subagent_max_loop_iterations)
            raw = path.read_text(encoding="utf-8")
            metadata = self._metadata(raw)
            agent_id = profile.agent_id or self._derived_id("custom", profile.name)
            declared_revision = profile.revision_id
            candidate = self._render(profile.model_copy(update={"agent_id": agent_id}), metadata)
            revision = self._revision(candidate)
            if declared_revision and declared_revision != revision:
                raise AgentProfileError("revision 内容摘要不匹配")
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
        except Exception:
            agent_id = self._derived_id("invalid", path.name)
            self._records[agent_id] = AgentRecord(
                None,
                agent_id,
                "",
                "custom",
                archived,
                path,
                {"name": path.stem},
                [AgentIssue("agent_parse_failed", None, "配置文件格式无效")],
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
        profile = AgentProfile(name=name, description=description, system_prompt=prompt, kind="agent", allowed_tools=tools,
                               readonly=bool(payload.get("readonly", False)), can_call_subagent="task" in tool_names,
                               default_provider=str(payload.get("default_provider") or "").strip() or None,
                               default_model=str(payload.get("default_model") or "").strip() or None,
                               default_thinking_value=str(payload.get("default_thinking_value") or "").strip() or None)
        self._validate_profile(profile)
        metadata = dict(base_metadata or {})
        metadata.update({"name": name, "kind": "agent", "description": description, "tools": tools, "readonly": profile.readonly,
                         "can_call_subagent": profile.can_call_subagent, "default_provider": profile.default_provider,
                         "default_model": profile.default_model, "default_thinking_value": profile.default_thinking_value})
        return profile, {key: value for key, value in metadata.items() if value is not None}

    def _validate_profile(self, profile: AgentProfile) -> None:
        if not profile.default_provider or not profile.default_model: raise AgentConfigError("必须选择已激活的 Provider 和 Model", code="llm_selection_required")
        provider = self.settings.llm_runtime.activated_providers.get(profile.default_provider)
        if not provider or profile.default_model not in provider.models: raise AgentConfigError("Provider 或 Model 不可用", code="llm_selection_invalid")
        if profile.default_thinking_value:
            resolve_thinking_value(self.settings, profile.default_provider, profile.default_model, {"thinking_value": profile.default_thinking_value})
        for tool in profile.allowed_tools:
            if tool.startswith("mcp:"):
                name = tool[4:]; config = self.settings.mcp.servers.get(name)
                if not config: raise AgentConfigError("MCP 服务不存在", code="mcp_unknown")
                if self.visibility == "private" and not config.assignable_to_private_agents:
                    raise AgentConfigError("MCP 服务未开放给私有 Agent", code="mcp_not_assignable", status=403)
                continue
            item = self.tool_registry.get(tool)
            if item is None: raise AgentConfigError("Tool 不存在", code="tool_unknown")
            if not getattr(item.spec, "assignable_to_custom_agents", True):
                allowed = getattr(item.spec, "allowed_agent_names", [])
                if profile.name not in allowed: raise AgentConfigError("Tool 不能分配给当前 Agent", code="tool_restricted")

    def _persist(self, profile: AgentProfile, metadata: dict[str, Any], content: str, current: Path) -> None:
        if len(content.encode("utf-8")) > MAX_SAVED_BYTES: raise AgentConfigError("配置文件过大", code="agent_document_too_large")
        revision_path = self._revision_dir(profile.agent_id) / f"{profile.revision_id}.md"
        self._atomic_write(revision_path, content)
        self._atomic_write(current, content)

    def _render(self, profile: AgentProfile, metadata: dict[str, Any]) -> str:
        data = dict(metadata)
        data.update({"name": profile.name, "agent_id": profile.agent_id or None, "revision_id": profile.revision_id or None,
                     "kind": profile.kind, "description": profile.description, "tools": profile.allowed_tools, "readonly": profile.readonly,
                     "can_call_subagent": profile.can_call_subagent, "default_provider": profile.default_provider,
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
                  "validation_issues": [{"code": i.code, "field": i.field, "message": i.message} for i in record.issues]}
        if profile:
            result.update({
                "description": profile.description,
                "readonly": profile.readonly,
                # 列表页需要直接展示默认模型，但不能为此提前返回 Prompt。
                "default_provider": profile.default_provider,
                "default_model": profile.default_model,
                "default_thinking_value": profile.default_thinking_value,
            })
        if detail and profile:
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
    ) -> None:
        self.settings = settings
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

    def list(self, user_id: str, status: str = "active") -> list[dict[str, Any]]:
        records = self.shared.list(status) + self._private_service(user_id).list(status)
        return sorted(records, key=lambda item: (bool(item.get("archived")), str(item.get("name")), str(item.get("agent_id"))))

    def get(self, user_id: str, agent_id: str) -> dict[str, Any]:
        private = self._try_get(self._private_service(user_id), agent_id)
        return private if private is not None else self.shared.get(agent_id)

    def create(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._private_service(user_id).create(payload)

    def update(self, user_id: str, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._private_service(user_id).update(agent_id, payload)

    def archive(self, user_id: str, agent_id: str) -> dict[str, Any]:
        return self._private_service(user_id).archive(agent_id)

    def restore(self, user_id: str, agent_id: str) -> dict[str, Any]:
        return self._private_service(user_id).restore(agent_id)

    def capabilities(self, user_id: str) -> dict[str, Any]:
        return self._private_service(user_id).capabilities()

    def get_active_profile_snapshot(self, user_id: str, agent_id: str) -> AgentProfile:
        private = self._try_profile(self._private_service(user_id), agent_id, active=True)
        return private if private is not None else self.shared.get_active_profile_snapshot(agent_id)

    def get_profile_revision_snapshot(self, user_id: str, agent_id: str, revision_id: str) -> AgentProfile:
        private = self._try_revision_profile(self._private_service(user_id), agent_id, revision_id)
        return private if private is not None else self.shared.get_profile_revision_snapshot(agent_id, revision_id)

    def get_record_snapshot(self, user_id: str, agent_id: str) -> dict[str, Any]:
        private = self._try_record(self._private_service(user_id), agent_id)
        return private if private is not None else self.shared.get_record_snapshot(agent_id)

    def list_active_profile_snapshots(self, user_id: str) -> list[AgentProfile]:
        return self.shared.list_active_profile_snapshots() + self._private_service(user_id).list_active_profile_snapshots()

    def list_active_subagent_profile_snapshots(self, user_id: str) -> list[AgentProfile]:
        return (
            self.shared.list_active_subagent_profile_snapshots()
            + self._private_service(user_id).list_active_subagent_profile_snapshots()
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
