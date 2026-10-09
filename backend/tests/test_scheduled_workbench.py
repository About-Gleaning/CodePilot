from __future__ import annotations

import pytest
import json
from types import SimpleNamespace
from uuid import uuid4

from codepilot.scheduler.models import ScheduleRun, ScheduleRunStatus, ScheduleTrigger, to_iso, utc_now
from codepilot.scheduler.store import ScheduleStore
from test_scheduler import build_runner, build_task


@pytest.mark.asyncio
async def test_overlapping_trigger_is_recorded_without_queueing(tmp_path):
    store = ScheduleStore(tmp_path)
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60), next_run_at="2026-01-01T00:00:00+00:00")
    store.upsert_task(task)
    runner = build_runner(tmp_path, store)
    await runner.tick_once()
    store.upsert_task(store.get_task(task.id).model_copy(update={"next_run_at": "2026-01-01T00:00:00+00:00"}))
    await runner.tick_once()
    assert len(store.active_runs()) == 1
    assert any(run.status == ScheduleRunStatus.SKIPPED for run in store.list_runs())
    runner.update_task(task.id, {"enabled": False})
    assert store.active_runs() == []


@pytest.mark.asyncio
async def test_reports_are_ordered_and_terminal_cannot_be_overwritten(tmp_path):
    store = ScheduleStore(tmp_path)
    run = ScheduleRun(task_id="task", task_name="测试", status=ScheduleRunStatus.RUNNING,
                      scheduled_at=to_iso(utc_now()), working_dir=str(tmp_path), session_id="sess_test")
    store.append_run(run)
    runner = build_runner(tmp_path, store)
    await runner.report(run.id, status=ScheduleRunStatus.RUNNING, session_id=run.session_id,
                        summary=None, error=None, report_seq=2, iteration=3)
    await runner.report(run.id, status=ScheduleRunStatus.RUNNING, session_id=run.session_id,
                        summary=None, error=None, report_seq=1, iteration=1)
    assert store.get_run(run.id).iteration == 3
    await runner.report(run.id, status=ScheduleRunStatus.CANCELLED, session_id=run.session_id,
                        summary=None, error=None, report_seq=3, stop_reason="max_iterations")
    await runner.report(run.id, status=ScheduleRunStatus.COMPLETED, session_id=run.session_id,
                        summary=None, error=None, report_seq=4)
    assert store.get_run(run.id).status == ScheduleRunStatus.CANCELLED
    assert store.get_run(run.id).stop_reason == "max_iterations"


def test_store_updates_index_without_reading_history_again(tmp_path, monkeypatch):
    store = ScheduleStore(tmp_path)
    run = ScheduleRun(task_id="task", task_name="测试", status=ScheduleRunStatus.RUNNING,
                      scheduled_at=to_iso(utc_now()), working_dir=str(tmp_path))
    store.append_run(run)
    monkeypatch.setattr(store, "list_run_snapshots", lambda: pytest.fail("不应重复解析完整历史"))
    assert store.get_run(run.id).id == run.id
    second = ScheduleStore(tmp_path)
    second.update_run(run.model_copy(update={"status": ScheduleRunStatus.COMPLETED}))
    assert store.get_run(run.id).status == ScheduleRunStatus.COMPLETED


def test_incremental_events_ignore_incomplete_line_and_reject_paths(tmp_path):
    from codepilot.scheduler.events import read_events
    path = tmp_path / "2026-09-23-sess_test.events.jsonl"
    first = {"event_id": "one", "event_type": "assistant_message_completed"}
    encoded = json.dumps(first).encode() + b"\n"
    path.write_bytes(encoded + b'{"event_id":')
    batch = read_events(tmp_path, "sess_test")
    assert batch["events"] == [first]
    assert batch["cursor"].endswith(f":{len(encoded)}")
    path.write_bytes(encoded + b'{"event_id":"two","event_type":"loop_iteration_started"}\n')
    assert read_events(tmp_path, "sess_test", batch["cursor"])["events"][0]["event_id"] == "two"
    with pytest.raises(ValueError):
        read_events(tmp_path, "../secret")
    with pytest.raises(ValueError):
        read_events(tmp_path, "sess_test", "../../secret:0")


def test_artifact_requires_successful_structured_output_inside_directory(tmp_path):
    from codepilot.session.workbench import artifact_target, decorate_message
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "result.md"
    target.write_text("受控成果")
    part = {"type": "tool", "tool": "write_file", "call_id": "call", "state": {"status": "completed", "output": {"status": "ok", "file_path": str(target)}}}
    assert artifact_target(part, root) == target
    message = decorate_message({"info": {"id": "message"}, "parts": [part]}, run_id="run", agent_id="agent", session_id="sess_test", root=root)
    assert message["parts"][0]["artifact"]["url"].endswith("/artifacts/message/call")
    outside = tmp_path / "secret"
    outside.write_text("私密")
    target.unlink()
    target.symlink_to(outside)
    assert artifact_target(part, root) is None
    part["state"]["status"] = "error"
    assert artifact_target(part, root) is None


