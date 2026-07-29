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

    def __init__(self, *, settings: AppSettings, root: Path, agent_profiles: dict[str, AgentProfile], tool_registry: Any, mcp_manager: Any) -> None:
        self.settings, self.root, self.agent_profiles = settings, root.resolve(), agent_profiles
        self.tool_registry, self.mcp_manager = tool_registry, mcp_manager
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

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            profile, metadata = self._validate_payload(payload, name_required=True)
            existing = self._by_name(profile.name)
            if existing:
                candidate = self._render(profile.model_copy(update={"agent_id": existing.agent_id, "source": "custom"}), metadata)
                if existing.revision_id == self._revision(candidate):
                    return self._view(existing, detail=True)
                raise AgentConfigError("Agent 名称已存在", code="agent_name_conflict", status=409)
            profile = profile.model_copy(update={"agent_id": str(uuid.uuid4()), "source": "custom"})
            rendered = self._render(profile, metadata)
            revision = self._revision(rendered)
            profile = profile.model_copy(update={"revision_id": revision})
            rendered = self._render(profile, metadata)
            path = self.root / f"{profile.name}.md"
            self._persist(profile, metadata, rendered, path)
            record = AgentRecord(profile, profile.agent_id, revision, "custom", False, path, metadata)
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
            profile = profile.model_copy(update={"agent_id": record.agent_id, "source": "custom"})
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
        providers = [{"provider": p.provider, "label": p.label, "models": p.models,
                      "model_capabilities": {m: {"thinking": v.thinking.model_dump() if v.thinking else None} for m, v in p.model_settings.items()}}
                     for p in self.settings.llm_runtime.activated_providers.values()]
        return {"providers": providers, "tools": tools, "mcp_servers": mcp}

    def _load_records(self) -> None:
        for name, profile in self.agent_profiles.items():
            source: Literal["builtin", "custom"] = "builtin" if name in BUILTIN_AGENT_NAMES else "custom"
            profile = profile.model_copy(update={"agent_id": profile.agent_id or self._derived_id(source, name), "source": source})
            raw = self._render(profile, {})
            revision = profile.revision_id or self._revision(raw)
            profile = profile.model_copy(update={"revision_id": revision})
            self.agent_profiles[name] = profile
            path = None if source == "builtin" else self.root / f"{name}.md"
            self._records[profile.agent_id] = AgentRecord(profile, profile.agent_id, revision, source, False, path)
        for archived in self._archive_dir().glob("*.md") if self._archive_dir().exists() else []:
            self._load_custom_file(archived, archived=True)

    def _load_custom_file(self, path: Path, *, archived: bool) -> None:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_DOCUMENT_BYTES: raise AgentProfileError("配置文件不可读取")
            profile = parse_agent_markdown(path, max_iterations=self.settings.agent.max_loop_iterations, subagent_max_iterations=self.settings.agent.subagent_max_loop_iterations)
            raw = path.read_text(encoding="utf-8")
            metadata = self._metadata(raw)
            agent_id = profile.agent_id or self._derived_id("custom", profile.name)
            revision = profile.revision_id or self._revision(self._render(profile.model_copy(update={"agent_id": agent_id}), metadata))
            profile = profile.model_copy(update={"agent_id": agent_id, "revision_id": revision, "source": "custom"})
            if agent_id not in self._records:
                self._records[agent_id] = AgentRecord(profile, agent_id, revision, "custom", archived, path, metadata)
        except Exception:
            agent_id = self._derived_id("invalid", path.name)
            self._records[agent_id] = AgentRecord(None, agent_id, "", "custom", archived, path, {"name": path.stem}, [AgentIssue("agent_parse_failed", None, "配置文件格式无效")])

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
                  "archived": record.archived, "validation_status": record.status,
                  "validation_issues": [{"code": i.code, "field": i.field, "message": i.message} for i in record.issues]}
        if profile: result.update({"description": profile.description, "readonly": profile.readonly})
        if detail and profile:
            result.update({"system_prompt": profile.system_prompt, "default_provider": profile.default_provider, "default_model": profile.default_model,
                           "default_thinking_value": profile.default_thinking_value, "tool_names": [x for x in profile.allowed_tools if not x.startswith("mcp:")],
                           "mcp_server_names": [x[4:] for x in profile.allowed_tools if x.startswith("mcp:")]})
        return result

    def _derived_id(self, source: str, value: str) -> str: return str(uuid.uuid5(_NAMESPACE, f"{source}:{value}"))
    def _archive_dir(self) -> Path: return self.root / ".archived"
    def _revision_dir(self, agent_id: str) -> Path: return self.root / ".revisions" / agent_id
    def _path_for(self, name: str, archived: bool) -> Path: return (self._archive_dir() if archived else self.root) / f"{name}.md"
