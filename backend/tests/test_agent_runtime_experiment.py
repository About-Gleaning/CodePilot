from __future__ import annotations

import asyncio

import pytest

from codepilot.experiments.agent_runtime import (
    InProcessRuntimeProbe,
    InteractionConflict,
    RunStatus,
    WorkerProtocolProbe,
    WorkspaceWriteBusy,
)


@pytest.mark.asyncio
async def test_parallel_runs_are_isolated_and_request_is_idempotent() -> None:
    probe = InProcessRuntimeProbe()
    first = await probe.start_run(agent_id="agent-a", session_id="session-a", client_request_id="request-a", event_count=10)
    duplicate = await probe.start_run(agent_id="agent-a", session_id="session-a", client_request_id="request-a", event_count=10)
    second = await probe.start_run(agent_id="agent-b", session_id="session-b", client_request_id="request-b", event_count=10)
    assert duplicate == first
    assert await probe.wait(first.run_id) == RunStatus.COMPLETED
    assert await probe.wait(second.run_id) == RunStatus.COMPLETED
    assert {event.agent_id for event in probe.events if event.run_id == first.run_id} == {"agent-a"}
    assert {event.session_id for event in probe.events if event.run_id == second.run_id} == {"session-b"}


@pytest.mark.asyncio
async def test_write_lease_interaction_and_bounded_subscriber() -> None:
    probe = InProcessRuntimeProbe()
    subscriber = probe.subscribe(max_events=1)
    first = await probe.start_run(
        agent_id="agent-a", session_id="session-a", client_request_id="request-a", writes_workspace=True, require_interaction=True
    )
    with pytest.raises(WorkspaceWriteBusy):
        await probe.start_run(agent_id="agent-b", session_id="session-b", client_request_id="request-b", writes_workspace=True)
    await asyncio.sleep(0.02)
    state = probe._runs[first.run_id]  # 验证原型内部状态，正式代码不得这样访问。
    with pytest.raises(InteractionConflict):
        await probe.resolve_interaction(first.run_id, "stale", True)
    await probe.resolve_interaction(first.run_id, state.interaction_id or "", True)
    assert await probe.wait(first.run_id) == RunStatus.COMPLETED
    assert subscriber.resync_required is True


@pytest.mark.asyncio
async def test_cancel_and_close_do_not_cancel_other_agent() -> None:
    probe = InProcessRuntimeProbe()
    first = await probe.start_run(agent_id="agent-a", session_id="session-a", client_request_id="request-a", event_count=100, delay_seconds=0.01)
    second = await probe.start_run(agent_id="agent-b", session_id="session-b", client_request_id="request-b", event_count=2)
    await probe.close_agent("agent-a")
    assert await probe.wait(first.run_id) == RunStatus.CANCELLED
    assert await probe.wait(second.run_id) == RunStatus.COMPLETED


def test_worker_protocol_has_real_process_boundary_and_crash_isolated() -> None:
    probe = WorkerProtocolProbe()
    try:
        assert probe.start() == {"type": "ready", "protocol": 1}
        assert probe.ping("ping-1") == {"type": "pong", "request_id": "ping-1"}
        probe.crash()
        assert probe.alive is False
    finally:
        probe.stop()
