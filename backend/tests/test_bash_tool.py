from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codepilot.config.settings import BashToolSettings
from codepilot.events import EventBus
from codepilot.hooks import HookManager, RuntimeHandles
from codepilot.session.agents import AgentProfile, build_agent_profiles
from codepilot.tools import BashTool, ToolDispatcher, ToolExecutionContext, ToolRegistry


def build_context(tmp_path: Path, *, agent_name: str = "build") -> ToolExecutionContext:
    return ToolExecutionContext(
        session=SimpleNamespace(session_id="session_1"),
        workspace=SimpleNamespace(workspace_path=tmp_path, workspace_dir=tmp_path / ".codepilot"),
        agent=SimpleNamespace(name=agent_name),
    )


def run_tool(tool: BashTool, args: dict[str, object], context: ToolExecutionContext) -> dict[str, Any]:
    return asyncio.run(tool.execute(args, context=context))


def test_approval_mode_all_requires_approval(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="all"), timeout_seconds=5)
    context = build_context(tmp_path)

    result = asyncio.run(tool.preflight({"command": "pwd"}, context))

    assert result.status == "requires_approval"


def test_approval_mode_allowlist_allows_matched_and_requires_unmatched(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="allowlist", allowlist=[["pwd"]]), timeout_seconds=5)
    context = build_context(tmp_path)

    allowed = asyncio.run(tool.preflight({"command": "pwd"}, context))
    pending = asyncio.run(tool.preflight({"command": "ls"}, context))

    assert allowed.status == "allow"
    assert pending.status == "requires_approval"


def test_approval_mode_none_allows_normal_command(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="none"), timeout_seconds=5)
    context = build_context(tmp_path)

    result = asyncio.run(tool.preflight({"command": "pwd"}, context))

    assert result.status == "allow"


def test_blacklist_has_highest_priority_for_every_mode(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    for mode in ["all", "allowlist", "none"]:
        tool = BashTool(
            settings=BashToolSettings(approval_mode=mode, allowlist=[["rm"]], blacklist=[["rm"]]),
            timeout_seconds=5,
        )

        result = asyncio.run(tool.preflight({"command": "rm -rf something"}, context))

        assert result.status == "blocked"


def test_blacklist_matches_later_shell_segments(tmp_path: Path) -> None:
    tool = BashTool(
        settings=BashToolSettings(approval_mode="none", blacklist=[["rm"]]),
        timeout_seconds=5,
    )

    result = asyncio.run(tool.preflight({"command": "pwd; rm -rf something"}, build_context(tmp_path)))

    assert result.status == "blocked"


def test_allowlist_requires_every_shell_segment_to_match(tmp_path: Path) -> None:
    tool = BashTool(
        settings=BashToolSettings(approval_mode="allowlist", allowlist=[["pwd"]]),
        timeout_seconds=5,
    )

    result = asyncio.run(tool.preflight({"command": "pwd; ls"}, build_context(tmp_path)))

    assert result.status == "requires_approval"


def test_cwd_not_found_returns_error(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="none"), timeout_seconds=5)

    result = run_tool(tool, {"command": "pwd", "cwd": "missing"}, build_context(tmp_path))

    assert result["status"] == "error"
    assert result["error_type"] == "BashCwdNotFound"


def test_cwd_not_found_does_not_request_approval(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="all"), timeout_seconds=5)

    result = asyncio.run(tool.preflight({"command": "pwd", "cwd": "missing"}, build_context(tmp_path)))

    assert result.status == "blocked"
    assert result.result is not None
    assert result.result["status"] == "error"
    assert result.result["error_type"] == "BashCwdNotFound"


def test_cwd_outside_workspace_is_rejected(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="none"), timeout_seconds=5)

    result = run_tool(tool, {"command": "pwd", "cwd": str(tmp_path.parent)}, build_context(tmp_path))

    assert result["status"] == "error"
    assert result["error_type"] == "BashCwdForbidden"


def test_command_timeout_returns_structured_result(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="none"), timeout_seconds=5)

    result = run_tool(tool, {"command": "sleep 2", "timeout_seconds": 1}, build_context(tmp_path))

    assert result["status"] == "error"
    assert result["timed_out"] is True
    assert result["error_type"] == "BashCommandTimedOut"


def test_non_zero_exit_code_returns_structured_result(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="none"), timeout_seconds=5)

    result = run_tool(tool, {"command": "echo before; echo err >&2; exit 7"}, build_context(tmp_path))

    assert result["status"] == "ok"
    assert result["exit_code"] == 7
    assert result["stdout"] == "before\n"
    assert result["stderr"] == "err\n"


def test_stdout_and_stderr_are_truncated(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="none", max_output_chars=4), timeout_seconds=5)

    result = run_tool(tool, {"command": "printf abcdef; printf ghijkl >&2"}, build_context(tmp_path))

    assert result["stdout"] == "abcd"
    assert result["stderr"] == "ghij"
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True