def api_fixture(tmp_path):
    from fastapi import APIRouter, FastAPI, Request
    from codepilot.api.schedule_routes import register_schedule_routes
    from codepilot.scheduler.multi_user import UserScheduleCoordinator
    from codepilot.config import AppSettings
    owner = str(uuid4())
    settings = AppSettings()
    workspace = SimpleNamespace(workspace_dir=tmp_path, for_user=lambda user: SimpleNamespace(user_runtime_dir=tmp_path / user, sessions_dir=tmp_path / user / "sessions"))
    coordinator = UserScheduleCoordinator(settings=settings, workspace=workspace, profile_provider=None)
    store = coordinator.store(owner)
    run = ScheduleRun(user_id=owner, agent_id="agent", revision_id="revision", session_id="sess_test", task_id="task", task_name="测试", status=ScheduleRunStatus.RUNNING, scheduled_at=to_iso(utc_now()), working_dir=str(tmp_path), worker_exited=False)
    store.append_run(run)
    coordinator.runner(owner).token_path(run.id).write_text("run-secret")
    app = FastAPI()
    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.principal = SimpleNamespace(user_id=request.headers.get("test-user", owner))
        return await call_next(request)
    router = APIRouter()
    register_schedule_routes(router, SimpleNamespace(schedule_coordinator=coordinator, settings=settings, workspace=workspace))
    app.include_router(router)
    return app, coordinator, run


def test_report_authentication_is_bound_to_run_and_owner(tmp_path):
    from fastapi.testclient import TestClient
    app, coordinator, run = api_fixture(tmp_path)
    payload = {"run_id": run.id, "user_id": run.user_id, "agent_id": run.agent_id, "session_id": run.session_id,
               "revision_id": run.revision_id, "report_seq": 1, "status": "cancelled", "stop_reason": "max_iterations"}
    with TestClient(app) as client:
        url = f"/schedule-runs/{run.id}/report"
        assert client.post(url, json=payload).status_code == 403
        headers = {"x-codepilot-schedule-token": "run-secret"}
        assert client.post(url, json={**payload, "user_id": str(uuid4())}, headers=headers).status_code == 403
        assert client.post(url, json={**payload, "session_id": "sess_other"}, headers=headers).status_code == 403
        response = client.post(url, json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["run"]["status"] == "cancelled"
        assert client.post(url, json={**payload, "report_seq": 2, "status": "completed"}, headers=headers).json()["run"]["status"] == "cancelled"
        assert client.get(f"/schedule-runs/{run.id}/events", headers={"test-user": str(uuid4())}).status_code == 404
        assert client.post(f"/schedule-runs/{run.id}/stop", headers={"test-user": str(uuid4())}).status_code == 404
        invalid = client.post(url, json={"secret": "must-not-echo"})
        assert invalid.status_code == 422
        assert "must-not-echo" not in invalid.text


def test_worker_tool_requires_frozen_capability(tmp_path):
    from fastapi.testclient import TestClient
    from codepilot.session.agents import AgentProfile
    app, coordinator, run = api_fixture(tmp_path)
    profile = AgentProfile(agent_id=run.agent_id, name="测试", system_prompt="测试", description="测试", allowed_tools=[])
    coordinator.runner(run.user_id).execution_profiles[run.id] = profile
    payload = {"user_id": run.user_id, "agent_id": run.agent_id, "session_id": run.session_id,
               "revision_id": run.revision_id, "sender_agent_id": run.agent_id, "arguments": {"action": "list_tasks"}}
    with TestClient(app) as client:
        url = f"/schedule-runs/{run.id}/tool"
        headers = {"x-codepilot-schedule-token": "run-secret"}
        assert client.post(url, json=payload).status_code == 403
        assert client.post(url, json=payload, headers=headers).json()["error_type"] == "ScheduleToolAgentForbidden"
        coordinator.runner(run.user_id).execution_profiles[run.id] = profile.model_copy(update={"allowed_tools": ["schedule_manage"]})
        assert client.post(url, json=payload, headers=headers).json()["status"] == "ok"


def test_session_remains_locked_until_worker_has_exited(tmp_path):
    from codepilot.session.agent_runtime import RuntimeConflict
    _, coordinator, run = api_fixture(tmp_path)
    store = coordinator.store(run.user_id)
    store.update_run(run.model_copy(update={"status": ScheduleRunStatus.COMPLETED}))
    with pytest.raises(RuntimeConflict, match="收尾"):
        coordinator.assert_session_idle(run.user_id, run.agent_id, run.session_id)
    store.update_run(run.model_copy(update={"status": ScheduleRunStatus.COMPLETED, "worker_exited": True}))
    coordinator.assert_session_idle(run.user_id, run.agent_id, run.session_id)


@pytest.mark.asyncio
async def test_restart_cancels_pending_without_replaying(tmp_path):
    store = ScheduleStore(tmp_path)
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60), next_run_at="2026-01-01T00:00:00+00:00")
    store.upsert_task(task)
    run = ScheduleRun(task_id=task.id, task_name=task.name, status=ScheduleRunStatus.PENDING, scheduled_at=task.next_run_at, working_dir=str(tmp_path))
    store.append_run(run)
    runner = build_runner(tmp_path, store)
    await runner._recover()
    await runner.tick_once()
    assert store.get_run(run.id).status == ScheduleRunStatus.INTERRUPTED
    assert store.active_runs() == []
    assert len(store.list_runs()) == 1


