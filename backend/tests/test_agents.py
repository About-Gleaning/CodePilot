from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.session.agents import AgentProfileError, build_agent_profiles, parse_agent_markdown


# 测试场景说明
# 场景1：正常流程 - 内置 agent 与自定义 agent 从 markdown 加载。
# 场景2：边界情况 - 未配置 max_iterations 时使用全局默认值。
# 场景3：异常处理 - 拒绝覆盖内置 agent、非法字段和不完整 subagent 调用配置。


def test_build_agent_profiles_loads_builtin_agents_only_by_default() -> None:
    profiles = build_agent_profiles(max_iterations=7, subagent_max_iterations=3)

    assert set(profiles) == {"build", "plan", "explore"}
    assert profiles["build"].kind == "agent"
    assert profiles["plan"].readonly is True
    assert profiles["explore"].kind == "subagent"
    assert profiles["build"].max_iterations == 7
    assert profiles["explore"].max_iterations == 3


def test_build_agent_profiles_loads_custom_agent(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "life.md",
        name="life",
        kind="agent",
        tools=["read_file", "long_memory_write"],
        readonly=False,
        can_call_subagent=False,
    )

    profiles = build_agent_profiles(max_iterations=5, subagent_max_iterations=2, custom_agents_root=tmp_path)

    assert "life" in profiles
    assert profiles["life"].description == "life 描述"
    assert profiles["life"].system_prompt == "life prompt"
    assert profiles["life"].allowed_tools == ["read_file", "long_memory_write"]
    assert profiles["life"].max_iterations == 5


def test_custom_agent_cannot_override_builtin_agent(tmp_path: Path) -> None:
    _write_agent(tmp_path, "build.md", name="build", kind="agent", tools=["read_file"])

    with pytest.raises(AgentProfileError, match="不能覆盖内置 agent"):
        build_agent_profiles(max_iterations=5, custom_agents_root=tmp_path)


def test_parse_agent_markdown_rejects_invalid_kind(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "bad.md", name="bad", kind="worker", tools=["read_file"])

    with pytest.raises(AgentProfileError, match="kind"):
        parse_agent_markdown(path, max_iterations=5, subagent_max_iterations=2)


def test_parse_agent_markdown_requires_task_when_agent_can_call_subagent(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "delegate.md",
        name="delegate",
        kind="agent",
        tools=["read_file"],
        can_call_subagent=True,
    )

    with pytest.raises(AgentProfileError, match="必须在 tools 中声明 task"):
        parse_agent_markdown(path, max_iterations=5, subagent_max_iterations=2)


def _write_agent(
    root: Path,
    filename: str,
    *,
    name: str,
    kind: str,
    tools: list[str],
    readonly: bool = True,
    can_call_subagent: bool = False,
) -> Path:
    path = root / filename
    path.write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"kind: {kind}",
                f"description: {name} 描述",
                "tools:",
                *[f"  - {tool}" for tool in tools],
                f"readonly: {str(readonly).lower()}",
                f"can_call_subagent: {str(can_call_subagent).lower()}",
                "---",
                f"{name} prompt",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path
