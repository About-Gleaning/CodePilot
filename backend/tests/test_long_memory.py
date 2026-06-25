from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from codepilot.session.agents import build_agent_profiles
from codepilot.session.state import AgentState, LLMState, SessionState, SessionStatus
from codepilot.session.system_prompt import build_system_prompt
from codepilot.tools import LongMemoryWriteTool, ToolExecutionContext
from codepilot.utils import utc_now_iso


MEMORY_HEADER = """---
type: memory_instruction
version: 1
applyTo:
  - life
---
"""


def test_long_memory_write_creates_instruction_file_with_frontmatter(tmp_path: Path) -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)

    result = asyncio.run(
        tool.execute({"old_string": "", "new_string": "用户喜欢简洁直接的回答"}, context=_tool_context(tmp_path))
    )

    memory_path = _memory_path(tmp_path)
    assert result["status"] == "ok"
    assert result["operation"] == "append"
    assert memory_path.read_text(encoding="utf-8") == MEMORY_HEADER + "\n- 用户喜欢简洁直接的回答\n"
    assert not (tmp_path / "memory" / "long-memory.md").exists()


def test_long_memory_write_appends_without_repeating_frontmatter(tmp_path: Path) -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)

    asyncio.run(tool.execute({"old_string": "", "new_string": "第一条记忆"}, context=_tool_context(tmp_path)))
    asyncio.run(tool.execute({"old_string": "", "new_string": "第二条记忆"}, context=_tool_context(tmp_path)))

    content = _memory_path(tmp_path).read_text(encoding="utf-8")
    assert content.count("type: memory_instruction") == 1
    assert "- 第一条记忆\n" in content
    assert "- 第二条记忆\n" in content


def test_long_memory_write_replaces_unique_text_and_returns_diff(tmp_path: Path) -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)
    _write_memory(tmp_path, "applyTo:\n  - life", "- 用户喜欢详细解释。\n")

    result = asyncio.run(
        tool.execute(
            {"old_string": "用户喜欢详细解释", "new_string": "用户喜欢简洁直接的回答"},
            context=_tool_context(tmp_path),
        )
    )

    assert result["status"] == "ok"
    assert result["operation"] == "replace"
    assert result["replaced_count"] == 1
    assert "- 用户喜欢详细解释。" in str(result["diff"])
    assert "+- 用户喜欢简洁直接的回答。" in str(result["diff"])
    assert "- 用户喜欢简洁直接的回答。\n" in _memory_path(tmp_path).read_text(encoding="utf-8")


def test_long_memory_write_rejects_missing_old_string(tmp_path: Path) -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)
    _write_memory(tmp_path, "applyTo:\n  - life", "- 用户喜欢简洁直接的回答。\n")

    result = asyncio.run(
        tool.execute(
            {"old_string": "不存在的记忆", "new_string": "用户喜欢详细解释"},
            context=_tool_context(tmp_path),
        )
    )

    assert result["status"] == "error"
    assert result["error_type"] == "LongMemoryTextNotFound"


def test_long_memory_write_rejects_non_unique_old_string(tmp_path: Path) -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)
    _write_memory(tmp_path, "applyTo:\n  - life", "- 用户喜欢简洁。\n- 用户喜欢中文简洁回答。\n")

    result = asyncio.run(
        tool.execute(
            {"old_string": "用户喜欢", "new_string": "用户喜欢详细解释"},
            context=_tool_context(tmp_path),
        )
    )

    assert result["status"] == "error"
    assert result["error_type"] == "LongMemoryMatchNotUnique"


def test_long_memory_write_rejects_unchanged_replacement(tmp_path: Path) -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)
    _write_memory(tmp_path, "applyTo:\n  - life", "- 用户喜欢简洁。\n")

    result = asyncio.run(
        tool.execute(
            {"old_string": "用户喜欢简洁", "new_string": "用户喜欢简洁"},
            context=_tool_context(tmp_path),
        )
    )

    assert result["status"] == "error"
    assert result["error_type"] == "LongMemoryContentUnchanged"


def test_long_memory_write_rejects_empty_content(tmp_path: Path) -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)

    result = asyncio.run(tool.execute({"old_string": "", "new_string": "   "}, context=_tool_context(tmp_path)))

    assert result["status"] == "error"
    assert result["error_type"] == "LongMemoryContentEmpty"
    assert not _memory_path(tmp_path).exists()


def test_long_memory_write_rejects_non_life_agent(tmp_path: Path) -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)

    result = asyncio.run(
        tool.execute({"old_string": "", "new_string": "用户偏好"}, context=_tool_context(tmp_path, agent_name="build"))
    )

    assert result["status"] == "error"
    assert result["error_type"] == "LongMemoryAgentForbidden"


def test_long_memory_write_requires_context() -> None:
    tool = LongMemoryWriteTool(timeout_seconds=5)

    result = asyncio.run(tool.execute({"old_string": "", "new_string": "用户偏好"}, context=None))

    assert result["status"] == "error"
    assert result["error_type"] == "ToolContextMissing"


