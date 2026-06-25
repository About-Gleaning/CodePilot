from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from codepilot.config import AppSettings
from codepilot.config.settings import ActivatedLLMProvider, LLMRuntimeSettings, ServerSettings
from codepilot.memory.projections import build_session_summary
from codepilot.scheduler import ScheduleRunner, ScheduleStore
from codepilot.scheduler.models import (
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleTask,
    ScheduleTrigger,
    compute_next_run_at,
    parse_iso_datetime,
    to_iso,
    utc_now,
)
from codepilot.scheduler.worker import _build_worker_settings, _prepare_worker_runtime
from codepilot.session.agents import build_agent_profiles
from codepilot.tools import BaseTool, ScheduleManageTool, ToolExecutionContext, ToolRegistry, ToolSpec


def build_task(tmp_path, trigger: ScheduleTrigger, *, next_run_at: str | None = None, enabled: bool = True) -> ScheduleTask:
    now = to_iso(utc_now())
    return ScheduleTask(
        name="巡检",
        prompt="检查项目状态",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        trigger=trigger,
        working_dir=str(tmp_path),
        enabled=enabled,
        created_at=now,
        updated_at=now,
        next_run_at=next_run_at,
    )


def build_runner(tmp_path, store: ScheduleStore, max_workers: int = 0) -> ScheduleRunner:
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    return ScheduleRunner(
        store=store,
        settings=AppSettings(),
        workspace=workspace,
        agent_profiles={},
        max_workers=max_workers,
        tick_seconds=0.01,
    )


def build_schedule_tool(tmp_path, store: ScheduleStore | None = None) -> ScheduleManageTool:
    schedule_store = store or ScheduleStore(tmp_path)
    settings = AppSettings(
        llm_runtime=LLMRuntimeSettings(
            activated_providers={
                "openai": ActivatedLLMProvider(
                    provider="openai",
                    label="OpenAI",
                    models=["gpt-5.3-codex"],
                )
            }
        )
    )
    runner = ScheduleRunner(
        store=schedule_store,
        settings=settings,
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        agent_profiles={"build": SimpleNamespace(kind="agent"), "life": SimpleNamespace(kind="agent")},
        max_workers=0,
        tick_seconds=0.01,
    )
    return ScheduleManageTool(
        store=schedule_store,
        runner=runner,
        settings=settings,
        agent_profiles={"build": SimpleNamespace(kind="agent"), "life": SimpleNamespace(kind="agent")},
        timeout_seconds=1,
    )


def build_tool_context(tmp_path, *, agent_name: str = "build") -> ToolExecutionContext:
    allowed_tools = ["schedule_manage"] if agent_name == "build" else []
    return ToolExecutionContext(
        session=SimpleNamespace(session_id="session_1"),
        workspace=SimpleNamespace(workspace_path=tmp_path, workspace_dir=tmp_path),
        agent=SimpleNamespace(name=agent_name, allowed_tools=allowed_tools),
    )


class ApprovalRequiredTool(BaseTool):
    spec = ToolSpec(
        name="approval_required",
        description="测试审批工具",
        input_schema={"type": "object", "properties": {}},
        requires_approval=True,
        timeout_seconds=1,
    )

    async def execute(self, args, context=None):
        return {"status": "ok"}


def test_worker_runtime_removes_question_and_keeps_approval_policy() -> None:
    settings = AppSettings()
    worker_settings = _build_worker_settings(settings)
    tool_registry = ToolRegistry()
    tool_registry.register(ApprovalRequiredTool())
    runtime = SimpleNamespace(
        tool_registry=tool_registry,
        agent_profiles=build_agent_profiles(max_iterations=10),
    )

    _prepare_worker_runtime(runtime)

    assert settings.tools.bash.approval_mode == "all"
    assert worker_settings.tools.bash.approval_mode == "all"
    assert tool_registry.get("approval_required").spec.requires_approval is True
    assert "question" not in runtime.agent_profiles["build"].allowed_tools
    assert "question" not in runtime.agent_profiles["plan"].allowed_tools


