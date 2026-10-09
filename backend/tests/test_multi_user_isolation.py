from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from codepilot.api.schedule_routes import ScheduleRunReportRequest
from codepilot.api.session_routes import StartRunRequest
from codepilot.events import EventBus, RunEventScope, SessionMetaEvent, StreamEvent
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.memory import UserPartitionedEventStore, UserPartitionedSessionMemory
from codepilot.migration import MigrationError, MultiUserMigration
from codepilot.scheduler import UserScheduleCoordinator
from codepilot.scheduler.models import ScheduleTask, ScheduleTrigger, to_iso, utc_now
from codepilot.session.agent_runtime import (
    AgentRuntimeManager,
    RunExecutionHandle,
    RunExecutionResult,
    SessionExecutionHandle,
)
from codepilot.session.agents import AgentProfile
from codepilot.session.state import AgentLifecycleState, RunRef, RunState, RunStatus


class _SharedProfiles:
    def __init__(self) -> None:
        self.profile = AgentProfile(
            agent_id="shared-agent",
            revision_id="shared-revision",
            name="same-name",
            description="共享 Agent",
            system_prompt="测试",
            visibility="shared",
        )

    def get_active_profile_snapshot(self, _user_id: str, _agent_id: str) -> AgentProfile:
        return self.profile.model_copy(deep=True)

    def get_record_snapshot(self, _user_id: str, _agent_id: str) -> dict[str, Any]:
        return {"profile": self.profile.model_copy(deep=True), "archived": False}

    def list_active_profile_snapshots(self, _user_id: str) -> list[AgentProfile]:
        return [self.profile.model_copy(deep=True)]


class _MultiUserBackend:
    def __init__(self) -> None:
        self.start_count = 0
        self.started_agents: list[tuple[str, str]] = []

    async def start_agent(self, user_id: str, agent_id: str) -> None:
        self.started_agents.append((user_id, agent_id))

    async def stop_agent(self, _user_id: str, _agent_id: str) -> None:
        return None

    async def load_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        _replay: dict[str, Any] | None,
        _profile: AgentProfile,
    ) -> SessionExecutionHandle:
        return SessionExecutionHandle(user_id, agent_id, session_id, SimpleNamespace())

    async def start_run(self, handle: SessionExecutionHandle, ref: RunRef, *_args: Any) -> RunExecutionHandle:
        self.start_count += 1
        return RunExecutionHandle(ref, handle, SimpleNamespace())

    async def wait_run(self, _handle: RunExecutionHandle) -> RunExecutionResult:
        return RunExecutionResult(status=RunStatus.COMPLETED)

    def get_session_snapshot(self, handle: SessionExecutionHandle) -> dict[str, Any]:
        return {"session_id": handle.session_id, "status": "RUNNING"}

    async def close_session(self, _handle: SessionExecutionHandle) -> None:
        return None

    async def shutdown(self) -> None:
        return None


@pytest.mark.asyncio
async def test_event_bus_filters_before_enqueue_and_replay(tmp_path: Path) -> None:
    user_a, user_b = str(uuid4()), str(uuid4())
    bus = EventBus()
    store = UserPartitionedEventStore(tmp_path)
    bus.subscribe_stream(store.append)
    subscription_a = bus.create_stream_subscription(user_id=user_a)
    subscription_b = bus.create_stream_subscription(user_id=user_b)
    ref = RunRef(user_id=user_a, agent_id="agent", session_id="same-session", run_id="run-a", revision_id="r1")

    await RunEventScope(bus, ref).publish_stream_event(
        StreamEvent(event_type="session_started", created_at="2026-08-17T00:00:00Z")
    )

    event = subscription_a.queue.get_nowait()
    assert event.user_id == user_a
    assert subscription_b.queue.empty()
    assert [item.user_id for item in store.replay(user_a, "same-session")] == [user_a]
    assert store.replay(user_b, "same-session") == []