def test_readonly_agent_allows_readonly_pipeline(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    tool = BashTool(settings=BashToolSettings(), timeout_seconds=5)

    result = run_tool(tool, {"command": "cat sample.txt | head -1"}, build_context(tmp_path, agent_name="plan"))

    assert result["status"] == "ok"
    assert result["stdout"] == "alpha\n"


def test_readonly_agent_rejects_write_command_without_approval(tmp_path: Path) -> None:
    tool = BashTool(settings=BashToolSettings(approval_mode="none"), timeout_seconds=5)

    result = asyncio.run(tool.preflight({"command": "touch created.txt"}, build_context(tmp_path, agent_name="plan")))

    assert result.status == "blocked"


def test_readonly_agent_allows_redirect_only_to_scratch(tmp_path: Path) -> None:
    context = build_context(tmp_path, agent_name="plan")
    scratch_target = tmp_path / ".codepilot" / "bash" / "session_1" / "out.txt"
    source_target = tmp_path / "source.txt"
    tool = BashTool(settings=BashToolSettings(), timeout_seconds=5)

    allowed = run_tool(tool, {"command": f"pwd > {scratch_target}"}, context)
    blocked = asyncio.run(tool.preflight({"command": f"pwd > {source_target}"}, context))

    assert allowed["status"] == "ok"
    assert scratch_target.read_text(encoding="utf-8") == f"{tmp_path}\n"
    assert blocked.status == "blocked"


def test_tool_registry_returns_agent_specific_bash_description() -> None:
    registry = ToolRegistry()
    registry.register(BashTool(settings=BashToolSettings(), timeout_seconds=5))

    build_schema = registry.get_llm_tool_schemas(["bash_tool"], agent_profile=AgentProfile(name="build", system_prompt=""))
    plan_schema = registry.get_llm_tool_schemas(["bash_tool"], agent_profile=AgentProfile(name="plan", system_prompt=""))

    assert "执行命令行命令" in str(build_schema[0]["function"]["description"])
    assert "只读探查命令" in str(plan_schema[0]["function"]["description"])


def test_bash_tool_schema_does_not_expose_env_parameter() -> None:
    registry = ToolRegistry()
    registry.register(BashTool(settings=BashToolSettings(), timeout_seconds=5))

    schema = registry.get_llm_tool_schemas(["bash_tool"])[0]
    properties = schema["function"]["parameters"]["properties"]  # type: ignore[index]

    assert "env" not in properties


def test_agent_profiles_all_receive_bash_tool() -> None:
    profiles = build_agent_profiles(max_iterations=3)

    assert "bash_tool" in profiles["build"].allowed_tools
    assert "bash_tool" in profiles["plan"].allowed_tools
    assert "bash_tool" in profiles["explore"].allowed_tools


def test_dispatcher_pauses_and_resumes_approved_bash_command(tmp_path: Path) -> None:
    async def run_case() -> tuple[Any, Any]:
        registry = ToolRegistry()
        registry.register(BashTool(settings=BashToolSettings(approval_mode="all"), timeout_seconds=5))
        dispatcher = ToolDispatcher(registry, HookManager())
        session = SimpleNamespace(session_id="session_1", messages=[])
        workspace = SimpleNamespace(workspace_path=tmp_path, workspace_dir=tmp_path / ".codepilot")
        agent = SimpleNamespace(name="build")
        runtime = RuntimeHandles(event_bus=EventBus())
        item = {"tool_call_id": "call_1", "tool_name": "bash_tool", "arguments": {"command": "echo approved"}}

        pending = await dispatcher.execute_tool_calls(
            session=session,
            workspace=workspace,
            agent=agent,
            tool_calls=[item],
            runtime=runtime,
            config=SimpleNamespace(),
        )
        assert pending.pending_approval is not None
        approved_part = await dispatcher.execute_approved_tool_call(
            session=session,
            workspace=workspace,
            agent=agent,
            item=pending.pending_approval.resume_item or {},
            runtime=runtime,
            config=SimpleNamespace(),
        )
        return pending, approved_part

    pending, approved_part = asyncio.run(run_case())

    assert pending.pending_approval is not None
    assert pending.tool_parts == []
    assert isinstance(pending.pending_approval.request.action, dict)
    assert approved_part.state.output["stdout"] == "approved\n"


def test_dispatcher_returns_blocked_preflight_result(tmp_path: Path) -> None:
    async def run_case() -> Any:
        registry = ToolRegistry()
        registry.register(BashTool(settings=BashToolSettings(approval_mode="none", blacklist=[["rm"]]), timeout_seconds=5))
        dispatcher = ToolDispatcher(registry, HookManager())
        session = SimpleNamespace(session_id="session_1", messages=[])
        workspace = SimpleNamespace(workspace_path=tmp_path, workspace_dir=tmp_path / ".codepilot")
        agent = SimpleNamespace(name="build")
        runtime = RuntimeHandles(event_bus=EventBus())

        return await dispatcher.execute_tool_calls(
            session=session,
            workspace=workspace,
            agent=agent,
            tool_calls=[{"tool_call_id": "call_1", "tool_name": "bash_tool", "arguments": {"command": "rm -rf x"}}],
            runtime=runtime,
            config=SimpleNamespace(),
        )

    batch = asyncio.run(run_case())

    assert batch.pending_approval is None
    assert batch.pending_question is None
    assert len(batch.tool_parts) == 1
    part = batch.tool_parts[0]
    assert part.call_id == "call_1"
    assert part.tool == "bash_tool"
    assert part.state.status == "completed"
    assert part.state.input == {"command": "rm -rf x"}
    assert part.state.output["status"] == "blocked"
    assert part.state.output["error_type"] == "BashCommandBlocked"