def test_schedule_store_persists_tasks_atomically(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    trigger = ScheduleTrigger(kind="once", run_at=to_iso(utc_now()))
    task = build_task(tmp_path, trigger, next_run_at=compute_next_run_at(trigger))

    store.upsert_task(task)

    loaded = store.list_tasks()
    assert len(loaded) == 1
    assert loaded[0].id == task.id
    assert loaded[0].trigger.kind == "once"
    assert (tmp_path / "schedules.json").exists()


@pytest.mark.asyncio
async def test_once_task_creates_pending_run_and_disables_task(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    due_at = "2026-01-01T00:00:00+00:00"
    task = build_task(tmp_path, ScheduleTrigger(kind="once", run_at=due_at), next_run_at=due_at)
    store.upsert_task(task)
    runner = build_runner(tmp_path, store)

    await runner.tick_once()

    runs = store.list_runs()
    tasks = store.list_tasks()
    assert len(runs) == 1
    assert runs[0].status == ScheduleRunStatus.PENDING
    assert tasks[0].enabled is False
    assert tasks[0].next_run_at is None


@pytest.mark.asyncio
async def test_interval_task_computes_following_run(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    due_at = "2026-01-01T00:00:00+00:00"
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60), next_run_at=due_at)
    store.upsert_task(task)
    runner = build_runner(tmp_path, store)

    await runner.tick_once()

    updated = store.list_tasks()[0]
    assert updated.enabled is True
    assert updated.next_run_at is not None
    assert parse_iso_datetime(updated.next_run_at) > utc_now()


def test_update_task_recomputes_next_run_when_enabled_changes(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60), enabled=False)
    store.upsert_task(task)
    runner = build_runner(tmp_path, store)

    enabled = runner.update_task(task.id, {"enabled": True})
    disabled = runner.update_task(task.id, {"enabled": False})

    assert enabled is not None
    assert enabled.enabled is True
    assert enabled.next_run_at is not None
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.next_run_at is None


def test_weekly_trigger_computes_next_run_in_same_week() -> None:
    trigger = ScheduleTrigger(kind="weekly", day_of_week=5, time_of_day="10:00", timezone="Asia/Shanghai")
    now = datetime(2026, 6, 10, 1, 0, tzinfo=UTC)

    next_run_at = compute_next_run_at(trigger, now=now)

    assert next_run_at == "2026-06-12T02:00:00+00:00"


def test_weekly_trigger_rolls_to_next_week_after_target_time() -> None:
    trigger = ScheduleTrigger(kind="weekly", day_of_week=5, time_of_day="10:00", timezone="Asia/Shanghai")
    now = datetime(2026, 6, 12, 2, 0, tzinfo=UTC)

    next_run_at = compute_next_run_at(trigger, now=now)

    assert next_run_at == "2026-06-19T02:00:00+00:00"


def test_weekly_trigger_requires_valid_day_and_time() -> None:
    with pytest.raises(ValidationError, match="day_of_week"):
        ScheduleTrigger(kind="weekly", time_of_day="10:00")

    with pytest.raises(ValidationError, match="1 到 7"):
        ScheduleTrigger(kind="weekly", day_of_week=8, time_of_day="10:00")

    with pytest.raises(ValidationError, match="time_of_day"):
        ScheduleTrigger(kind="weekly", day_of_week=5)