@pytest.mark.asyncio
async def test_new_worker_uses_current_profile_and_keeps_overrides(tmp_path, monkeypatch):
    import asyncio
    from codepilot.scheduler.runner import ScheduleRunner
    from codepilot.session.agents import build_agent_profiles
    from test_session_hooks import build_settings
    profile = build_agent_profiles(7)["build"].model_copy(update={"agent_id": "agent", "revision_id": "new", "default_provider": "openai", "default_model": "gpt-5.3-codex"})
    provider = SimpleNamespace(get_active_profile_snapshot=lambda *_: profile, get_profile_revision_snapshot=lambda *_: profile)
    store = ScheduleStore(tmp_path)
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60)).model_copy(update={"user_id": "user", "agent_id": "agent", "revision_id": "old"})
    run = ScheduleRun(task_id=task.id, task_name=task.name, status=ScheduleRunStatus.PENDING, scheduled_at=to_iso(utc_now()), working_dir=str(tmp_path))
    store.append_run(run)
    runner = ScheduleRunner(store=store, settings=build_settings(), workspace=SimpleNamespace(workspace_dir=tmp_path), agent_profiles={}, profile_provider=provider)
    captured = {}
    class Process:
        pid = 12345
        async def wait(self): return 0
    async def launch(*args, **kwargs):
        captured["args"] = args
        return Process()
    monkeypatch.setattr("codepilot.scheduler.runner.asyncio.create_subprocess_exec", launch)
    await runner._start_run(task, run)
    await asyncio.gather(*runner._monitor_tasks)
    args = captured["args"]
    assert args[args.index("--revision-id") + 1] == "new"
    assert args[args.index("--execution-dir") + 1] == task.working_dir
    assert store.get_run(run.id).session_id.startswith("sess_")
    assert store.get_run(run.id).worker_exited


@pytest.mark.asyncio
@pytest.mark.parametrize("iterations,expected", [(2, "completed"), (1, "cancelled")])
async def test_real_worker_loop_auto_approves_and_persists_results(tmp_path, monkeypatch, iterations, expected):
    from codepilot.scheduler.worker import run_worker
    from codepilot.session.agents import AgentProfile
    from test_session_hooks import build_settings, SequencedLiteLLMClient
    from codepilot.memory import UserPartitionedSessionMemory
    user_id = str(uuid4())
    target = tmp_path / "result.md"
    settings = build_settings()
    profile = AgentProfile(agent_id="agent-test", revision_id="test-revision", name="自动执行", system_prompt="测试",
        allowed_tools=["write_file", "question"], max_iterations=iterations, skill_ids=[], hook_ids=[], memory_enabled=False)
    client = SequencedLiteLLMClient([
        SimpleNamespace(text="", reasoning="", tool_calls=[{"tool_call_id": "write-test", "tool_name": "write_file", "arguments": {"file_path": str(target), "content": "受控输出"}}]),
        SimpleNamespace(text="任务已完成", reasoning="", tool_calls=[]),
    ])
    monkeypatch.setattr("codepilot.runtime.LiteLLMClient", lambda **_: client)
    monkeypatch.setattr("codepilot.runtime.build_title_service", lambda _: None)
    monkeypatch.setattr("codepilot.scheduler.worker.load_settings", lambda _: settings)
    monkeypatch.setattr("codepilot.scheduler.worker.load_dotenv", lambda **_: None)
    monkeypatch.setattr("codepilot.scheduler.worker.configure_logging", lambda *_: None)
    monkeypatch.setattr("codepilot.scheduler.worker.AuthStore", lambda *_, **__: SimpleNamespace(find_enabled_user=lambda _: SimpleNamespace(user_id=user_id)))
    monkeypatch.setattr("codepilot.session.agent_config.MultiUserAgentConfigService", lambda **_: SimpleNamespace(
        get_profile_revision_snapshot=lambda *_, **__: profile.model_copy(deep=True), resolve_execution_profile=lambda _, value: value))
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"schema_version": 2, "prompt": "写文件", "profile": profile.model_dump()}))
    args = SimpleNamespace(session_id="sess_worker", run_id="run_test", task_id="task_test", task_name="测试任务",
        user_id=user_id, agent_id=profile.agent_id, revision_id=profile.revision_id, provider="openai", model="gpt-5.3-codex",
        execution_dir=str(tmp_path), storage_workspace_dir=str(tmp_path / "home" / "workspace" / "test"), execution_bundle_file=str(bundle))
    reports = []
    async def record_report(_, **payload): reports.append(payload)
    monkeypatch.setattr("codepilot.scheduler.worker.report", record_report)
    await run_worker(args)
    assert reports[-1]["status"].value == expected
    assert target.read_text() == "受控输出"
    memory = UserPartitionedSessionMemory(tmp_path / "home" / "workspace" / "test")
    replay = await memory.replay(user_id, "sess_worker")
    assert replay["messages"]
    assert not any(record["record_type"] == "human_interaction" for record in replay["records"])
    assert "无人值守" in client.last_provider_messages[0]["content"]