@pytest.mark.asyncio
async def test_same_session_id_is_partitioned_by_user(tmp_path: Path) -> None:
    user_a, user_b = str(uuid4()), str(uuid4())
    memory = UserPartitionedSessionMemory(tmp_path)
    for user_id, title in ((user_a, "A 的会话"), (user_b, "B 的会话")):
        await memory.handle_domain_event(
            SessionMetaEvent(
                user_id=user_id,
                agent_id="shared-agent",
                session_id="same-session",
                created_at="2026-08-17T00:00:00Z",
                data={"session_id": "same-session", "agent_id": "shared-agent", "title": title},
            )
        )

    replay_a = await memory.replay(user_a, "same-session")
    replay_b = await memory.replay(user_b, "same-session")
    assert replay_a["session"]["data"]["title"] == "A 的会话"
    assert replay_b["session"]["data"]["title"] == "B 的会话"
    assert replay_a["session"]["data"]["user_id"] == user_a
    assert replay_b["session"]["data"]["user_id"] == user_b


@pytest.mark.asyncio
async def test_same_client_request_id_is_isolated_by_user(tmp_path: Path) -> None:
    user_a, user_b = str(uuid4()), str(uuid4())
    backend = _MultiUserBackend()
    manager = AgentRuntimeManager(
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        config=SimpleNamespace(),
        event_bus=EventBus(),
        session_memory=UserPartitionedSessionMemory(tmp_path),
        profile_provider=_SharedProfiles(),
        backend=backend,
        max_active_runs=5,
        max_active_runs_per_user=2,
    )
    manager._resolve_new_session_llm = lambda profile, request: ("test", "model", None)  # type: ignore[method-assign]
    request = GatewayInput(type=GatewayInputType.USER_MESSAGE, content="hello", agent_name="same-name")
    await manager.start_agent(user_a, "shared-agent")
    await manager.start_agent(user_b, "shared-agent")

    run_a, run_b = await asyncio.gather(
        manager.start_run(user_a, "shared-agent", request, client_request_id="same-request"),
        manager.start_run(user_b, "shared-agent", request, client_request_id="same-request"),
    )

    assert run_a.ref.user_id == user_a
    assert run_b.ref.user_id == user_b
    assert run_a.ref.run_id != run_b.ref.run_id
    assert backend.start_count == 2
    await manager.shutdown()


