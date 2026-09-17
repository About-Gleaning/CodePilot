from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codepilot.events import EventBus
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.gateway import UploadedAttachmentInput
from codepilot.hooks import HookManager
from codepilot.hooks import RuntimeHandles
from codepilot.memory import UserPartitionedSessionMemory
from codepilot.session.agent_runtime import AgentRuntimeManager, InProcessAgentRuntimeBackend, SessionRunnerFactory, RuntimeConflict
from codepilot.session.session_runner import SessionRunner
from codepilot.session.session import AgentLoop
from codepilot.session.state import RunRef, SessionStatus
from codepilot.session.agents import AgentProfile
from test_multi_user_isolation import _SharedProfiles
from test_session_hooks import build_settings, StubLiteLLMClient, StubToolRegistry, StubToolDispatcher, ContinueToolDispatcher


class ControlledClient(StubLiteLLMClient):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_chat(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().stream_chat(**kwargs)


async def setup_runtime(tmp_path: Path, *, max_iterations: int = 4, client=None):
    owner = str(uuid4())
    settings = build_settings()
    bus = EventBus()
    memory = UserPartitionedSessionMemory(tmp_path)
    bus.subscribe_domain(memory.handle_domain_event, critical=True)
    profiles = _SharedProfiles()
    profiles.profile.max_iterations = max_iterations
    profiles.profile.default_provider = "openai"
    profiles.profile.default_model = "gpt-5.3-codex"
    client = client or ControlledClient()
    hooks = HookManager()
    loop = AgentLoop(llm_client=client, tool_registry=StubToolRegistry(), tool_dispatcher=StubToolDispatcher(), hook_manager=hooks)

    async def no_title(*args):
        pass

    def factory(user_id):
        workspace = SimpleNamespace(workspace_id="test", workspace_path=tmp_path, workspace_dir=tmp_path / "users" / user_id,
                                    shared_runtime_dir=tmp_path, user_id=user_id)
        return SessionRunner(workspace, settings, bus, hooks, loop, {}, title_service=SimpleNamespace(generate_for_session=no_title))

    backend = InProcessAgentRuntimeBackend(SessionRunnerFactory(factory))
    manager = AgentRuntimeManager(workspace=SimpleNamespace(workspace_dir=tmp_path), config=settings, event_bus=bus,
                                  session_memory=memory, profile_provider=profiles, backend=backend)
    bus.subscribe_domain(manager.handle_domain_event)
    await manager.recover({owner})
    await manager.start_agent(owner, "shared-agent")
    return manager, client, memory, owner, bus


def request(text="初始要求", **kwargs):
    return GatewayInput(type=GatewayInputType.USER_MESSAGE, content=text, agent_name="runtime", **kwargs)


async def send(manager, owner, req, client_id, session_id=None, expected=None):
    return await manager.submit_message(owner, "shared-agent", req, session_id, client_id, expected)


async def finish(manager, ref):
    await asyncio.wait_for(manager._terminal_events[(ref.user_id, ref.agent_id, ref.session_id, ref.run_id)].wait(), 3)


@pytest.mark.asyncio
async def test_append_is_durable_idempotent_and_runs_before_normal_finish(tmp_path):
    manager, client, memory, owner, _ = await setup_runtime(tmp_path)
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    results = await asyncio.gather(*(send(manager, owner, request("追加要求"), "append", ref.session_id, ref.run_id) for _ in range(10)))
    assert {result["ref"]["run_id"] for result in results} == {ref.run_id}
    assert len({result["submission"]["message_id"] for result in results}) == 1
    replay = await memory.replay(owner, ref.session_id)
    assert len(replay["submissions"]) == 1
    assert replay["submissions"][0]["status"] == "pending"
    assert len(replay["messages"]) == 1
    assert manager._active_run_total == 1
    client.release.set()
    await finish(manager, ref)
    assert client.calls == 2
    inputs = [[item.text_content() for item in batch if hasattr(item, "text_content")] for batch in client.provider_message_calls]
    assert inputs[0] == ["初始要求"]
    assert inputs[1] == ["初始要求", "done", "追加要求"]
    replay = await memory.replay(owner, ref.session_id)
    assert replay["submissions"][0]["status"] == "included"
    retried = await send(manager, owner, request("追加要求"), "append", ref.session_id, ref.run_id)
    assert retried["ref"]["run_id"] == ref.run_id
    assert manager._active_run_total == 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_limit_preserves_pending_and_manual_next_run_keeps_order(tmp_path):
    manager, client, memory, owner, _ = await setup_runtime(tmp_path, max_iterations=1)
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    await send(manager, owner, request("旧追加"), "append", ref.session_id, ref.run_id)
    client.release.set()
    await finish(manager, ref)
    assert manager.get_run_state(ref).error_code == "max_iterations"
    assert manager.get_run_state(ref).status.value == "CANCELLED"
    replay = await memory.replay(owner, ref.session_id)
    assert replay["submissions"][0]["status"] == "pending"
    second = await send(manager, owner, request("人工继续"), "second", ref.session_id)
    await finish(manager, RunRef.model_validate(second["ref"]))
    texts = [item.text_content() for item in client.provider_message_calls[-1] if hasattr(item, "text_content")]
    assert texts.index("旧追加") < texts.index("人工继续")
    replay = await memory.replay(owner, ref.session_id)
    texts = [item["parts"][0].get("text") for item in replay["messages"]]
    assert texts.index("旧追加") < texts.index("人工继续")
    assert replay["submissions"][0]["status"] == "included"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_recovery_retry_does_not_start_run(tmp_path):
    manager, client, memory, owner, _ = await setup_runtime(tmp_path)
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    await send(manager, owner, request("保留"), "append", ref.session_id, ref.run_id)
    await manager.cancel_run(ref)
    await manager.shutdown()
    await manager.recover({owner})
    manager._submissions.clear()
    manager._submission_users_loaded.clear()
    retry = await send(manager, owner, request("保留"), "append", ref.session_id, ref.run_id)
    assert retry["ref"]["run_id"] == ref.run_id
    assert manager._active_run_total == 0
    with pytest.raises(RuntimeConflict, match="原执行"):
        await send(manager, owner, request("迟到"), "late", ref.session_id, ref.run_id)
    with pytest.raises(RuntimeConflict, match="不同请求"):
        await send(manager, owner, request("篡改"), "append", ref.session_id, ref.run_id)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_waiting_capacity_and_explicit_model_guards(tmp_path):
    manager, client, _, owner, _ = await setup_runtime(tmp_path)
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    manager._max_active_runs = 1
    runner = manager._sessions[(owner, ref.agent_id, ref.session_id)].execution.runner
    await runner._inbox.wait_for_human(runner._session)
    with pytest.raises(RuntimeConflict):
        await send(manager, owner, request("等待时拒绝"), "waiting", ref.session_id)
    runner._session.status = SessionStatus.RUNNING
    with pytest.raises(RuntimeConflict, match="模型"):
        await send(manager, owner, request("改模型", provider="openai", model="other"), "model", ref.session_id)
    for index in range(20):
        await send(manager, owner, request(str(index)), f"append_{index}", ref.session_id)
    with pytest.raises(RuntimeConflict, match="20"):
        await send(manager, owner, request("溢出"), "overflow", ref.session_id)
    client.release.set()
    await finish(manager, ref)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_submission_persistence_failure_is_not_accepted(tmp_path):
    manager, client, memory, owner, bus = await setup_runtime(tmp_path)
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    original = bus._domain_persist_subscribers[0]

    async def fail_submission(event):
        if event.event_type.value == "message_submission":
            raise OSError("模拟落盘失败")
        await original(event)

    bus._domain_persist_subscribers[0] = fail_submission
    with pytest.raises(OSError):
        await send(manager, owner, request("未收到"), "failed", ref.session_id)
    assert not (await memory.replay(owner, ref.session_id))["submissions"]
    bus._domain_persist_subscribers[0] = original
    client.release.set()
    await finish(manager, ref)
    assert client.calls == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_messages_received_during_preparation_wait_for_following_decision(tmp_path):
    manager, client, _, owner, _ = await setup_runtime(tmp_path)
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    runner = manager._sessions[(owner, ref.agent_id, ref.session_id)].execution.runner
    compressor = runner._agent_loop._turn_executor.context_compressor
    prepare_entered, prepare_release = asyncio.Event(), asyncio.Event()
    original = compressor.compress

    async def blocked_compression(**kwargs):
        if kwargs.get("protected_message_ids"):
            prepare_entered.set()
            await prepare_release.wait()
        return await original(**kwargs)

    compressor.compress = blocked_compression
    await send(manager, owner, request("第一批"), "one", ref.session_id)
    client.release.set()
    await asyncio.wait_for(prepare_entered.wait(), 3)
    await send(manager, owner, request("准备期间第二批"), "two", ref.session_id)
    prepare_release.set()
    await finish(manager, ref)
    inputs = [[item.text_content() for item in batch if hasattr(item, "text_content")] for batch in client.provider_message_calls]
    assert len(inputs) == 3
    assert "第一批" in inputs[1] and "准备期间第二批" not in inputs[1]
    assert inputs[2].count("准备期间第二批") == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_attachment_and_user_isolation(tmp_path):
    manager, client, memory, owner, bus = await setup_runtime(tmp_path)
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    other = str(uuid4())
    await manager.start_agent(other, ref.agent_id)
    with pytest.raises(RuntimeConflict):
        await send(manager, other, request("越权"), "foreign", ref.session_id, ref.run_id)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
    req = request("看图", attachments=[UploadedAttachmentInput(filename="../photo.png", mime="image/png", data_base64=encoded)])
    await send(manager, owner, req, "image", ref.session_id)
    replay = await memory.replay(owner, ref.session_id)
    part = replay["submissions"][0]["message"]["parts"][1]
    assert Path(part["source"]["value"]).read_bytes().startswith(b"\x89PNG")
    assert part["filename"] == "photo.png"
    assert encoded not in str(replay)
    from codepilot.session.inbox import public_submission
    assert "source" not in public_submission(replay["submissions"][0])["message"]["parts"][1]
    assert "source" in replay["submissions"][0]["message"]["parts"][1]
    client.release.set()
    await finish(manager, ref)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_final_allowed_iteration_is_successful_without_pending(tmp_path):
    manager, client, _, owner, _ = await setup_runtime(tmp_path, max_iterations=1)
    client.release.set()
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await finish(manager, ref)
    assert manager.get_run_state(ref).status.value == "COMPLETED"
    assert manager.get_run_state(ref).error_code is None
    await manager.shutdown()


def test_worker_preserves_cancelled_status():
    from codepilot.scheduler.worker import _run_status_from_session
    assert _run_status_from_session(SessionStatus.CANCELLED).value == "cancelled"


@pytest.mark.asyncio
async def test_preparation_failure_keeps_input_pending_without_duplicate_on_continue(tmp_path):
    manager, client, memory, owner, _ = await setup_runtime(tmp_path)
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    runner = manager._sessions[(owner, ref.agent_id, ref.session_id)].execution.runner
    compressor = runner._agent_loop._turn_executor.context_compressor
    original = compressor.compress

    async def fail_preparation(**kwargs):
        raise RuntimeError("模拟准备阻断")

    compressor.compress = fail_preparation
    await send(manager, owner, request("准备被阻断的新要求"), "append", ref.session_id)
    client.release.set()
    await finish(manager, ref)
    assert manager.get_run_state(ref).status.value == "FAILED"
    assert (await memory.replay(owner, ref.session_id))["submissions"][0]["status"] == "pending"
    compressor.compress = original
    resumed = await send(manager, owner, request("人工继续"), "resume", ref.session_id)
    await finish(manager, RunRef.model_validate(resumed["ref"]))
    texts = [item.text_content() for item in client.provider_message_calls[-1] if hasattr(item, "text_content")]
    assert texts.count("准备被阻断的新要求") == 1
    assert texts.index("准备被阻断的新要求") < texts.index("人工继续")
    await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent", [False, True])
async def test_append_does_not_interrupt_current_tool_or_child(tmp_path, subagent):
    manager, client, _, owner, _ = await setup_runtime(tmp_path)
    original_stream = client.stream_chat
    entered, release = asyncio.Event(), asyncio.Event()
    child_inputs = []

    async def stream(**kwargs):
        if kwargs["session"].metadata.get("agent_kind") == "subagent":
            child_inputs.extend(kwargs["provider_messages"])
            entered.set()
            await release.wait()
            return SimpleNamespace(text="子执行完成", reasoning="", tool_calls=[])
        result = await original_stream(**kwargs)
        if client.calls == 1:
            result.tool_calls = [{"tool_call_id": "call_one", "tool_name": "continue_tool", "arguments": {}}]
        return result

    client.stream_chat = stream
    started = await send(manager, owner, request(), "first")
    ref = RunRef.model_validate(started["ref"])
    await client.entered.wait()
    runner = manager._sessions[(owner, ref.agent_id, ref.session_id)].execution.runner

    class BlockingDispatcher(ContinueToolDispatcher):
        async def execute_tool_calls(self, **kwargs):
            if subagent:
                await runner._agent_loop.run_subagent(
                    parent_session=kwargs["session"], workspace=kwargs["workspace"],
                    agent_profile=AgentProfile(name="explore", kind="subagent", system_prompt="探查", max_iterations=1),
                    task="子执行要求", parent_call_id="call_one", config=kwargs["config"],
                    runtime=RuntimeHandles(event_bus=kwargs["runtime"].event_bus, run_ref=ref), stop_event=kwargs["stop_event"],
                )
            else:
                entered.set()
                await release.wait()
            return await super().execute_tool_calls(**kwargs)

    runner._agent_loop._turn_executor.tool_dispatcher = BlockingDispatcher()
    client.release.set()
    await asyncio.wait_for(entered.wait(), 3)
    await send(manager, owner, request("仅主决策追加"), "append", ref.session_id)
    assert not release.is_set()
    assert "仅主决策追加" not in str(child_inputs)
    release.set()
    await finish(manager, ref)
    inputs = [item.text_content() for item in client.provider_message_calls[-1] if hasattr(item, "text_content")]
    assert inputs[-1] == "仅主决策追加"
    assert client.calls == 2
    await manager.shutdown()


@pytest.mark.asyncio
async def test_existing_http_endpoint_routes_both_submission_modes(tmp_path):
    from fastapi import APIRouter, FastAPI
    from httpx import ASGITransport, AsyncClient
    from codepilot.api.session_routes import register_session_routes

    manager, client, _, owner, _ = await setup_runtime(tmp_path)
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(req, call_next):
        req.state.principal = SimpleNamespace(user_id=owner)
        return await call_next(req)

    router = APIRouter(prefix="/api")
    register_session_routes(router, SimpleNamespace(agent_runtime=manager))
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as http:
        first = await http.post("/api/agents/shared-agent/runs", json={"content": "开始", "client_request_id": "first"})
        assert first.status_code == 200
        data = first.json()
        assert data["submission"]["mode"] == "started"
        await client.entered.wait()
        second = await http.post("/api/agents/shared-agent/runs", json={
            "content": "追加", "client_request_id": "append", "session_id": data["ref"]["session_id"],
            "expected_run_id": data["ref"]["run_id"],
        })
        assert second.status_code == 200
        assert second.json()["submission"]["mode"] == "appended"
        assert second.json()["ref"] == data["ref"]
    client.release.set()
    await finish(manager, RunRef.model_validate(data["ref"]))
    await manager.shutdown()
