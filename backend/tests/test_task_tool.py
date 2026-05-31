from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codepilot.events import EventBus
from codepilot.hooks import RuntimeHandles
from codepilot.session.agents import AgentProfile
from codepilot.session.message import Message, TextPart, build_assistant_message_info
from codepilot.session.state import SessionState, SessionStatus
from codepilot.tools import TaskTool, ToolExecutionContext
from codepilot.utils import utc_now_iso, utc_now_millis


class FakeSubagentLoop:
    def __init__(self, *, status: SessionStatus = SessionStatus.COMPLETED, subagent_error: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = status
        self.subagent_error = subagent_error

    async def run_subagent(self, **kwargs: Any) -> SessionState:
        self.calls.append(kwargs)
        parent_session = kwargs["parent_session"]
        profile = kwargs["agent_profile"]
        child = SessionState(
            session_id=parent_session.session_id,
            workspace_id=parent_session.workspace_id,
            workspace_path=parent_session.workspace_path,
            agent_name=profile.name,
            provider=parent_session.provider,
            model=parent_session.model,
            status=self.status,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            metadata={"agent_context_id": "ctx_child"},
        )
        if self.subagent_error:
            child.metadata["subagent_error"] = self.subagent_error
        child.messages.append(
            Message(
                info=build_assistant_message_info(
                    message_id="msg_child",
                    session_id=parent_session.session_id,
                    created_at_ms=utc_now_millis(),
                    parent_id="msg_task",
                    agent=profile.name,
                    agent_kind=profile.kind,
                    context_id="ctx_child",
                    parent_call_id=kwargs["parent_call_id"],
                    provider_id=parent_session.provider,
                    model_id=parent_session.model,
                    cwd=str(Path.cwd()),
                    root=parent_session.workspace_path,
                ),
                parts=[TextPart(text="探查完成")],
            )
        )
        return child


def build_context(tmp_path: Path, *, agent: Any | None = None, stop_event: Any | None = None) -> ToolExecutionContext:
    session = SessionState(
        session_id="session_1",
        workspace_id="ws_1",
        workspace_path=str(tmp_path),
        agent_name="build",
        provider="openai",
        model="gpt-test",
        status=SessionStatus.RUNNING,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    return ToolExecutionContext(
        session=session,
        workspace=SimpleNamespace(workspace_path=tmp_path, workspace_dir=tmp_path / ".codepilot"),
        agent=agent or SimpleNamespace(name="build", kind="agent", can_call_subagent=True),
        runtime=RuntimeHandles(event_bus=EventBus()),
        config=SimpleNamespace(),
        tool_call_id="call_task_1",
        stop_event=stop_event,
    )


def build_tool(loop: FakeSubagentLoop) -> TaskTool:
    return TaskTool(
        agent_loop=loop,
        agent_profiles={
            "build": AgentProfile(name="build", system_prompt="", kind="agent"),
            "explore": AgentProfile(name="explore", system_prompt="", kind="subagent"),
        },
        timeout_seconds=5,
    )


def run_tool(tool: TaskTool, args: dict[str, object], context: ToolExecutionContext) -> dict[str, Any]:
    return asyncio.run(tool.execute(args, context=context))


def test_task_tool_runs_target_subagent_with_parent_call_id(tmp_path: Path) -> None:
    loop = FakeSubagentLoop()
    tool = build_tool(loop)

    result = run_tool(tool, {"agent": "explore", "task": "读取 README"}, build_context(tmp_path))

    assert result["status"] == "ok"
    assert result["agent"] == "explore"
    assert result["context_id"] == "ctx_child"
    assert result["parent_call_id"] == "call_task_1"
    assert result["summary"] == "探查完成"
    assert loop.calls[0]["task"] == "读取 README"
    assert loop.calls[0]["parent_call_id"] == "call_task_1"


def test_task_tool_passes_parent_stop_event_to_subagent(tmp_path: Path) -> None:
    loop = FakeSubagentLoop()
    tool = build_tool(loop)
    stop_event = asyncio.Event()

    result = run_tool(tool, {"agent": "explore", "task": "读取 README"}, build_context(tmp_path, stop_event=stop_event))

    assert result["status"] == "ok"
    assert loop.calls[0]["stop_event"] is stop_event


def test_task_tool_returns_error_when_subagent_fails(tmp_path: Path) -> None:
    loop = FakeSubagentLoop(status=SessionStatus.FAILED, subagent_error="subagent 不支持人工审批")
    tool = build_tool(loop)

    result = run_tool(tool, {"agent": "explore", "task": "需要审批"}, build_context(tmp_path))

    assert result["status"] == "error"
    assert result["error_type"] == "TaskSubagentFailed"
    assert "subagent 不支持人工审批" in result["error_message"]


def test_task_tool_rejects_subagent_caller(tmp_path: Path) -> None:
    loop = FakeSubagentLoop()
    tool = build_tool(loop)
    context = build_context(tmp_path, agent=SimpleNamespace(name="explore", kind="subagent", can_call_subagent=False))

    result = run_tool(tool, {"agent": "explore", "task": "读取 README"}, context)

    assert result["status"] == "error"
    assert result["error_type"] == "TaskAgentForbidden"
    assert loop.calls == []


def test_task_tool_rejects_non_subagent_target(tmp_path: Path) -> None:
    loop = FakeSubagentLoop()
    tool = build_tool(loop)

    result = run_tool(tool, {"agent": "build", "task": "读取 README"}, build_context(tmp_path))

    assert result["status"] == "error"
    assert result["error_type"] == "TaskTargetForbidden"
    assert loop.calls == []


def test_task_tool_description_includes_available_subagents() -> None:
    tool = TaskTool(
        agent_loop=FakeSubagentLoop(),
        agent_profiles={
            "build": AgentProfile(name="build", description="主开发 Agent", system_prompt="", kind="agent"),
            "explore": AgentProfile(
                name="explore",
                description="只读文件搜索、代码定位和上下文探查专家。",
                system_prompt="",
                kind="subagent",
            ),
        },
        timeout_seconds=5,
    )

    description = tool.get_llm_description()

    assert "{{available_subagents}}" not in description
    assert "可用 subagent" in description
    assert "- explore: 只读文件搜索、代码定位和上下文探查专家。" in description
    assert "build: 主开发 Agent" not in description


def test_task_tool_description_reports_empty_subagent_list() -> None:
    tool = TaskTool(
        agent_loop=FakeSubagentLoop(),
        agent_profiles={
            "build": AgentProfile(name="build", description="主开发 Agent", system_prompt="", kind="agent"),
        },
        timeout_seconds=5,
    )

    description = tool.get_llm_description()

    assert "{{available_subagents}}" not in description
    assert "当前没有可用 subagent。" in description