@pytest.mark.asyncio
async def test_recover_cancels_all_runs_and_only_starts_enabled_users(tmp_path: Path) -> None:
    enabled_user, disabled_user = str(uuid4()), str(uuid4())
    backend = _MultiUserBackend()
    manager = AgentRuntimeManager(
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        config=SimpleNamespace(),
        event_bus=EventBus(),
        session_memory=UserPartitionedSessionMemory(tmp_path),
        profile_provider=_SharedProfiles(),
        backend=backend,
        max_active_runs=5,
    )
    for user_id in (enabled_user, disabled_user):
        user_dir = tmp_path / "users" / user_id
        user_dir.mkdir(parents=True)
        (user_dir / "agent-runtimes.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "user_id": user_id,
                    "agents": {
                        "shared-agent": {
                            "desired_state": "RUNNING",
                            "lifecycle_state": "RUNNING",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        await manager._stores.runs(user_id).append(
            RunState(
                ref=RunRef(
                    user_id=user_id,
                    agent_id="shared-agent",
                    session_id=f"session-{user_id}",
                    run_id=f"run-{user_id}",
                    revision_id="shared-revision",
                ),
                client_request_id=f"request-{user_id}",
                request_fingerprint=f"fingerprint-{user_id}",
                status=RunStatus.RUNNING,
                created_at="2026-08-17T00:00:00+00:00",
            )
        )

    await manager.recover({enabled_user})

    assert manager.get_agent_state(enabled_user, "shared-agent").lifecycle_state == AgentLifecycleState.RUNNING
    disabled_state = manager.get_agent_state(disabled_user, "shared-agent")
    assert disabled_state.desired_state == AgentLifecycleState.STOPPED
    assert disabled_state.lifecycle_state == AgentLifecycleState.STOPPED
    assert all(run.status == RunStatus.CANCELLED for run in manager._runs.values())
    assert all(run.error_code == "service_restarted" for run in manager._runs.values())
    persisted = json.loads((tmp_path / "users" / disabled_user / "agent-runtimes.json").read_text(encoding="utf-8"))
    assert persisted["agents"]["shared-agent"]["desired_state"] == "STOPPED"


@pytest.mark.asyncio
async def test_recover_enforces_per_user_started_agent_capacity(tmp_path: Path) -> None:
    user_id = "user-capacity"
    backend = _MultiUserBackend()
    manager = AgentRuntimeManager(
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        config=SimpleNamespace(),
        event_bus=EventBus(),
        session_memory=UserPartitionedSessionMemory(tmp_path),
        profile_provider=_SharedProfiles(),
        backend=backend,
        max_started_agents=5,
        max_started_agents_per_user=2,
    )
    user_dir = tmp_path / "users" / user_id
    user_dir.mkdir(parents=True)
    (user_dir / "agent-runtimes.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "user_id": user_id,
                "agents": {
                    f"agent-{index}": {"desired_state": "RUNNING", "lifecycle_state": "RUNNING"}
                    for index in range(3)
                },
            }
        ),
        encoding="utf-8",
    )

    await manager.recover({user_id})

    assert len(backend.started_agents) == 2
    overflow = manager.get_agent_state(user_id, "agent-2")
    assert overflow.desired_state == AgentLifecycleState.STOPPED
    assert overflow.lifecycle_state == AgentLifecycleState.STOPPED
    assert overflow.error_code == "user_started_agent_capacity_exceeded"


@pytest.mark.asyncio
async def test_recover_enforces_service_started_agent_capacity_across_users(tmp_path: Path) -> None:
    users = ["user-a", "user-b"]
    backend = _MultiUserBackend()
    manager = AgentRuntimeManager(
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        config=SimpleNamespace(),
        event_bus=EventBus(),
        session_memory=UserPartitionedSessionMemory(tmp_path),
        profile_provider=_SharedProfiles(),
        backend=backend,
        max_started_agents=3,
        max_started_agents_per_user=2,
    )
    for user_id in users:
        user_dir = tmp_path / "users" / user_id
        user_dir.mkdir(parents=True)
        (user_dir / "agent-runtimes.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "user_id": user_id,
                    "agents": {
                        f"agent-{index}": {"desired_state": "RUNNING", "lifecycle_state": "RUNNING"}
                        for index in range(2)
                    },
                }
            ),
            encoding="utf-8",
        )

    await manager.recover(set(users))

    assert len(backend.started_agents) == 3
    overflow = manager.get_agent_state("user-b", "agent-1")
    assert overflow.desired_state == AgentLifecycleState.STOPPED
    assert overflow.lifecycle_state == AgentLifecycleState.STOPPED
    assert overflow.error_code == "service_started_agent_capacity_exceeded"


@pytest.mark.asyncio
async def test_schedule_start_disables_offline_disabled_user_without_runner(tmp_path: Path, monkeypatch) -> None:
    enabled_user, disabled_user = str(uuid4()), str(uuid4())

    class Workspace:
        workspace_dir = tmp_path

        def for_user(self, user_id: str):
            user_dir = tmp_path / "users" / user_id
            user_dir.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(user_runtime_dir=user_dir)

    coordinator = UserScheduleCoordinator(
        settings=SimpleNamespace(),
        workspace=Workspace(),
        profile_provider=_SharedProfiles(),
    )
    started: list[str] = []

    async def fake_start(runner):
        started.append(runner._user_id)

    monkeypatch.setattr("codepilot.scheduler.multi_user.ScheduleRunner.start", fake_start)
    now = to_iso(utc_now())
    for user_id in (enabled_user, disabled_user):
        coordinator.store(user_id).save_tasks(
            [
                ScheduleTask(
                    user_id=user_id,
                    name="巡检",
                    prompt="检查",
                    agent_id="shared-agent",
                    agent_name="same-name",
                    revision_id="shared-revision",
                    provider="test",
                    model="model",
                    trigger=ScheduleTrigger(kind="interval", interval_seconds=60),
                    working_dir=str(tmp_path),
                    created_at=now,
                    updated_at=now,
                    next_run_at=now,
                )
            ]
        )

    await coordinator.start({enabled_user})

    assert started == [enabled_user]
    assert disabled_user not in coordinator._runners
    disabled_task = coordinator.store(disabled_user).list_tasks()[0]
    assert disabled_task.enabled is False
    assert disabled_task.next_run_at is None


