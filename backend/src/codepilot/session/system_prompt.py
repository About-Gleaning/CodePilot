from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from codepilot.skills import SkillRegistry
from codepilot.session.agents import AgentProfile
from codepilot.session.state import AgentState, LLMState, SessionState


_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def build_system_prompt(
    *,
    session: SessionState,
    workspace: Any,
    agent_state: AgentState,
    agent_profile: AgentProfile,
    llm_state: LLMState,
    skill_registry: SkillRegistry | None = None,
) -> str:
    """按分层策略组装最终发送给模型的 system prompt。"""
    workspace_path = Path(workspace.workspace_path)
    sections = [
        _section("常驻层：Agent 角色说明", agent_profile.system_prompt),
        _section("常驻层：工作区 AGENTS.md", _read_workspace_agents(workspace_path)),
        _section("按需加载层：Skills 与领域知识", _build_skills_context(skill_registry)),
        _section(
            "运行时注入层：当前上下文",
            _build_runtime_context(session, workspace_path, agent_state, llm_state),
        ),
    ]
    return "\n\n".join(section for section in sections if section)


def build_runtime_context_prompt(
    *,
    session: SessionState,
    workspace: Any,
    agent_state: AgentState,
    llm_state: LLMState,
    now: datetime | None = None,
) -> str:
    """构造每轮动态上下文，放入最新用户消息尾部以避免破坏稳定 system 前缀。"""
    workspace_path = Path(workspace.workspace_path)
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    utc_offset = _format_utc_offset(local_now)
    timezone_name = local_now.tzname() or "本地时区"
    lines = [
        "<runtime_context>",
        f"current_time: {local_now:%Y-%m-%d} {_WEEKDAYS[local_now.weekday()]} {local_now:%H:%M:%S}",
        f"timezone: {timezone_name}",
        f"utc_offset: UTC{utc_offset}",
        f"workspace: {workspace_path}",
        f"agent: {agent_state.name}",
        f"model: {llm_state.provider}/{llm_state.model}",
        f"session_id: {session.session_id}",
        "</runtime_context>",
    ]
    return "\n".join(lines)


def _section(title: str, content: str | None) -> str:
    if not content:
        return ""
    return f"## {title}\n{content.strip()}"


def _read_workspace_agents(workspace_path: Path) -> str | None:
    agents_path = workspace_path / "AGENTS.md"
    if not agents_path.is_file():
        return None
    try:
        return agents_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _build_skills_context(skill_registry: SkillRegistry | None) -> str:
    if skill_registry is None or not skill_registry.skills:
        return "Skills 和领域知识采用按需加载策略：当前没有可用 skills。"

    lines = [
        "Skills 和领域知识采用按需加载策略：这里只列出可用 skill 的名称和简短描述；"
        "当任务匹配某个 skill 时，必须先调用 load_skill 加载完整 SKILL.md，再按其规范执行。",
        "",
        "<available_skills>",
    ]
    for skill in skill_registry.skills:
        # 这里只暴露路由所需信息，不暴露本地路径，避免把运行时目录结构注入模型上下文。
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(skill.description)}</description>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _build_runtime_context(
    session: SessionState,
    workspace_path: Path,
    agent_state: AgentState,
    llm_state: LLMState,
) -> str:
    git_enabled = (workspace_path / ".git").exists()
    lines = [
        f"- 工作根目录：{workspace_path}",
        f"- 当前 Agent：{agent_state.name}",
        f"- Agent 角色：{agent_state.role}",
        f"- 当前模型：{llm_state.provider}/{llm_state.model}",
        f"- 当前会话：{session.session_id}",
        f"- 是否使用 Git：{'是' if git_enabled else '否'}",
        "- 项目语言：预留，尚未接入自动语言检测。",
        "- 用户偏好：预留，尚未接入用户偏好存储。",
    ]
    return "\n".join(lines)


def _format_utc_offset(value: datetime) -> str:
    offset = value.strftime("%z")
    if not offset:
        return "+00:00"
    return f"{offset[:3]}:{offset[3:]}"