def test_agent_profiles_expose_long_memory_only_to_life() -> None:
    profiles = build_agent_profiles(max_iterations=3)

    assert "long_memory_write" in profiles["life"].allowed_tools
    assert "long_memory_write" not in profiles["build"].allowed_tools
    assert "long_memory_write" not in profiles["plan"].allowed_tools
    assert "long_memory_write" not in profiles["explore"].allowed_tools


def test_system_prompt_includes_long_memory_when_apply_to_matches(tmp_path: Path) -> None:
    memory_path = _write_memory(tmp_path, "applyTo:\n  - life", "- 用户希望回答保持简洁。\n")

    prompt = build_system_prompt(
        session=_session(tmp_path, agent_name="life"),
        workspace=_workspace(tmp_path),
        agent_state=AgentState(name="life", role="life"),
        agent_profile=build_agent_profiles(max_iterations=3)["life"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=None,
    )

    assert "常驻层：长期记忆" in prompt
    assert "用户希望回答保持简洁" in prompt
    assert "type: memory_instruction" not in prompt
    assert "applyTo:" not in prompt
    assert str(memory_path) not in prompt


def test_system_prompt_ignores_long_memory_when_apply_to_does_not_match(tmp_path: Path) -> None:
    _write_memory(tmp_path, "applyTo:\n  - life", "- 用户希望回答保持简洁。\n")

    prompt = build_system_prompt(
        session=_session(tmp_path, agent_name="build"),
        workspace=_workspace(tmp_path),
        agent_state=AgentState(name="build", role="build"),
        agent_profile=build_agent_profiles(max_iterations=3)["build"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=None,
    )

    assert "常驻层：长期记忆" not in prompt
    assert "用户希望回答保持简洁" not in prompt


def test_system_prompt_includes_long_memory_when_apply_to_is_wildcard(tmp_path: Path) -> None:
    _write_memory(tmp_path, "applyTo: '**'", "- 所有 Agent 都应读取这条记忆。\n")

    prompt = build_system_prompt(
        session=_session(tmp_path, agent_name="build"),
        workspace=_workspace(tmp_path),
        agent_state=AgentState(name="build", role="build"),
        agent_profile=build_agent_profiles(max_iterations=3)["build"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=None,
    )

    assert "常驻层：长期记忆" in prompt
    assert "所有 Agent 都应读取这条记忆" in prompt


def test_system_prompt_includes_long_memory_when_apply_to_inline_list_matches(tmp_path: Path) -> None:
    _write_memory(tmp_path, "applyTo: [build, life]", "- build 和 life 共享这条记忆。\n")

    prompt = build_system_prompt(
        session=_session(tmp_path, agent_name="build"),
        workspace=_workspace(tmp_path),
        agent_state=AgentState(name="build", role="build"),
        agent_profile=build_agent_profiles(max_iterations=3)["build"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=None,
    )

    assert "常驻层：长期记忆" in prompt
    assert "build 和 life 共享这条记忆" in prompt


def test_system_prompt_omits_long_memory_when_apply_to_missing(tmp_path: Path) -> None:
    _write_memory(tmp_path, "type: memory_instruction", "- 没有 applyTo 的记忆不注入。\n")

    prompt = build_system_prompt(
        session=_session(tmp_path, agent_name="life"),
        workspace=_workspace(tmp_path),
        agent_state=AgentState(name="life", role="life"),
        agent_profile=build_agent_profiles(max_iterations=3)["life"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=None,
    )

    assert "常驻层：长期记忆" not in prompt
    assert "没有 applyTo 的记忆不注入" not in prompt


def test_system_prompt_omits_empty_long_memory_body(tmp_path: Path) -> None:
    _write_memory(tmp_path, "applyTo:\n  - life", "   \n")

    prompt = build_system_prompt(
        session=_session(tmp_path, agent_name="life"),
        workspace=_workspace(tmp_path),
        agent_state=AgentState(name="life", role="life"),
        agent_profile=build_agent_profiles(max_iterations=3)["life"],
        llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
        skill_registry=None,
    )

    assert "常驻层：长期记忆" not in prompt


def _tool_context(tmp_path: Path, *, agent_name: str = "life") -> ToolExecutionContext:
    return ToolExecutionContext(
        session=SimpleNamespace(session_id="session_1"),
        workspace=_workspace(tmp_path),
        agent=SimpleNamespace(name=agent_name),
    )


def _workspace(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_path=tmp_path / "workspace", workspace_dir=tmp_path / "workspace_runtime", codepilot_home=tmp_path)


def _memory_path(tmp_path: Path) -> Path:
    return tmp_path / "instructions" / "memory.instruction.md"


def _write_memory(tmp_path: Path, header: str, body: str) -> Path:
    memory_path = _memory_path(tmp_path)
    memory_path.parent.mkdir()
    memory_path.write_text(f"---\n{header}\n---\n{body}", encoding="utf-8")
    return memory_path


def _session(tmp_path: Path, *, agent_name: str) -> SessionState:
    return SessionState(
        session_id="session_1",
        workspace_id="workspace_1",
        workspace_path=str(tmp_path / "workspace"),
        agent_name=agent_name,
        provider="openai",
        model="gpt-5.3-codex",
        status=SessionStatus.RUNNING,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        messages=[],
    )
