from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from codepilot.api.session_routes import register_session_routes
from codepilot.events import EventBus, HumanInteractionEvent
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.memory import JsonlSessionMemory
from codepilot.session.agent_runtime import AgentRuntimeManager, RuntimeConflict, _request_fingerprint
from codepilot.session.agents import AgentProfile
from codepilot.session.state import AgentLifecycleState, AgentRuntimeState, RunRef, RunState, RunStatus


class FakeProfileProvider:
    def __init__(self, count: int = 6) -> None:
        self.profiles = {
            f"agent-{index}": AgentProfile(
                agent_id=f"agent-{index}",
                revision_id=f"revision-{index}",
                name=f"agent_{index}",
                description="测试 Agent",
                system_prompt="测试",
                allowed_tools=["read_file"],
            )
            for index in range(count)
        }

    def get_active_profile_snapshot(self, agent_id: str) -> AgentProfile:
        return self.profiles[agent_id].model_copy(deep=True)

    def get_record_snapshot(self, agent_id: str) -> dict[str, object]:
        profile = self.profiles[agent_id]
        return {"agent_id": agent_id, "name": profile.name, "archived": False, "profile": profile.model_copy(deep=True)}

    def list_active_profile_snapshots(self) -> list[AgentProfile]:
        return [profile.model_copy(deep=True) for profile in self.profiles.values()]


class FakeBackend:
    def __init__(self) -> None:
        self.started: set[str] = set()
        self.stopped: set[str] = set()

    async def start_agent(self, agent_id: str) -> None:
        self.started.add(agent_id)

    async def stop_agent(self, agent_id: str) -> None:
        self.stopped.add(agent_id)

    def get_session_snapshot(self, _handle: object) -> dict[str, object]:
        return {}

    async def shutdown(self) -> None:
        return None


def build_manager(tmp_path: Path) -> tuple[AgentRuntimeManager, FakeBackend]:
    backend = FakeBackend()
    manager = AgentRuntimeManager(
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        config=SimpleNamespace(),
        event_bus=EventBus(),
        session_memory=JsonlSessionMemory(tmp_path / "sessions"),
        profile_provider=FakeProfileProvider(),
        backend=backend,  # type: ignore[arg-type]
        max_started_agents=5,
    )
    return manager, backend


@pytest.mark.asyncio
async def test_five_agents_start_independently_and_sixth_is_rejected(tmp_path: Path) -> None:
    manager, backend = build_manager(tmp_path)
    for index in range(5):
        state = await manager.start_agent(f"agent-{index}")
        assert state.lifecycle_state.value == "RUNNING"
    with pytest.raises(RuntimeConflict, match="上限") as exc_info:
        await manager.start_agent("agent-5")
    assert exc_info.value.code == "started_agent_capacity_exceeded"
    assert backend.started == {f"agent-{index}" for index in range(5)}


@pytest.mark.asyncio
async def test_stopping_one_agent_does_not_change_another(tmp_path: Path) -> None:
    manager, backend = build_manager(tmp_path)
    await manager.start_agent("agent-0")
    await manager.start_agent("agent-1")
    await manager.stop_agent("agent-0")
    assert manager.get_agent_state("agent-0").lifecycle_state.value == "STOPPED"
    assert manager.get_agent_state("agent-1").lifecycle_state.value == "RUNNING"
    assert backend.stopped == {"agent-0"}
    assert (tmp_path / "agent-runtimes.json").stat().st_mode & 0o777 == 0o600


def test_resource_api_enforces_started_capacity_and_cursor_validation(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)
    state = SimpleNamespace(
        agent_runtime=manager,
        session_runner=SimpleNamespace(get_status_snapshot=lambda: {"session_id": None, "status": "IDLE"}),
        session_memory=JsonlSessionMemory(tmp_path / "sessions"),
        settings=SimpleNamespace(sse=SimpleNamespace(heartbeat_seconds=1, replay_on_connect=True)),
        event_bus=EventBus(),
        event_store=SimpleNamespace(replay=lambda **_: []),
    )
    app = FastAPI()
    router = APIRouter(prefix="/api")
    register_session_routes(router, state)
    app.include_router(router)
    with TestClient(app) as client:
        overview = client.get("/api/agent-runtimes").json()
        assert overview["capacity"] == {
            "started_agents": 0,
            "max_started_agents": 5,
            "active_runs": 0,
            "max_active_runs": 1,
        }
        assert isinstance(overview["cursor"], str)
        for index in range(5):
            assert client.post(f"/api/agents/agent-{index}/start").status_code == 200
        rejected = client.post("/api/agents/agent-5/start")
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "started_agent_capacity_exceeded"
        invalid_cursor = client.get("/api/agent-runtimes/stream?cursor=not-a-cursor")
        assert invalid_cursor.status_code == 422
        assert invalid_cursor.json()["detail"]["code"] == "invalid_runtime_cursor"