@pytest.mark.asyncio
async def test_schedule_manage_tool_creates_and_lists_task(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    tool = build_schedule_tool(tmp_path, store)
    context = build_tool_context(tmp_path)

    created = await tool.execute(
        {
            "action": "create_task",
            "name": "每日巡检",
            "prompt": "检查项目状态",
            "agent_name": "build",
            "provider": "openai",
            "model": "gpt-5.3-codex",
            "working_dir": str(tmp_path),
            "trigger": {"kind": "interval", "interval_seconds": 60},
        },
        context=context,
    )
    listed = await tool.execute({"action": "list_tasks"}, context=context)

    assert created["status"] == "ok"
    assert created["schedule"]["name"] == "每日巡检"
    assert created["schedule"]["next_run_at"] is not None
    assert listed["tasks"][0]["id"] == created["schedule"]["id"]


@pytest.mark.asyncio
async def test_schedule_manage_tool_creates_weekly_task(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    tool = build_schedule_tool(tmp_path, store)
    context = build_tool_context(tmp_path)

    created = await tool.execute(
        {
            "action": "create_task",
            "name": "每周巡检",
            "prompt": "检查项目状态",
            "agent_name": "build",
            "provider": "openai",
            "model": "gpt-5.3-codex",
            "working_dir": str(tmp_path),
            "trigger": {"kind": "weekly", "day_of_week": 5, "time_of_day": "10:00", "timezone": "Asia/Shanghai"},
        },
        context=context,
    )

    assert created["status"] == "ok"
    assert created["schedule"]["trigger"]["kind"] == "weekly"
    assert created["schedule"]["trigger"]["day_of_week"] == 5
    assert created["schedule"]["next_run_at"] is not None


@pytest.mark.asyncio
async def test_schedule_manage_tool_updates_and_toggles_task(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    tool = build_schedule_tool(tmp_path, store)
    context = build_tool_context(tmp_path)
    created = await tool.execute(
        {
            "action": "create_task",
            "name": "巡检",
            "prompt": "检查项目状态",
            "agent_name": "build",
            "provider": "openai",
            "model": "gpt-5.3-codex",
            "working_dir": str(tmp_path),
            "trigger": {"kind": "interval", "interval_seconds": 60},
        },
        context=context,
    )
    task_id = created["schedule"]["id"]

    updated = await tool.execute({"action": "update_task", "task_id": task_id, "prompt": "检查测试状态"}, context=context)
    disabled = await tool.execute({"action": "disable_task", "task_id": task_id}, context=context)
    enabled = await tool.execute({"action": "enable_task", "task_id": task_id}, context=context)

    assert updated["schedule"]["prompt"] == "检查测试状态"
    assert disabled["schedule"]["enabled"] is False
    assert disabled["schedule"]["next_run_at"] is None
    assert enabled["schedule"]["enabled"] is True
    assert enabled["schedule"]["next_run_at"] is not None


@pytest.mark.asyncio
async def test_schedule_manage_tool_delete_cancels_pending_runs(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    tool = build_schedule_tool(tmp_path, store)
    context = build_tool_context(tmp_path)
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60))
    store.upsert_task(task)
    pending = ScheduleRun(
        task_id=task.id,
        task_name=task.name,
        status=ScheduleRunStatus.PENDING,
        scheduled_at=to_iso(utc_now()),
        working_dir=str(tmp_path),
    )
    store.append_run(pending)

    deleted = await tool.execute({"action": "delete_task", "task_id": task.id}, context=context)

    assert deleted["status"] == "ok"
    assert deleted["cancelled_pending_runs"] == 1
    assert store.get_task(task.id) is None
    assert store.get_run(pending.id).status == ScheduleRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_schedule_manage_tool_preflight_requires_approval_for_mutations(tmp_path) -> None:
    tool = build_schedule_tool(tmp_path)
    context = build_tool_context(tmp_path)

    readonly_actions = ["list_tasks", "list_runs"]
    mutation_actions = ["create_task", "update_task", "enable_task", "disable_task", "delete_task"]

    for action in readonly_actions:
        preflight = await tool.preflight({"action": action}, context)
        assert preflight.status == "allow"

    for action in mutation_actions:
        preflight = await tool.preflight({"action": action}, context)
        assert preflight.status == "requires_approval"
        assert preflight.reason is not None


@pytest.mark.asyncio
async def test_schedule_manage_tool_rejects_forbidden_agent(tmp_path) -> None:
    tool = build_schedule_tool(tmp_path)

    result = await tool.execute({"action": "list_tasks"}, context=build_tool_context(tmp_path, agent_name="plan"))

    assert result["status"] == "error"
    assert result["error_type"] == "ScheduleToolAgentForbidden"


@pytest.mark.asyncio
async def test_schedule_manage_tool_rejects_invalid_model(tmp_path) -> None:
    tool = build_schedule_tool(tmp_path)

    result = await tool.execute(
        {
            "action": "create_task",
            "name": "巡检",
            "prompt": "检查项目状态",
            "agent_name": "build",
            "provider": "openai",
            "model": "unknown",
            "working_dir": str(tmp_path),
            "trigger": {"kind": "interval", "interval_seconds": 60},
        },
        context=build_tool_context(tmp_path),
    )

    assert result["status"] == "error"
    assert result["error_type"] == "ScheduleModelInvalid"


@pytest.mark.asyncio
async def test_schedule_manage_tool_rejects_blank_working_dir(tmp_path) -> None:
    tool = build_schedule_tool(tmp_path)

    result = await tool.execute(
        {
            "action": "create_task",
            "name": "巡检",
            "prompt": "检查项目状态",
            "agent_name": "build",
            "provider": "openai",
            "model": "gpt-5.3-codex",
            "working_dir": "   ",
            "trigger": {"kind": "interval", "interval_seconds": 60},
        },
        context=build_tool_context(tmp_path),
    )

    assert result["status"] == "error"
    assert result["error_type"] == "ScheduleWorkingDirInvalid"


def test_delete_task_cancels_pending_runs_but_keeps_running_runs(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60))
    store.upsert_task(task)
    pending = ScheduleRun(
        task_id=task.id,
        task_name=task.name,
        status=ScheduleRunStatus.PENDING,
        scheduled_at=to_iso(utc_now()),
        working_dir=str(tmp_path),
    )
    running = ScheduleRun(
        task_id=task.id,
        task_name=task.name,
        status=ScheduleRunStatus.RUNNING,
        scheduled_at=to_iso(utc_now()),
        started_at=to_iso(utc_now()),
        pid=1,
        working_dir=str(tmp_path),
    )
    store.append_run(pending)
    store.append_run(running)
    runner = build_runner(tmp_path, store)

    deleted = runner.delete_task(task.id)

    assert deleted is True
    assert store.get_task(task.id) is None
    assert store.get_run(pending.id).status == ScheduleRunStatus.CANCELLED
    assert store.get_run(running.id).status == ScheduleRunStatus.RUNNING


def test_recover_running_run_without_pid_marks_interrupted(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    run = ScheduleRun(
        task_id="scht_1",
        task_name="巡检",
        status=ScheduleRunStatus.RUNNING,
        scheduled_at=to_iso(utc_now()),
        started_at=to_iso(utc_now()),
        pid=99999999,
        working_dir=str(tmp_path),
    )
    store.append_run(run)

    recovered = store.recover_running_runs()

    assert len(recovered) == 1
    assert store.get_run(run.id).status == ScheduleRunStatus.INTERRUPTED


def test_recover_running_run_with_matching_worker_keeps_running(tmp_path, monkeypatch) -> None:
    store = ScheduleStore(tmp_path)
    run = ScheduleRun(
        task_id="scht_1",
        task_name="巡检",
        status=ScheduleRunStatus.RUNNING,
        scheduled_at=to_iso(utc_now()),
        started_at=to_iso(utc_now()),
        pid=12345,
        working_dir=str(tmp_path),
    )
    store.append_run(run)
    monkeypatch.setattr(
        "codepilot.scheduler.store._process_command_line",
        lambda pid: ["python", "-m", "codepilot.scheduler.worker", "--run-id", run.id],
    )

    recovered = store.recover_running_runs()

    assert recovered == []
    assert store.get_run(run.id).status == ScheduleRunStatus.RUNNING


def test_recover_running_run_with_reused_pid_marks_interrupted(tmp_path, monkeypatch) -> None:
    store = ScheduleStore(tmp_path)
    run = ScheduleRun(
        task_id="scht_1",
        task_name="巡检",
        status=ScheduleRunStatus.RUNNING,
        scheduled_at=to_iso(utc_now()),
        started_at=to_iso(utc_now()),
        pid=12345,
        working_dir=str(tmp_path),
    )
    store.append_run(run)
    monkeypatch.setattr(
        "codepilot.scheduler.store._process_command_line",
        lambda pid: ["python", "-m", "codepilot.scheduler.worker", "--run-id", "run_other"],
    )

    recovered = store.recover_running_runs()

    assert len(recovered) == 1
    assert store.get_run(run.id).status == ScheduleRunStatus.INTERRUPTED


def test_recover_running_run_with_unknown_pid_identity_marks_interrupted(tmp_path, monkeypatch) -> None:
    store = ScheduleStore(tmp_path)
    run = ScheduleRun(
        task_id="scht_1",
        task_name="巡检",
        status=ScheduleRunStatus.RUNNING,
        scheduled_at=to_iso(utc_now()),
        started_at=to_iso(utc_now()),
        pid=12345,
        working_dir=str(tmp_path),
    )
    store.append_run(run)
    monkeypatch.setattr("codepilot.scheduler.store._process_command_line", lambda pid: None)

    recovered = store.recover_running_runs()

    assert len(recovered) == 1
    assert store.get_run(run.id).status == ScheduleRunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_runner_passes_prompt_by_file_not_command_line(tmp_path, monkeypatch) -> None:
    store = ScheduleStore(tmp_path)
    secret_prompt = "包含项目细节和凭据的定时任务 prompt"
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60))
    task = task.model_copy(update={"prompt": secret_prompt})
    run = ScheduleRun(
        task_id=task.id,
        task_name=task.name,
        status=ScheduleRunStatus.PENDING,
        scheduled_at=to_iso(utc_now()),
        working_dir=str(tmp_path),
    )
    runner = build_runner(tmp_path, store)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345

        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        prompt_file = args[args.index("--prompt-file") + 1]
        captured["prompt_file"] = prompt_file
        captured["prompt_content"] = Path(prompt_file).read_text(encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("codepilot.scheduler.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    await runner._start_run(task, run)
    await asyncio.gather(*runner._monitor_tasks)

    args = captured["args"]
    assert "--prompt-file" in args
    assert "--prompt" not in args
    assert secret_prompt not in args
    assert captured["prompt_content"] == secret_prompt
    assert not Path(captured["prompt_file"]).exists()


@pytest.mark.asyncio
async def test_runner_uses_loopback_report_url_when_server_binds_all_interfaces(tmp_path, monkeypatch) -> None:
    store = ScheduleStore(tmp_path)
    task = build_task(tmp_path, ScheduleTrigger(kind="interval", interval_seconds=60))
    run = ScheduleRun(
        task_id=task.id,
        task_name=task.name,
        status=ScheduleRunStatus.PENDING,
        scheduled_at=to_iso(utc_now()),
        working_dir=str(tmp_path),
    )
    runner = ScheduleRunner(
        store=store,
        settings=AppSettings(server=ServerSettings(host="0.0.0.0", port=8765)),
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        agent_profiles={},
        max_workers=0,
        tick_seconds=0.01,
    )
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345

        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        return FakeProcess()

    monkeypatch.setattr("codepilot.scheduler.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    await runner._start_run(task, run)
    await asyncio.gather(*runner._monitor_tasks)

    args = captured["args"]
    report_url = args[args.index("--report-url") + 1]
    assert report_url == f"http://127.0.0.1:8765/api/schedule-runs/{run.id}/report"
    assert "0.0.0.0" not in report_url


@pytest.mark.asyncio
async def test_report_updates_running_run(tmp_path) -> None:
    store = ScheduleStore(tmp_path)
    run = ScheduleRun(
        task_id="scht_1",
        task_name="巡检",
        status=ScheduleRunStatus.RUNNING,
        scheduled_at=to_iso(utc_now()),
        started_at=to_iso(utc_now()),
        pid=1,
        working_dir=str(tmp_path),
    )
    store.append_run(run)
    runner = build_runner(tmp_path, store)

    updated = await runner.report(
        run.id,
        status=ScheduleRunStatus.COMPLETED,
        session_id="sess_1",
        summary="完成",
        error=None,
    )

    assert updated.status == ScheduleRunStatus.COMPLETED
    assert store.get_run(run.id).session_id == "sess_1"


def test_session_summary_contains_schedule_marker() -> None:
    records = [
        {
            "record_type": "session_meta",
            "session_id": "sess_1",
            "created_at": "2026-06-01T00:00:00+00:00",
            "updated_at": "2026-06-01T00:00:00+00:00",
            "data": {
                "title": "巡检",
                "workspace_id": "ws_1",
                "workspace_path": "/tmp/project",
                "source": "schedule",
                "schedule_task_id": "scht_1",
                "schedule_run_id": "run_1",
                "schedule_task_name": "每日巡检",
            },
        },
        {
            "record_type": "session_started",
            "session_id": "sess_1",
            "created_at": "2026-06-01T00:00:01+00:00",
            "data": {"status": "RUNNING", "agent_name": "build", "provider": "openai", "model": "gpt-5.3-codex"},
        },
    ]

    summary = build_session_summary(records)

    assert summary["source"] == "schedule"
    assert summary["schedule_task_name"] == "每日巡检"


def test_schedule_manage_tool_is_only_exposed_to_writable_agents() -> None:
    profiles = build_agent_profiles(max_iterations=10)

    assert "schedule_manage" in profiles["build"].allowed_tools
    assert "schedule_manage" not in profiles["plan"].allowed_tools
    assert "schedule_manage" not in profiles["explore"].allowed_tools
