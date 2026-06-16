from __future__ import annotations

"""定时任务主进程调度器。"""

import asyncio
import secrets
import sys
from asyncio.subprocess import Process
from pathlib import Path
from typing import Any

from codepilot.config import AppSettings, WorkspaceState
from codepilot.scheduler.models import (
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleTask,
    ScheduleTrigger,
    TERMINAL_RUN_STATUSES,
    compute_following_run_at,
    compute_next_run_at,
    parse_iso_datetime,
    to_iso,
    utc_now,
)
from codepilot.scheduler.store import ScheduleStore


class ScheduleRunner:
    """主进程调度到期任务，并用独立 worker 子进程执行。"""

    def __init__(
        self,
        *,
        store: ScheduleStore,
        settings: AppSettings,
        workspace: WorkspaceState,
        agent_profiles: dict[str, Any],
        max_workers: int = 2,
        tick_seconds: float = 1.0,
        worker_timeout_seconds: int = 60 * 60,
    ) -> None:
        self._store = store
        self._settings = settings
        self._workspace = workspace
        self._agent_profiles = agent_profiles
        self._max_workers = max_workers
        self._tick_seconds = tick_seconds
        self._worker_timeout_seconds = worker_timeout_seconds
        self._task: asyncio.Task[None] | None = None
        self._processes: dict[str, Process] = {}
        self._monitor_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._store.recover_running_runs()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever(), name="codepilot-schedule-runner")

    async def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for task in list(self._monitor_tasks):
            task.cancel()
        if self._monitor_tasks:
            await asyncio.gather(*self._monitor_tasks, return_exceptions=True)

    async def tick_once(self) -> None:
        async with self._lock:
            now = utc_now()
            self._enqueue_due_tasks(now)
            await self._start_pending_runs()

    def create_task(
        self,
        *,
        name: str,
        prompt: str,
        agent_name: str,
        provider: str,
        model: str,
        trigger: ScheduleTrigger,
        working_dir: str,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
        isolation_mode: str = "subprocess",
    ) -> ScheduleTask:
        now_iso = to_iso(utc_now())
        task = ScheduleTask(
            name=name,
            prompt=prompt,
            agent_name=agent_name,
            provider=provider,
            model=model,
            metadata=metadata or {},
            trigger=trigger,
            working_dir=working_dir,
            isolation_mode=isolation_mode,
            enabled=enabled,
            created_at=now_iso,
            updated_at=now_iso,
            next_run_at=compute_next_run_at(trigger) if enabled else None,
        )
        self._store.upsert_task(task)
        return task

    def update_task(self, task_id: str, updates: dict[str, Any]) -> ScheduleTask | None:
        current = self._store.get_task(task_id)
        if current is None:
            return None
        merged = current.model_dump()
        merged.update(updates)
        merged["updated_at"] = to_iso(utc_now())
        if "trigger" in updates and isinstance(updates["trigger"], ScheduleTrigger):
            merged["trigger"] = updates["trigger"]
        if "enabled" in updates or "trigger" in updates:
            enabled = bool(merged.get("enabled"))
            trigger = merged["trigger"] if isinstance(merged["trigger"], ScheduleTrigger) else ScheduleTrigger.model_validate(merged["trigger"])
            merged["next_run_at"] = compute_next_run_at(trigger) if enabled else None
        task = ScheduleTask.model_validate(merged)
        self._store.upsert_task(task)
        return task

    def delete_task(self, task_id: str) -> bool:
        deleted = self._store.delete_task(task_id)
        if deleted:
            self._store.cancel_pending_runs_for_task(task_id)
        return deleted

    async def report(self, run_id: str, *, status: ScheduleRunStatus, session_id: str | None, summary: str | None, error: str | None) -> ScheduleRun:
        async with self._lock:
            run = self._store.get_run(run_id)
            if run is None:
                raise ValueError(f"run `{run_id}` 不存在")
            if run.status != ScheduleRunStatus.RUNNING:
                raise ValueError(f"run `{run_id}` 当前状态不是 running")
            updated = run.model_copy(
                update={
                    "status": status,
                    "session_id": session_id,
                    "summary": summary,
                    "error": error,
                    "finished_at": to_iso(utc_now()),
                }
            )
            self._store.update_run(updated)
            return updated

    async def _run_forever(self) -> None:
        while True:
            await self.tick_once()
            await asyncio.sleep(self._tick_seconds)

    def _enqueue_due_tasks(self, now: Any) -> None:
        tasks = self._store.list_tasks()
        changed = False
        for index, task in enumerate(tasks):
            if not task.enabled or not task.next_run_at:
                continue
            if parse_iso_datetime(task.next_run_at) > now:
                continue
            run = ScheduleRun(
                task_id=task.id,
                task_name=task.name,
                status=ScheduleRunStatus.PENDING,
                scheduled_at=task.next_run_at,
                working_dir=task.working_dir,
            )
            self._store.append_run(run)
            next_run_at = compute_following_run_at(task.trigger, task.next_run_at, now=now)
            tasks[index] = task.model_copy(
                update={
                    "enabled": task.trigger.kind != "once",
                    "last_run_at": task.next_run_at,
                    "next_run_at": next_run_at,
                    "updated_at": to_iso(now),
                }
            )
            changed = True
        if changed:
            self._store.save_tasks(tasks)

    async def _start_pending_runs(self) -> None:
        active_count = len([run for run in self._store.active_runs() if run.status == ScheduleRunStatus.RUNNING])
        available = max(self._max_workers - active_count, 0)
        if available <= 0:
            return
        pending_runs = [run for run in self._store.list_runs() if run.status == ScheduleRunStatus.PENDING]
        pending_runs.sort(key=lambda item: item.scheduled_at)
        for run in pending_runs[:available]:
            task = self._store.get_task(run.task_id)
            if task is None:
                self._store.update_run(
                    run.model_copy(
                        update={
                            "status": ScheduleRunStatus.CANCELLED,
                            "finished_at": to_iso(utc_now()),
                            "error": "任务已不存在，pending run 已取消。",
                        }
                    )
                )
                continue
            await self._start_run(task, run)

    async def _start_run(self, task: ScheduleTask, run: ScheduleRun) -> None:
        stdout_path = self._store.run_logs_dir / f"{run.id}.stdout.log"
        stderr_path = self._store.run_logs_dir / f"{run.id}.stderr.log"
        prompt_path = self._write_prompt_file(run.id, task.prompt)
        stdout = stdout_path.open("ab")
        stderr = stderr_path.open("ab")
        report_url = f"http://127.0.0.1:{self._settings.server.port}/api/schedule-runs/{run.id}/report"
        args = [
            sys.executable,
            "-m",
            "codepilot.scheduler.worker",
            "--run-id",
            run.id,
            "--task-id",
            task.id,
            "--task-name",
            task.name,
            "--prompt-file",
            str(prompt_path),
            "--agent-name",
            task.agent_name,
            "--provider",
            task.provider,
            "--model",
            task.model,
            "--execution-dir",
            task.working_dir,
            "--storage-workspace-dir",
            str(self._workspace.workspace_dir),
            "--report-url",
            report_url,
            "--report-token-file",
            str(self._store.token_file),
        ]
        try:
            process = await asyncio.create_subprocess_exec(*args, stdout=stdout, stderr=stderr, cwd=task.working_dir)
        except Exception as exc:  # noqa: BLE001
            stdout.close()
            stderr.close()
            self._delete_prompt_file(prompt_path)
            self._store.update_run(
                run.model_copy(
                    update={
                        "status": ScheduleRunStatus.FAILED,
                        "finished_at": to_iso(utc_now()),
                        "error": f"启动 worker 失败：{exc}",
                    }
                )
            )
            return
        running = run.model_copy(update={"status": ScheduleRunStatus.RUNNING, "started_at": to_iso(utc_now()), "pid": process.pid})
        self._store.update_run(running)
        self._processes[run.id] = process
        monitor = asyncio.create_task(
            self._monitor_process(run.id, process, stdout_path, stderr_path, prompt_path, stdout, stderr),
            name=f"codepilot-schedule-monitor-{run.id}",
        )
        self._monitor_tasks.add(monitor)
        monitor.add_done_callback(self._monitor_tasks.discard)

    def _write_prompt_file(self, run_id: str, prompt: str) -> Path:
        prompt_dir = self._store.workspace_dir / "schedule_prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{run_id}.{secrets.token_urlsafe(12)}.prompt"
        prompt_path.write_text(prompt, encoding="utf-8")
        try:
            prompt_path.chmod(0o600)
        except OSError:
            # 权限收紧失败不阻断本地执行；文件仍不再暴露到进程命令行。
            pass
        return prompt_path

    def _delete_prompt_file(self, prompt_path: Path) -> None:
        try:
            prompt_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def _monitor_process(
        self,
        run_id: str,
        process: Process,
        stdout_path: Path,
        stderr_path: Path,
        prompt_path: Path,
        stdout: Any,
        stderr: Any,
    ) -> None:
        try:
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout=self._worker_timeout_seconds)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                await self._mark_running_process_finished(
                    run_id,
                    ScheduleRunStatus.TIMEOUT,
                    f"worker 超过 {self._worker_timeout_seconds} 秒未完成，已终止。",
                )
                return
            run = self._store.get_run(run_id)
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                return
            if return_code == 0:
                await self._mark_running_process_finished(run_id, ScheduleRunStatus.FAILED, "worker 退出但未上报执行结果。")
            else:
                await self._mark_running_process_finished(
                    run_id,
                    ScheduleRunStatus.FAILED,
                    f"worker 异常退出，exit_code={return_code}，日志：{stdout_path} / {stderr_path}",
                )
        finally:
            stdout.close()
            stderr.close()
            self._delete_prompt_file(prompt_path)
            self._processes.pop(run_id, None)

    async def _mark_running_process_finished(self, run_id: str, status: ScheduleRunStatus, error: str) -> None:
        async with self._lock:
            run = self._store.get_run(run_id)
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                return
            self._store.update_run(
                run.model_copy(
                    update={
                        "status": status,
                        "finished_at": to_iso(utc_now()),
                        "error": error,
                    }
                )
            )