@pytest.mark.asyncio
async def test_runtime_overview_keeps_archived_agent_with_runtime_state(tmp_path: Path) -> None:
    provider = FakeProfileProvider()
    backend = FakeBackend()
    manager = AgentRuntimeManager(
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        config=SimpleNamespace(),
        event_bus=EventBus(),
        session_memory=JsonlSessionMemory(tmp_path / "sessions"),
        profile_provider=provider,
        backend=backend,  # type: ignore[arg-type]
        max_started_agents=5,
    )
    await manager.start_agent("agent-0")
    provider.profiles.pop("agent-0")
    overview = await manager.get_runtime_overview()
    runtime = next(item for item in overview["runtimes"] if item["agent_id"] == "agent-0")
    assert runtime["lifecycle_state"] == "RUNNING"
    assert overview["capacity"]["started_agents"] == 1


@pytest.mark.asyncio
async def test_interaction_changes_publish_safe_control_events(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)
    await manager.start_agent("agent-0")
    run = RunState(
        ref=RunRef(agent_id="agent-0", session_id="session-0", run_id="run-0", revision_id="revision-0"),
        client_request_id="request-0",
        request_fingerprint="fingerprint-0",
        status=RunStatus.RUNNING,
        created_at="2026-07-31T00:00:00Z",
    )
    manager._runs[("agent-0", "session-0", "run-0")] = run
    subscription = manager.create_runtime_subscription()
    await manager.handle_domain_event(
        HumanInteractionEvent(
            agent_id="agent-0",
            session_id="session-0",
            run_id="run-0",
            revision_id="revision-0",
            interaction_id="interaction-0",
            created_at="2026-07-31T00:00:01Z",
            data={
                "kind": "approval",
                "status": "pending",
                "request": {"secret": "不能进入聚合流"},
            },
        )
    )
    event = await subscription.queue.get()
    assert event.event_type == "interaction_pending"
    assert event.data == {
        "interaction_id": "interaction-0",
        "kind": "approval",
        "status": "pending",
    }
    assert manager.get_agent_state("agent-0").waiting_human_count == 1


def test_request_fingerprint_depends_only_on_client_intent() -> None:
    request = GatewayInput(
        type=GatewayInputType.USER_MESSAGE,
        content="hello",
        agent_name="build",
        metadata={},
    )
    first = _request_fingerprint("agent-a", None, request)
    # revision 与解析后的默认模型不属于函数输入，因此配置更新后重试仍命中原 Run。
    second = _request_fingerprint("agent-a", None, request.model_copy(deep=True))
    assert first == second


@pytest.mark.asyncio
async def test_terminal_compare_and_set_releases_capacity_once(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)
    run = RunState(
        ref=RunRef(agent_id="agent-0", session_id="session-0", run_id="run-0", revision_id="revision-0"),
        client_request_id="request-0",
        request_fingerprint="fingerprint-0",
        status=RunStatus.RUNNING,
        created_at="2026-07-29T00:00:00Z",
    )
    manager._runs[("agent-0", "session-0", "run-0")] = run
    manager._runtimes["agent-0"] = AgentRuntimeState(
        agent_id="agent-0",
        desired_state=AgentLifecycleState.RUNNING,
        lifecycle_state=AgentLifecycleState.RUNNING,
        active_run_count=1,
    )
    await manager._transition_terminal(run, RunStatus.COMPLETED)
    await manager._transition_terminal(run, RunStatus.CANCELLED)
    assert run.status == RunStatus.COMPLETED
    assert manager._runtimes["agent-0"].active_run_count == 0
