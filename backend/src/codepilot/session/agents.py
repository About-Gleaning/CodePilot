from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


BUILTIN_AGENT_DIR = Path(__file__).resolve().parent / "agent_profiles"
BUILTIN_AGENT_NAMES = frozenset({"build", "plan", "explore"})


class AgentAssembly(BaseModel):
    """可选字段为空表示沿用旧行为；空数组表示显式关闭对应能力。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool_ids: list[str] = Field(default_factory=list, max_length=100)
    hook_ids: list[str] | None = Field(default=None, max_length=100)
    hook_parameters: dict[str, dict[str, str | int | bool]] = Field(default_factory=dict, max_length=100)
    skill_ids: list[str] | None = Field(default=None, max_length=100)
    working_directory: str | None = Field(default=None, max_length=4096)
    connection_ids: list[str] | None = Field(default=None, max_length=100)
    memory_enabled: bool = True
    delegate_agent_ids: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_delegate_ids(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "delegate_agent_ids" not in migrated:
            if "subagent_ids" in migrated:
                migrated["delegate_agent_ids"] = migrated["subagent_ids"]
            elif "allowed_subagent_ids" in migrated:
                migrated["delegate_agent_ids"] = migrated["allowed_subagent_ids"]
        migrated.pop("subagent_ids", None)
        migrated.pop("allowed_subagent_ids", None)
        return migrated

    @property
    def subagent_ids(self) -> list[str] | None:
        """兼容迁移期间的旧调用方；序列化不会再写入旧字段。"""
        return self.delegate_agent_ids


class AgentProfile(AgentAssembly):
    publication_id: str | None = None
    publication_children: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resolved_connection_versions: dict[str, str] = Field(default_factory=dict)
    resolved_tool_versions: dict[str, str] = Field(default_factory=dict)
    resolved_hook_versions: dict[str, str] = Field(default_factory=dict)
    resolved_platform_hook_versions: dict[str, str] | None = None
    name: str
    # agent_id/revision_id 为后续运行时归属预留；旧 Markdown 缺失时由加载器派生。
    agent_id: str = ""
    revision_id: str = ""
    source: Literal["builtin", "custom"] = "custom"
    visibility: Literal["builtin", "shared", "private"] = "private"
    description: str = ""
    system_prompt: str
    launch_modes: list[Literal["direct", "delegated"]] = Field(default_factory=lambda: ["direct"], min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    readonly: bool = False
    max_iterations: int = 50
    can_delegate: bool = False
    default_provider: str | None = None
    default_model: str | None = None
    default_thinking_value: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_role_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "launch_modes" not in migrated and "kind" in migrated:
            migrated["launch_modes"] = ["delegated" if migrated["kind"] == "subagent" else "direct"]
        if "can_delegate" not in migrated and "can_call_subagent" in migrated:
            migrated["can_delegate"] = migrated["can_call_subagent"]
        migrated.pop("kind", None)
        migrated.pop("can_call_subagent", None)
        return migrated

    @model_validator(mode="after")
    def _validate_launch_modes(self) -> AgentProfile:
        self.launch_modes = list(dict.fromkeys(self.launch_modes))
        if self.can_delegate and "task" not in self.allowed_tools:
            raise ValueError("启用委派时必须在 tools 中声明 task 工具")
        return self

    @property
    def kind(self) -> Literal["agent", "subagent"]:
        """兼容旧客户端：双模式 Agent 优先表示为可直接启动的 agent。"""
        return "agent" if "direct" in self.launch_modes else "subagent"

    @property
    def can_call_subagent(self) -> bool:
        return self.can_delegate

    @property
    def supports_direct(self) -> bool:
        return "direct" in self.launch_modes

    @property
    def supports_delegated(self) -> bool:
        return "delegated" in self.launch_modes


class AgentProfileError(ValueError):
    """Agent markdown 配置错误。"""

    def __init__(self, message: str, *, code: str | None = None, field: str | None = None) -> None:
        super().__init__(message)
        self.code, self.field = code, field


def build_agent_profiles(
    max_iterations: int,
    subagent_max_iterations: int = 8,
    *,
    custom_agents_root: str | Path | None = None,
) -> dict[str, AgentProfile]:
    """从内置目录和自定义目录加载 Agent 配置。"""
    profiles = _load_builtin_agent_profiles(max_iterations, subagent_max_iterations)
    custom_profiles = _load_custom_agent_profiles(custom_agents_root, max_iterations, subagent_max_iterations)
    for name, profile in custom_profiles.items():
        if name in profiles:
            raise AgentProfileError(f"自定义 agent `{name}` 不能覆盖内置 agent")
        profiles[name] = profile
    return profiles


def _load_builtin_agent_profiles(max_iterations: int, subagent_max_iterations: int) -> dict[str, AgentProfile]:
    profiles = _load_agent_profiles_from_dir(BUILTIN_AGENT_DIR, max_iterations, subagent_max_iterations)
    missing = sorted(BUILTIN_AGENT_NAMES - set(profiles))
    if missing:
        raise AgentProfileError(f"缺少内置 agent：{', '.join(missing)}")
    extra = sorted(set(profiles) - BUILTIN_AGENT_NAMES)
    if extra:
        raise AgentProfileError(f"内置目录只能包含 build、plan、explore，发现：{', '.join(extra)}")
    return profiles


def _load_custom_agent_profiles(
    custom_agents_root: str | Path | None,
    max_iterations: int,
    subagent_max_iterations: int,
) -> dict[str, AgentProfile]:
    if custom_agents_root is None:
        return {}
    root = Path(custom_agents_root).expanduser().resolve()
    if not root.is_dir():
        return {}
    return _load_agent_profiles_from_dir(root, max_iterations, subagent_max_iterations)


def _load_agent_profiles_from_dir(
    root: Path,
    max_iterations: int,
    subagent_max_iterations: int,
) -> dict[str, AgentProfile]:
    profiles: dict[str, AgentProfile] = {}
    for path in sorted(root.glob("*.md"), key=lambda item: item.name.lower()):
        profile = parse_agent_markdown(path, max_iterations=max_iterations, subagent_max_iterations=subagent_max_iterations)
        if profile.name in profiles:
            raise AgentProfileError(f"agent `{profile.name}` 重复定义")
        profiles[profile.name] = profile
    return profiles


def parse_agent_markdown(
    path: Path,
    *,
    max_iterations: int,
    subagent_max_iterations: int,
) -> AgentProfile:
    raw = path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(raw, path)
    name = _require_string(metadata, "name", path)
    launch_modes = _read_launch_modes(metadata, path)
    description = _require_string(metadata, "description", path)
    tools = _require_string_list(metadata, "tools", path)
    readonly = _optional_bool(metadata, "readonly", default=False, path=path)
    can_delegate = _optional_bool(
        metadata,
        "can_delegate" if "can_delegate" in metadata else "can_call_subagent",
        default=False,
        path=path,
    )
    configured_iterations = _optional_positive_int(metadata, "max_iterations", path=path)
    iterations = configured_iterations or (
        subagent_max_iterations if launch_modes == ["delegated"] else max_iterations
    )
    prompt = body.strip()
    if not prompt:
        raise AgentProfileError("Agent 指令正文不能为空", code="agent_field_required", field="system_prompt")
    if can_delegate and "task" not in tools:
        raise AgentProfileError("启用子 Agent 委派时必须在 tools 中声明 task 工具", code="agent_field_invalid", field="tools")
    return AgentProfile(
        name=name,
        agent_id=_optional_string(metadata, "agent_id", path=path) or "",
        revision_id=_optional_string(metadata, "revision_id", path=path) or "",
        description=description,
        system_prompt=prompt,
        launch_modes=launch_modes,
        allowed_tools=tools,
        readonly=readonly,
        max_iterations=iterations,
        can_delegate=can_delegate,
        default_provider=_optional_string(metadata, "default_provider", path=path),
        default_model=_optional_string(metadata, "default_model", path=path),
        default_thinking_value=_optional_string(metadata, "default_thinking_value", path=path),
        **AgentAssembly.model_validate(
            {
                key: metadata[key]
                for key in (*AgentAssembly.model_fields, "subagent_ids", "allowed_subagent_ids")
                if key in metadata
            }
        ).model_dump(),
    )


def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        raise AgentProfileError("配置文件缺少 YAML 头部", code="agent_yaml_invalid")
    marker = "\n---\n"
    end = raw.find(marker, 4)
    if end < 0:
        raise AgentProfileError("配置文件 YAML 头部未正确结束", code="agent_yaml_invalid")
    metadata_raw = raw[4:end]
    body = raw[end + len(marker) :]
    try:
        metadata = yaml.safe_load(metadata_raw) or {}
    except yaml.YAMLError as exc:
        raise AgentProfileError("配置文件 YAML 语法错误，请检查缩进和标点", code="agent_yaml_invalid") from exc
    if not isinstance(metadata, dict):
        raise AgentProfileError("配置文件 YAML 头部必须是字段映射", code="agent_yaml_invalid")
    return metadata, body


def _require_string(metadata: dict[str, Any], key: str, path: Path) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentProfileError(f"缺少有效字段 {key}，必须填写非空文本", code="agent_field_required", field=key)
    return value.strip()


def _read_launch_modes(metadata: dict[str, Any], path: Path) -> list[Literal["direct", "delegated"]]:
    value = metadata.get("launch_modes")
    if value is None:
        kind = _require_string(metadata, "kind", path)
        if kind not in {"agent", "subagent"}:
            raise AgentProfileError("字段 kind 只能是 agent 或 subagent", code="agent_field_invalid", field="kind")
        return ["delegated" if kind == "subagent" else "direct"]
    if not isinstance(value, list) or not value:
        raise AgentProfileError("字段 launch_modes 必须是非空数组", code="agent_field_invalid", field="launch_modes")
    modes: list[Literal["direct", "delegated"]] = []
    for item in value:
        if item not in {"direct", "delegated"}:
            raise AgentProfileError(
                "字段 launch_modes 只能包含 direct 或 delegated",
                code="agent_field_invalid",
                field="launch_modes",
            )
        if item not in modes:
            modes.append(item)
    return modes


def _require_string_list(metadata: dict[str, Any], key: str, path: Path) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        raise AgentProfileError(f"字段 {key} 必须是字符串数组", code="agent_field_invalid", field=key)
    tools: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AgentProfileError(f"字段 {key} 只能包含非空字符串", code="agent_field_invalid", field=key)
        tools.append(item.strip())
    if not tools:
        raise AgentProfileError(f"字段 {key} 不能为空", code="agent_field_required", field=key)
    return tools


def _optional_bool(metadata: dict[str, Any], key: str, *, default: bool, path: Path) -> bool:
    value = metadata.get(key, default)
    if not isinstance(value, bool):
        raise AgentProfileError(f"字段 {key} 必须是布尔值", code="agent_field_invalid", field=key)
    return value


def _optional_positive_int(metadata: dict[str, Any], key: str, *, path: Path) -> int | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AgentProfileError(f"字段 {key} 必须是正整数", code="agent_field_invalid", field=key)
    return value


def _optional_string(metadata: dict[str, Any], key: str, *, path: Path) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AgentProfileError(f"字段 {key} 必须是非空字符串", code="agent_field_invalid", field=key)
    return value.strip()
