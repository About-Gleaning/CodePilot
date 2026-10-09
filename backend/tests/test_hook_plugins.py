import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot.events import EventBus
from codepilot.hooks import HookContext, HookManager, HookType, RuntimeHandles
from codepilot.hooks.plugins import AgentPluginHook, CommandPluginHook, HttpPluginHook
from codepilot.session.state import AgentState, SessionState, SessionStatus
from codepilot.utils import utc_now_iso
from codepilot.tools import BaseTool, ToolDispatcher, ToolRegistry, ToolSpec


def context(tmp_path: Path) -> HookContext:
    return HookContext(
        hook_type="loop.before",
        session=SessionState(session_id="session", workspace_id="workspace", workspace_path=str(tmp_path),
                             agent_name="test", provider="test", model="test", status=SessionStatus.RUNNING,
                             created_at=utc_now_iso(), updated_at=utc_now_iso()),
        workspace=SimpleNamespace(workspace_path=tmp_path),
        agent=AgentState(name="test"), messages=[], runtime=RuntimeHandles(event_bus=EventBus()),
    )


def command(code: str, **kwargs) -> CommandPluginHook:
    return CommandPluginHook(hook_id="command", hook_type=HookType.LOOP_BEFORE, name="命令",
                             config={"argv": [sys.executable, "-c", code]}, **kwargs)


@pytest.mark.asyncio
async def test_command_hook_consumes_input_and_returns_control(tmp_path: Path) -> None:
    hook = command('import sys,json; value=json.load(sys.stdin); print(json.dumps({"stop_loop":value["session_id"]=="session"}))')
    assert (await hook.execute(context(tmp_path))).stop_loop is True


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ['print("not-json")', 'print(\'{"unknown":true}\')', 'print("x" * 100000)'])
async def test_command_rejects_invalid_or_excessive_output(tmp_path: Path, code: str) -> None:
    with pytest.raises(ValueError):
        await command(code).execute(context(tmp_path))


@pytest.mark.asyncio
async def test_command_timeout_reaps_process(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError):
        await command("import time; time.sleep(10)", timeout_seconds=0.05).execute(context(tmp_path))


@pytest.mark.asyncio
async def test_stop_signal_cancels_command_and_reaps_process(tmp_path):
    import os

    ctx = context(tmp_path)
    ctx.runtime.stop_event = asyncio.Event()
    manager = HookManager()
    manager.register(command('import os,time,pathlib; pathlib.Path("pid").write_text(str(os.getpid())); time.sleep(30)'))
    running = asyncio.create_task(manager.run(HookType.LOOP_BEFORE, ctx))
    try:
        async with asyncio.timeout(2):
            while not (tmp_path / "pid").exists():
                await asyncio.sleep(0.01)
        pid = int((tmp_path / "pid").read_text())
        ctx.runtime.stop_event.set()
        result = await asyncio.wait_for(running, 2)
        assert result.stop_loop
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_unimplemented_agent_hook_fails_explicitly(tmp_path: Path) -> None:
    hook = AgentPluginHook(hook_id="agent", hook_type=HookType.LOOP_BEFORE, name="代理")
    with pytest.raises(ValueError, match="尚不支持"):
        await hook.execute(context(tmp_path))


@pytest.mark.asyncio
async def test_hook_selection_and_context_patch_isolation(tmp_path: Path) -> None:
    manager = HookManager()
    manager.register(command('print(\'{"context_patch":{"user_id":"other"}}\')', selectable=True))
    ctx = context(tmp_path)
    ctx.agent.hook_ids = []
    assert not (await manager.run(HookType.LOOP_BEFORE, ctx)).context_patch
    ctx.agent.hook_ids = ["command"]
    result = await manager.run(HookType.LOOP_BEFORE, ctx)
    assert result.context_patch == {"hook_data:command": {"user_id": "other"}}
    assert ctx.session.user_id == ""
    assert len(ctx.runtime.hook_instances) == 1


@pytest.mark.asyncio
async def test_http_hook_uses_fixed_target_and_validates_response(tmp_path: Path, monkeypatch) -> None:
    import httpx
    original = httpx.AsyncClient

    def handle(request):
        assert str(request.url) == "https://hook.test/check"
        assert b'"session_id": "session"' in request.content
        return httpx.Response(200, json={"fail_session": True})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    hook = HttpPluginHook(hook_id="http", hook_type=HookType.LOOP_BEFORE, name="HTTP",
                          config={"url": "https://hook.test/check"})
    assert (await hook.execute(context(tmp_path))).fail_session is True


class CountingTool(BaseTool):
    def __init__(self):
        self.calls = 0
        self.spec = ToolSpec(name="counter", description="计数", input_schema={"type": "object"}, timeout_seconds=1)

    async def execute(self, args, context=None):
        self.calls += 1
        return {"status": "ok", "output": "已执行"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [HookType.TOOL_BEFORE, HookType.TOOL_AFTER])
async def test_tool_hooks_control_execution_and_keep_facts(tmp_path: Path, stage) -> None:
    manager = HookManager()
    hook = command('print(\'{"fail_session":true}\')').model_copy(update={"hook_type": stage})
    manager.register(hook)
    registry = ToolRegistry()
    tool = CountingTool()
    registry.register(tool)
    ctx = context(tmp_path)
    ctx.agent.allowed_tools = ["counter"]
    batch = await ToolDispatcher(registry, manager).execute_tool_calls(
        session=ctx.session, workspace=ctx.workspace, agent=ctx.agent,
        tool_calls=[{"tool_name": "counter", "tool_call_id": "call", "arguments": {}}],
        runtime=ctx.runtime, config=SimpleNamespace(),
    )
    assert ctx.session.status == SessionStatus.FAILED
    assert batch.hook_result.fail_session
    assert tool.calls == (0 if stage == HookType.TOOL_BEFORE else 1)
    if stage == HookType.TOOL_AFTER:
        assert batch.tool_parts[0].state.output["output"] == "已执行"


@pytest.mark.asyncio
async def test_approval_resume_does_not_repeat_command_hook(tmp_path: Path) -> None:
    manager = HookManager()
    hook = command('from pathlib import Path; import json; p=Path("count"); p.write_text(p.read_text()+"x" if p.exists() else "x"); print(json.dumps({"requires_human_input":True,"human_request":{"approval_id":"approval","reason":"确认","created_at":"2026-09-17"}}))')
    manager.register(hook.model_copy(update={"hook_type": HookType.TOOL_BEFORE}))
    registry = ToolRegistry()
    tool = CountingTool()
    registry.register(tool)
    dispatcher = ToolDispatcher(registry, manager)
    ctx = context(tmp_path)
    ctx.agent.allowed_tools = ["counter"]
    kwargs = dict(session=ctx.session, workspace=ctx.workspace, agent=ctx.agent, runtime=ctx.runtime, config=SimpleNamespace())
    batch = await dispatcher.execute_tool_calls(**kwargs, tool_calls=[{"tool_name": "counter", "tool_call_id": "call", "arguments": {}}])
    assert batch.pending_approval is not None
    await dispatcher.resume_tool_batch(**kwargs, resume_batch=batch.resume_batch)
    assert tool.calls == 1
    assert (tmp_path / "count").read_text() == "x"
