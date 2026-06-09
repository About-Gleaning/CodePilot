from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from codepilot.session.agents import build_agent_profiles
from codepilot.session.state import AgentState, LLMState, SessionState, SessionStatus
from codepilot.session.system_prompt import _build_current_time_context, build_system_prompt
from codepilot.skills import SkillRegistry
from codepilot.utils import utc_now_iso


def write_skill(skills_root: Path, dirname: str, content: str) -> Path:
    skill_dir = skills_root / dirname
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def test_skill_registry_discovers_and_sorts_skills(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    write_skill(skills_root, "zeta", "---\nname: zeta\ndescription: Z\n---\n# Z\n")
    write_skill(skills_root, "alpha", "---\nname: alpha\ndescription: A\n---\n# A\n")

    registry = SkillRegistry(skills_root)
    skills = registry.discover()

    assert [skill.name for skill in skills] == ["alpha", "zeta"]
    assert registry.list_briefs() == [
        {"name": "alpha", "description": "A"},
        {"name": "zeta", "description": "Z"},
    ]


def test_skill_registry_uses_directory_and_body_fallback(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    write_skill(skills_root, "fallback", "# 标题\n\n第一条有效说明\n后续内容\n")

    registry = SkillRegistry(skills_root)
    registry.discover()

    skill = registry.get_skill("FALLBACK")
    assert skill is not None
    assert skill.name == "fallback"
    assert skill.description == "第一条有效说明"


def test_skill_registry_missing_root_is_empty(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "missing")

    assert registry.discover() == []
    assert registry.list_briefs() == []


def test_system_prompt_lists_skills_without_full_content_or_path(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = write_skill(
        skills_root,
        "demo",
        "---\nname: demo\ndescription: 演示 skill\n---\n# Demo\n完整规范正文\n",
    )
    registry = SkillRegistry(skills_root)
    registry.discover()

    prompt = build_system_prompt(
        session=build_session(tmp_path),
        workspace=SimpleNamespace(workspace_path=tmp_path),
        agent_state=AgentState(name="build", role="build"),
        agent_profile=build_agent_profiles(max_iterations=3)["build"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=registry,
    )

    assert "<name>demo</name>" in prompt
    assert "<description>演示 skill</description>" in prompt
    assert "load_skill" in prompt
    assert "完整规范正文" not in prompt
    assert str(skill_dir) not in prompt


def test_system_prompt_reports_empty_skills(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "missing")
    registry.discover()

    prompt = build_system_prompt(
        session=build_session(tmp_path),
        workspace=SimpleNamespace(workspace_path=tmp_path),
        agent_state=AgentState(name="build", role="build"),
        agent_profile=build_agent_profiles(max_iterations=3)["build"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=registry,
    )

    assert "当前没有可用 skills" in prompt


def test_system_prompt_injects_explicit_time_context_without_model_inference(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        session=build_session(tmp_path),
        workspace=SimpleNamespace(workspace_path=tmp_path),
        agent_state=AgentState(name="build", role="build"),
        agent_profile=build_agent_profiles(max_iterations=3)["build"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=None,
    )

    assert "- 当前本地完整时间：" in prompt
    assert "- 当前本地星期：" not in prompt
    assert "- 当前时间：" not in prompt


def test_build_current_time_context_formats_local_date_weekday_and_utc_offset() -> None:
    now = datetime(2026, 6, 5, 17, 11, 32, tzinfo=timezone(timedelta(hours=8), "CST"))

    lines = _build_current_time_context(now)

    assert lines == [
        "- 当前本地完整时间：2026-06-05 星期五 17:11:32（CST，UTC+08:00）",
    ]


def build_session(workspace_path: Path) -> SessionState:
    return SessionState(
        session_id="session_1",
        workspace_id="workspace_1",
        workspace_path=str(workspace_path),
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        status=SessionStatus.RUNNING,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        messages=[],
    )