@pytest.mark.parametrize("reserved", ["user_id", "source", "agent_id", "revision_id", "schedule_run_id"])
def test_resource_run_rejects_reserved_user_metadata(reserved: str) -> None:
    with pytest.raises(ValidationError, match="服务端保留字段"):
        StartRunRequest(content="hello", client_request_id="request", user_metadata={reserved: "forged"})


def test_worker_report_requires_complete_execution_identity() -> None:
    with pytest.raises(ValidationError, match="Field required"):
        ScheduleRunReportRequest(
            run_id="run",
            status="completed",
            user_id=str(uuid4()),
        )


def test_migration_preview_apply_and_retry(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    user_id = str(uuid4())
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "life.md").write_text("---\nname: life\nkind: agent\ndescription: test\ntools: []\nreadonly: false\ncan_call_subagent: false\n---\ntest\n", encoding="utf-8")
    (workspace / "sessions").mkdir(parents=True)
    (workspace / "sessions" / "2026-08-17-same.jsonl").write_text(
        json.dumps({"record_type": "session_meta", "session_id": "same", "data": {"session_id": "same"}}) + "\n",
        encoding="utf-8",
    )
    migration = MultiUserMigration(codepilot_home=home, workspace_dir=workspace)

    preview = migration.preview(user_id)
    assert preview["migration_required"] is True
    assert all(not item.startswith("/") for item in preview["sources"])
    first = migration.apply(user_id)
    second = migration.apply(user_id)

    assert first == second
    migrated = workspace / "users" / user_id / "sessions" / "2026-08-17-same.jsonl"
    record = json.loads(migrated.read_text(encoding="utf-8"))
    assert record["schema_version"] == 2
    assert record["user_id"] == user_id
    assert record["data"]["user_id"] == user_id
    assert (home / first["backup"].removeprefix("codepilot_home/")).exists()


def test_migration_disables_schedule_without_stable_agent_mapping(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    user_id = str(uuid4())
    workspace.mkdir(parents=True)
    now = to_iso(utc_now())
    (workspace / "schedules.json").write_text(
        json.dumps(
            [
                {
                    "name": "旧巡检",
                    "prompt": "检查",
                    "agent_name": "missing-agent",
                    "provider": "test",
                    "model": "model",
                    "trigger": {"kind": "interval", "interval_seconds": 60},
                    "working_dir": str(tmp_path),
                    "enabled": True,
                    "created_at": now,
                    "updated_at": now,
                    "next_run_at": now,
                    "metadata": {"source": "legacy"},
                }
            ]
        ),
        encoding="utf-8",
    )

    MultiUserMigration(codepilot_home=home, workspace_dir=workspace).apply(
        user_id,
        profile_by_name=lambda _name: None,
    )

    migrated = json.loads((workspace / "users" / user_id / "schedules.json").read_text(encoding="utf-8"))[0]
    assert migrated["enabled"] is False
    assert migrated["next_run_at"] is None
    assert migrated["metadata"]["migration_status"] == "disabled_agent_unresolved"


def test_migration_fails_closed_on_middle_jsonl_corruption(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    sessions = workspace / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "broken.jsonl").write_text('{"record_type":"a"}\nnot-json\n{"record_type":"b"}\n', encoding="utf-8")
    migration = MultiUserMigration(codepilot_home=home, workspace_dir=workspace)

    with pytest.raises(MigrationError, match="中间记录损坏"):
        migration.apply(str(uuid4()))
    assert not migration.sentinel.exists()
