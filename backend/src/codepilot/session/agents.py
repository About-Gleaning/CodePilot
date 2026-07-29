from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


BUILTIN_AGENT_DIR = Path(__file__).resolve().parent / "agent_profiles"
BUILTIN_AGENT_NAMES = frozenset({"build", "plan", "explore"})


class AgentProfile(BaseModel):
    name: str
    # agent_id/revision_id 为后续运行时归属预留；旧 Markdown 缺失时由加载器派生。
    agent_id: str = ""
    revision_id: str = ""
    source: Literal["builtin", "custom"] = "custom"
    description: str = ""
    system_prompt: str
    kind: Literal["agent", "subagent"] = "agent"
    allowed_tools: list[str] = Field(default_factory=list)
    readonly: bool = False
    max_iterations: int = 50
    can_call_subagent: bool = False
    default_provider: str | None = None
    default_model: str | None = None
    default_thinking_value: str | None = None


class AgentProfileError(ValueError):
    """Agent markdown 配置错误。"""


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
    kind = _require_kind(metadata, path)
    description = _require_string(metadata, "description", path)
    tools = _require_string_list(metadata, "tools", path)
    readonly = _optional_bool(metadata, "readonly", default=False, path=path)
    can_call_subagent = _optional_bool(metadata, "can_call_subagent", default=False, path=path)
    configured_iterations = _optional_positive_int(metadata, "max_iterations", path=path)
    iterations = configured_iterations or (subagent_max_iterations if kind == "subagent" else max_iterations)
    prompt = body.strip()
    if not prompt:
        raise AgentProfileError(f"{path} 的正文 prompt 不能为空")
    if can_call_subagent and "task" not in tools:
        raise AgentProfileError(f"agent `{name}` can_call_subagent=true 时必须在 tools 中声明 task")
    if kind == "subagent" and can_call_subagent:
        raise AgentProfileError(f"subagent `{name}` 不能调用其他 subagent")
    return AgentProfile(
        name=name,
        agent_id=_optional_string(metadata, "agent_id", path=path) or "",
        revision_id=_optional_string(metadata, "revision_id", path=path) or "",
        description=description,
        system_prompt=prompt,
        kind=kind,
        allowed_tools=tools,
        readonly=readonly,
        max_iterations=iterations,
        can_call_subagent=can_call_subagent,
        default_provider=_optional_string(metadata, "default_provider", path=path),
        default_model=_optional_string(metadata, "default_model", path=path),
        default_thinking_value=_optional_string(metadata, "default_thinking_value", path=path),
    )


def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        raise AgentProfileError(f"{path} 缺少 YAML frontmatter")
    marker = "\n---\n"
    end = raw.find(marker, 4)
    if end < 0:
        raise AgentProfileError(f"{path} 的 YAML frontmatter 未正确结束")
    metadata_raw = raw[4:end]
    body = raw[end + len(marker) :]
    try:
        metadata = yaml.safe_load(metadata_raw) or {}
    except yaml.YAMLError as exc:
        raise AgentProfileError(f"{path} 的 YAML frontmatter 解析失败：{exc}") from exc
    if not isinstance(metadata, dict):
        raise AgentProfileError(f"{path} 的 YAML frontmatter 必须是对象")
    return metadata, body


def _require_string(metadata: dict[str, Any], key: str, path: Path) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentProfileError(f"{path} 缺少有效字段 `{key}`")
    return value.strip()


def _require_kind(metadata: dict[str, Any], path: Path) -> Literal["agent", "subagent"]:
    kind = _require_string(metadata, "kind", path)
    if kind not in {"agent", "subagent"}:
        raise AgentProfileError(f"{path} 字段 `kind` 只能是 agent 或 subagent")
    return kind


def _require_string_list(metadata: dict[str, Any], key: str, path: Path) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        raise AgentProfileError(f"{path} 字段 `{key}` 必须是字符串数组")
    tools: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AgentProfileError(f"{path} 字段 `{key}` 只能包含非空字符串")
        tools.append(item.strip())
    if not tools:
        raise AgentProfileError(f"{path} 字段 `{key}` 不能为空")
    return tools


def _optional_bool(metadata: dict[str, Any], key: str, *, default: bool, path: Path) -> bool:
    value = metadata.get(key, default)
    if not isinstance(value, bool):
        raise AgentProfileError(f"{path} 字段 `{key}` 必须是布尔值")
    return value


def _optional_positive_int(metadata: dict[str, Any], key: str, *, path: Path) -> int | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AgentProfileError(f"{path} 字段 `{key}` 必须是正整数")
    return value


def _optional_string(metadata: dict[str, Any], key: str, *, path: Path) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AgentProfileError(f"{path} 字段 `{key}` 必须是非空字符串")
    return value.strip()
