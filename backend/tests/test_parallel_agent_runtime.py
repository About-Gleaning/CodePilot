from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codepilot.events import EventBus, RunEventScope, StreamEvent
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.memory import JsonlSessionMemory
from codepilot.session.agent_runtime import (
    AgentRuntimeManager,
    CancellationResult,
    RunExecutionHandle,
    RunExecutionResult,
    RuntimeConflict,
    SessionExecutionHandle,
)
from codepilot.session.agents import AgentProfile
from codepilot.session.state import RunRef, RunStatus
from codepilot.tools.workspace_lease import WorkspaceWriteBusy, WorkspaceWriteLeaseManager


class _Profiles:
    def __init__(self) -> None:
        self.profile = AgentProfile(
            agent_id="agent-1",
            revision_id="revision-1",
            name="agent_1",
            description="并发测试",
            system_prompt="测试",
            allowed_tools=["read_file"],
        )

    def get_active_profile_snapshot(self, agent_id: str) -> AgentProfile:
        return self.profile.model_copy(deep=True)

    def get_record_snapshot(self, agent_id: str) -> dict[str, Any]:
        return {"profile": self.profile.model_copy(deep=True)}

    def list_active_profile_snapshots(self) -> list[AgentProfile]:
        return [self.profile.model_copy(deep=True)]


class _Backend:
    def __init__(self) -> None:
        self.start_count = 0
        self.finish = asyncio.Event()

    async def start_agent(self, agent_id: str) -> None:
        return None

    async def stop_agent(self, agent_id: str) -> None:
        return None

    async def load_session(
        self,
        agent_id: str,
        session_id: str,
        replay: dict[str, Any] | None,
        profile_snapshot: AgentProfile,
    ) -> SessionExecutionHandle:
        await asyncio.sleep(0.01)
        return SessionExecutionHandle(agent_id, session_id, SimpleNamespace())

    async def start_run(
        self,
        session_handle: SessionExecutionHandle,
        run_ref: RunRef,
        request: GatewayInput,
        profile_snapshot: AgentProfile,
        event_scope: RunEventScope,
    ) -> RunExecutionHandle:
        self.start_count += 1
        return RunExecutionHandle(run_ref, SimpleNamespace(), event_scope)

    def get_session_snapshot(self, session_handle: SessionExecutionHandle) -> dict[str, Any]:
        return {"session_id": session_handle.session_id, "status": "RUNNING"}

    async def wait_run(self, run_handle: RunExecutionHandle) -> RunExecutionResult:
        await self.finish.wait()
        return RunExecutionResult(status=RunStatus.COMPLETED)

    async def cancel_run(
        self, session_handle: SessionExecutionHandle, run_ref: RunRef
    ) -> CancellationResult:
        self.finish.set()
        return CancellationResult(confirmed=True)

    async def reply_interaction(self, *args: Any) -> None:
        return None

    async def close_session(self, session_handle: SessionExecutionHandle) -> None:
        return None

    async def shutdown(self) -> None:
        self.finish.set()


def _manager(tmp_path: Path) -> tuple[AgentRuntimeManager, _Backend]:
    backend = _Backend()
    manager = AgentRuntimeManager(
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        config=SimpleNamespace(),
        event_bus=EventBus(),
        session_memory=JsonlSessionMemory(tmp_path / "sessions"),
        profile_provider=_Profiles(),
        backend=backend,
        max_active_runs=5,
    )
    manager._resolve_new_session_llm = lambda profile, request: ("test", "model", None)  # type: ignore[method-assign]
    return manager, backend


@pytest.mark.asyncio
async def test_twenty_concurrent_identical_requests_create_one_run(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path)
    await manager.start_agent("agent-1")
    request = GatewayInput(type=GatewayInputType.USER_MESSAGE, content="hello", agent_name="agent_1")
    runs = await asyncio.gather(
        *(manager.start_run("agent-1", request, client_request_id="same-request") for _ in range(20))
    )
    assert len({run.ref.run_id for run in runs}) == 1
    assert backend.start_count == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_same_session_is_reserved_before_runner_load(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    await manager.start_agent("agent-1")

    async def replay(session_id: str) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        return {
            "session": {
                "data": {
                    "agent_id": "agent-1",
                    "agent_name": "agent_1",
                    "provider": "test",
                    "model": "model",
                    "metadata": {},
                }
            }
        }

    manager._session_memory.replay = replay  # type: ignore[method-assign]
    request = GatewayInput(type=GatewayInputType.USER_MESSAGE, content="hello", agent_name="agent_1")
    results = await asyncio.gather(
        manager.start_run("agent-1", request, "session-1", "request-1"),
        manager.start_run("agent-1", request, "session-1", "request-2"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RuntimeConflict) and item.code == "session_run_conflict" for item in results) == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_five_runs_execute_and_sixth_is_rejected(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path)
    await manager.start_agent("agent-1")
    request = GatewayInput(type=GatewayInputType.USER_MESSAGE, content="hello", agent_name="agent_1")
    runs = await asyncio.gather(
        *(
            manager.start_run("agent-1", request, client_request_id=f"request-{index}")
            for index in range(5)
        )
    )
    assert len({run.ref.run_id for run in runs}) == 5
    with pytest.raises(RuntimeConflict) as exc_info:
        await manager.start_run("agent-1", request, client_request_id="request-6")
    assert exc_info.value.code == "run_capacity_exceeded"
    assert backend.start_count == 5
    await manager.shutdown()


@pytest.mark.asyncio
async def test_cancel_and_watcher_publish_one_cancelled_terminal(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    await manager.start_agent("agent-1")
    request = GatewayInput(type=GatewayInputType.USER_MESSAGE, content="hello", agent_name="agent_1")
    run = await manager.start_run("agent-1", request, client_request_id="cancel-request")
    terminal = await manager.cancel_run(run.ref)
    assert terminal.status == RunStatus.CANCELLED
    assert manager._active_run_count() == 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_workspace_write_lease_is_run_scoped_and_nonblocking(tmp_path: Path) -> None:
    first = WorkspaceWriteLeaseManager(tmp_path)
    second = WorkspaceWriteLeaseManager(tmp_path)
    ref_a = SimpleNamespace(agent_id="a", session_id="s1", run_id="r1")
    ref_b = SimpleNamespace(agent_id="b", session_id="s2", run_id="r2")
    await first.acquire(ref_a)
    await first.acquire(ref_a)
    with pytest.raises(WorkspaceWriteBusy):
        await second.acquire(ref_b)
    await first.release(ref_a)
    await second.acquire(ref_b)
    await second.release(ref_b)
    assert (tmp_path / "workspace-write.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_run_event_scope_keeps_concurrent_sequence_contiguous() -> None:
    bus = EventBus()
    recorded: list[int] = []

    async def persist(event: StreamEvent) -> None:
        await asyncio.sleep(0)
        recorded.append(event.run_seq)

    bus.subscribe_stream(persist)
    ref = RunRef(agent_id="a", session_id="s", run_id="r", revision_id="v")
    scope = RunEventScope(bus, ref)
    await asyncio.gather(
        *(
            scope.publish_stream_event(
                StreamEvent(event_type="token", created_at="2026-07-29T00:00:00Z", data={"index": index})
            )
            for index in range(100)
        )
    )
    assert recorded == list(range(1, 101))