@pytest.mark.asyncio
async def test_worker_exit_reconciles_durable_result_without_report(tmp_path):
    from unittest.mock import AsyncMock
    import io
    store = ScheduleStore(tmp_path)
    run = ScheduleRun(task_id="task", task_name="测试", status=ScheduleRunStatus.RUNNING,
                      scheduled_at=to_iso(utc_now()), working_dir=str(tmp_path), session_id="sess_test", worker_exited=False)
    store.append_run(run)
    runner = build_runner(tmp_path, store)
    runner._session_memory = SimpleNamespace(
        replay=AsyncMock(return_value={"session": {"data": {"run_id": run.id, "status": "CANCELLED", "stop_reason": "max_iterations"}}}),
        handle_domain_event=AsyncMock(),
    )
    process = SimpleNamespace(pid=123, wait=AsyncMock(return_value=0))
    await runner._monitor_process(run.id, process, tmp_path / "out", tmp_path / "err", tmp_path / "bundle", io.StringIO(), io.StringIO())
    finished = store.get_run(run.id)
    assert finished.status == ScheduleRunStatus.CANCELLED
    assert finished.stop_reason == "max_iterations"
    assert finished.worker_exited
    assert runner._session_memory.handle_domain_event.call_args.args[0].status == "CANCELLED"


def test_descendant_cleanup_preserves_reused_process_identity(monkeypatch):
    from codepilot.scheduler.store import worker_descendants, reap_worker_descendants
    killed = []
    monkeypatch.setattr("codepilot.scheduler.store._process_table", lambda: {10: (1, "父"), 11: (10, "子"), 12: (11, "孙")})
    children = worker_descendants(10)
    assert children == {11: "子", 12: "孙"}
    monkeypatch.setattr("codepilot.scheduler.store._process_table", lambda: {11: (1, "已复用"), 12: (1, "孙")})
    monkeypatch.setattr("codepilot.scheduler.store.os.kill", lambda pid, _: killed.append(pid))
    reap_worker_descendants(children)
    assert killed == [12]


@pytest.mark.asyncio
async def test_unattended_hook_confirmation_fails_without_waiting():
    import asyncio
    from test_custom_hooks import WaitingHook
    from test_session_hooks import build_session, build_settings, StubLiteLLMClient, StubToolRegistry, StubToolDispatcher
    from codepilot.hooks import HookManager, HookType, RuntimeHandles
    from codepilot.events import EventBus
    from codepilot.session.session import AgentLoop
    from codepilot.session.state import SessionStatus
    from codepilot.session.agents import AgentProfile
    hooks = HookManager()
    hooks.register(WaitingHook(hook_id="confirm", hook_type=HookType.LLM_BEFORE, name="确认"))
    client = StubLiteLLMClient()
    loop = AgentLoop(llm_client=client, tool_registry=StubToolRegistry(), tool_dispatcher=StubToolDispatcher(), hook_manager=hooks)
    session = build_session()
    await asyncio.wait_for(loop.run(session, SimpleNamespace(workspace_path="/tmp/codepilot"),
        AgentProfile(name="build", system_prompt="测试", max_iterations=1), RuntimeHandles(event_bus=EventBus()),
        build_settings(), asyncio.Event(), {"result": None}, asyncio.Event(),
        allow_manual_approval=False, allow_question_interaction=False), 2)
    assert session.status == SessionStatus.FAILED
    assert session.stop_reason == "hook_requires_human"
    assert client.calls == 0
