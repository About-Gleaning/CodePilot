from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codepilot.events import EventBus
from codepilot.hooks import HookManager, RuntimeHandles
from codepilot.tools import BaseTool, ToolDispatcher, ToolExecutionContext, ToolRegistry, ToolSpec


class DummyTool(BaseTool):
    def __init__(self, name: str, *, can_parallel: bool) -> None:
        self.spec = ToolSpec(
            name=name,
            description=f"{name} dummy",
            input_schema={"type": "object", "properties": {}},
            can_parallel=can_parallel,
            timeout_seconds=1,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"status": "ok", "tool_name": self.spec.name, "output": str(args.get("value", ""))}


def run_dispatcher(tmp_path: Path, tool_calls: list[dict[str, Any]], *, allowed_tools: list[str] | None = None):
    async def _run():
        registry = ToolRegistry()
        registry.register(DummyTool("parallel_a", can_parallel=True))
        registry.register(DummyTool("parallel_b", can_parallel=True))
        registry.register(DummyTool("serial", can_parallel=False))
        dispatcher = ToolDispatcher(registry, HookManager())
        return await dispatcher.execute_tool_calls(
            session=SimpleNamespace(session_id="session_1", messages=[]),
            workspace=SimpleNamespace(workspace_path=tmp_path),
            agent=SimpleNamespace(name="build", allowed_tools=allowed_tools or []),
            tool_calls=tool_calls,
            runtime=RuntimeHandles(event_bus=EventBus()),
            config=SimpleNamespace(),
        )

    return asyncio.run(_run())


def test_parallel_tool_batch_writes_execution_group_metadata(tmp_path: Path) -> None:
    batch = run_dispatcher(
        tmp_path,
        [
            {"tool_name": "parallel_a", "arguments": {"value": "a"}},
            {"tool_name": "parallel_b", "arguments": {"value": "b"}},
        ],
    )

    group_ids = [part.metadata.get("execution_group") for part in batch.tool_parts]

    assert len(batch.tool_parts) == 2
    assert group_ids[0]
    assert group_ids[0] == group_ids[1]


def test_serial_tool_does_not_write_execution_group_metadata(tmp_path: Path) -> None:
    batch = run_dispatcher(
        tmp_path,
        [
            {"tool_name": "parallel_a", "arguments": {"value": "a"}},
            {"tool_name": "serial", "arguments": {"value": "serial"}},
            {"tool_name": "parallel_b", "arguments": {"value": "b"}},
        ],
    )

    assert [part.metadata.get("execution_group") for part in batch.tool_parts] == [None, None, None]


def test_dispatcher_rejects_tool_not_authorized_for_agent(tmp_path: Path) -> None:
    batch = run_dispatcher(
        tmp_path,
        [{"tool_name": "serial", "arguments": {"value": "forbidden"}}],
    )

    result = batch.tool_parts[0].state.output

    assert result["status"] == "error"
    assert result["error_type"] == "ToolAgentForbidden"


def test_dispatcher_executes_tool_authorized_for_agent(tmp_path: Path) -> None:
    batch = run_dispatcher(
        tmp_path,
        [{"tool_name": "serial", "arguments": {"value": "allowed"}}],
        allowed_tools=["serial"],
    )

    assert batch.tool_parts[0].state.output["output"] == "allowed"
