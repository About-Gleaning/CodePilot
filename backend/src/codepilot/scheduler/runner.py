from __future__ import annotations

"""定时任务主进程调度器。"""

import asyncio
import json
import secrets
import sys
import os
import signal
from asyncio.subprocess import Process
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from codepilot.scheduler.store import ScheduleStore, _atomic_write, _is_matching_schedule_worker, worker_descendants, reap_worker_descendants


class ScheduleRunner:
    """主进程调度到期任务，并用独立 worker 子进程执行。"""

    def __init__(
        self,
        *,
        store: ScheduleStore,
        settings: AppSettings,
        workspace: WorkspaceState,
        agent_profiles: dict[str, Any],
        user_id: str = "",
        profile_provider: Any | None = None,
        max_workers: int = 2,
        tick_seconds: float = 1.0,
        worker_timeout_seconds: int = 60 * 60,
        session_memory: Any = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._workspace = workspace
        self._agent_profiles = agent_profiles
        self._user_id = user_id
        self._profile_provider = profile_provider
        self._max_workers = max_workers
        self._tick_seconds = tick_seconds
        self._worker_timeout_seconds = worker_timeout_seconds
        self._session_memory = session_memory
        self._task: asyncio.Task[None] | None = None
        self._processes: dict[str, Process] = {}
        self._terminations: dict[int, asyncio.Task[None]] = {}
        self._monitor_tasks: set[asyncio.Task[None]] = set()
        self.execution_profiles: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            await self._recover()
            self._task = asyncio.create_task(self._run_forever(), name="codepilot-schedule-runner")

    async def _recover(self) -> None:
        # 重启只清理经身份核实的旧进程，绝不续跑旧队列或旧工具。
        for run in self._store.active_runs():
            if run.pid and _is_matching_schedule_worker(run.pid, run.id):
                descendants = await asyncio.to_thread(worker_descendants, run.pid)
                try:
                    os.kill(run.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if not _is_matching_schedule_worker(run.pid, run.id):
                        break
                if _is_matching_schedule_worker(run.pid, run.id):
                    try:
                        os.kill(run.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.sleep(0.2)
                    if _is_matching_schedule_worker(run.pid, run.id):
                        raise RuntimeError("旧定时进程尚未确认退出，禁止启动调度")
                await asyncio.to_thread(reap_worker_descendants, descendants)
            recovered = run.model_copy(update={
                "status": ScheduleRunStatus.INTERRUPTED, "stop_reason": "service_restarted",
                "finished_at": to_iso(utc_now()), "worker_exited": True,
                "error": "服务已重启，本轮未自动恢复；已发出的外部操作可能仍生效。", "phase": "finished",
                "external_effect_uncertain": run.status != ScheduleRunStatus.PENDING,
            })
            await self._finish_session(recovered)
            self._store.update_run(recovered)
            self.token_path(run.id).unlink(missing_ok=True)
        now = utc_now()
        tasks = self._store.list_tasks()
        for index, task in enumerate(tasks):
            if task.enabled and task.next_run_at and parse_iso_datetime(task.next_run_at) <= now:
                tasks[index] = task.model_copy(update={
                    "next_run_at": compute_following_run_at(task.trigger, task.next_run_at, now=now),
                    "enabled": task.trigger.kind != "once",
                })
        self._store.save_tasks(tasks)

    def token_path(self, run_id: str) -> Path:
        return self._store.run_logs_dir / f"{run_id}.token"

    async def shutdown(
        self,
        *,
        status: ScheduleRunStatus = ScheduleRunStatus.INTERRUPTED,
        reason: str = "调度器已关闭，worker 已终止。",
    ) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        processes = list(self._processes.items())
        for run_id, process in processes:
            await self._mark_running_process_finished(run_id, status, reason)
            await self._terminate(process)
        for task in list(self._monitor_tasks):
            task.cancel()
        if self._monitor_tasks:
            await asyncio.gather(*self._monitor_tasks, return_exceptions=True)
        for run_id, _ in processes:
            await self._mark_running_process_finished(run_id, status, reason)
            self._processes.pop(run_id, None)

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
        agent_id: str = "",
        agent_name: str,
        revision_id: str = "",
        provider: str,
        model: str,
        trigger: ScheduleTrigger,
        working_dir: str,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
        isolation_mode: str = "subprocess",
        follow_agent_model: bool = False,
        follow_agent_directory: bool = False,
    ) -> ScheduleTask:
        now_iso = to_iso(utc_now())
        task = ScheduleTask(
            user_id=self._user_id,
            name=name,
            prompt=prompt,
            agent_id=agent_id,
            agent_name=agent_name,
            revision_id=revision_id,
            provider=provider,
            model=model,
            follow_agent_model=follow_agent_model,
            follow_agent_directory=follow_agent_directory,
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
        if not task.enabled:
            self._store.cancel_pending_runs_for_task(task_id)
        return task

    async def stop_run(self, run_id: str) -> ScheduleRun:
        async with self._lock:
            run = self._store.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status in TERMINAL_RUN_STATUSES or run.status == ScheduleRunStatus.STOPPING:
                return run
            process = self._processes.get(run_id)
            updated = run.model_copy(update={
                "status": ScheduleRunStatus.STOPPING if process else ScheduleRunStatus.CANCELLED,
                "stop_reason": "user_cancelled", "phase": "stopping" if process else "finished",
                "finished_at": None if process else to_iso(utc_now()),
                "external_effect_uncertain": process is not None,
                "error": "正在停止；已发出的外部操作可能仍然生效。" if process else None,
            })
            self._store.update_run(updated)
        if process is not None:
            await self._terminate(process)
        return self._store.get_run(run_id)

    async def _terminate(self, process: Process) -> None:
        cleanup = self._terminations.get(process.pid)
        if cleanup is None:
            cleanup = asyncio.create_task(self._terminate_process(process))
            self._terminations[process.pid] = cleanup
        await asyncio.shield(cleanup)

    async def _terminate_process(self, process: Process) -> None:
        if process.returncode is not None:
            return
        descendants = await asyncio.to_thread(worker_descendants, process.pid)
        try:
            process.terminate()
        except ProcessLookupError:
            await asyncio.to_thread(reap_worker_descendants, descendants)
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            # 自建进程组只在句柄仍存活时强制回收，避免误杀复用 PID。
            if process.returncode is None:
                descendants.update(await asyncio.to_thread(worker_descendants, process.pid))
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()
        await asyncio.to_thread(reap_worker_descendants, descendants)

    def delete_task(self, task_id: str) -> bool:
        deleted = self._store.delete_task(task_id)
        if deleted:
            self._store.cancel_pending_runs_for_task(task_id)
        return deleted

    async def report(self, run_id: str, *, status: ScheduleRunStatus, session_id: str | None, summary: str | None, error: str | None,
                     report_seq: int = 0, phase: str = "", iteration: int = 0,
                     stop_reason: str | None = None) -> ScheduleRun:
        async with self._lock:
            run = self._store.get_run(run_id)
            if run is None:
                raise ValueError(f"run `{run_id}` 不存在")
            if run.session_id and session_id != run.session_id:
                raise ValueError("上报会话与本轮执行不一致")
            if run.status in TERMINAL_RUN_STATUSES or (report_seq and report_seq <= run.report_seq):
                return run
            if run.status == ScheduleRunStatus.STOPPING:
                return run
            updated = run.model_copy(
                update={
                    "status": status,
                    "session_id": session_id,
                    "summary": summary,
                    "error": error,
                    "finished_at": to_iso(utc_now()) if status in TERMINAL_RUN_STATUSES else None,
                    "report_seq": report_seq or run.report_seq + 1,
                    "phase": phase or ("finished" if status in TERMINAL_RUN_STATUSES else run.phase),
                    "iteration": max(run.iteration, iteration),
                    "stop_reason": stop_reason,
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
        occupied = {run.task_id for run in self._store.active_runs()}
        changed = False
        for index, task in enumerate(tasks):
            if not task.enabled or not task.next_run_at:
                continue
            if parse_iso_datetime(task.next_run_at) > now:
                continue
            run = ScheduleRun(
                user_id=task.user_id,
                task_id=task.id,
                task_name=task.name,
                agent_id=task.agent_id,
                agent_name=task.agent_name,
                revision_id=task.revision_id,
                status=ScheduleRunStatus.SKIPPED if task.id in occupied else ScheduleRunStatus.PENDING,
                scheduled_at=task.next_run_at,
                working_dir=task.working_dir,
                finished_at=to_iso(now) if task.id in occupied else None,
                stop_reason="previous_run_active" if task.id in occupied else None,
                error="上一轮尚未结束，已跳过本次触发。" if task.id in occupied else None,
            )
            self._store.append_run(run)
            occupied.add(task.id)
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
        active_count = len(self._processes)
        available = max(self._max_workers - active_count, 0)
        if available <= 0:
            return
        pending_runs = [run for run in self._store.active_runs() if run.status == ScheduleRunStatus.PENDING]
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
        if not task.user_id or not task.agent_id:
            self._fail_unstarted_run(run, "定时任务缺少稳定的用户、Agent 或 revision 归属，未启动 worker。")
            return
        bundle_path: Path | None = None
        stdout = None
        stderr = None
        try:
            if hasattr(self._profile_provider, "get_active_profile_snapshot"):
                from codepilot.scheduler.service import validate_schedule_task_payload
                values = validate_schedule_task_payload(settings=self._settings, agent_profiles=None,
                    payload=task.model_dump(), profile_resolver=self._profile_provider.get_active_profile_snapshot,
                    user_id=task.user_id)
                task = task.model_copy(update=values)
            bundle_path = self._write_execution_bundle(run.id, task)
            from codepilot.session.agents import AgentProfile
            self.execution_profiles[run.id] = AgentProfile.model_validate(json.loads(bundle_path.read_text())["profile"])
            run = run.model_copy(update={"user_id": task.user_id, "agent_id": task.agent_id,
                "revision_id": task.revision_id, "session_id": f"sess_{uuid4().hex}",
                "provider": task.provider, "model": task.model, "working_dir": task.working_dir,
                "max_iterations": json.loads(bundle_path.read_text())["profile"]["max_iterations"]})
            _atomic_write(self.token_path(run.id), secrets.token_urlsafe(32))
            self._store.update_run(run)
            if self._session_memory is not None:
                from codepilot.events import SessionMetaEvent, SessionLifecycleEvent
                await self._session_memory.handle_domain_event(SessionMetaEvent(
                    user_id=task.user_id, agent_id=task.agent_id, session_id=run.session_id,
                    run_id=run.id, revision_id=task.revision_id, created_at=to_iso(utc_now()),
                    data={"user_id": task.user_id, "agent_id": task.agent_id, "agent_name": task.agent_name,
                          "session_id": run.session_id, "status": "RUNNING", "source": "schedule",
                          "schedule_task_id": task.id, "schedule_run_id": run.id, "schedule_task_name": task.name,
                          "provider": task.provider, "model": task.model, "workspace_path": task.working_dir,
                          "workspace_id": getattr(self._workspace, "workspace_id", "schedule"), "title": task.name},
                ))
                await self._session_memory.handle_domain_event(SessionLifecycleEvent(
                    user_id=task.user_id, agent_id=task.agent_id, session_id=run.session_id,
                    run_id=run.id, revision_id=task.revision_id, created_at=to_iso(utc_now()), status="RUNNING",
                    data={"agent_name": task.agent_name, "provider": task.provider, "model": task.model},
                ))
            stdout = stdout_path.open("ab")
            stderr = stderr_path.open("ab")
        except Exception:  # 配置详情和本地路径不得写入持久化错误。
            if stdout is not None:
                stdout.close()
            if stderr is not None:
                stderr.close()
            if bundle_path is not None:
                self._delete_prompt_file(bundle_path)
            self._fail_unstarted_run(run, "定时执行配置或存储不可用，请检查 Agent 依赖、模型及工作目录。")
            await self._finish_session(self._store.get_run(run.id))
            self.execution_profiles.pop(run.id, None)
            self.token_path(run.id).unlink(missing_ok=True)
            return
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
            str(self.token_path(run.id)),
            "--session-id", run.session_id,
        ]
        args[5:5] = [
            "--execution-bundle-file", str(bundle_path),
            "--user-id", task.user_id,
            "--agent-id", task.agent_id,
            "--revision-id", task.revision_id,
        ]
        try:
            process = await asyncio.create_subprocess_exec(*args, stdout=stdout, stderr=stderr, cwd=task.working_dir, start_new_session=True)
        except Exception as exc:  # noqa: BLE001
            stdout.close()
            stderr.close()
            self._delete_prompt_file(bundle_path)
            self.token_path(run.id).unlink(missing_ok=True)
            self.execution_profiles.pop(run.id, None)
            self._store.update_run(
                run.model_copy(
                    update={
                        "status": ScheduleRunStatus.FAILED,
                        "finished_at": to_iso(utc_now()),
                        "error": "无法创建定时执行进程，请检查服务运行环境。",
                    }
                )
            )
            await self._finish_session(self._store.get_run(run.id))
            return
        running = run.model_copy(update={"status": ScheduleRunStatus.RUNNING, "started_at": to_iso(utc_now()), "pid": process.pid, "worker_exited": False, "phase": "starting"})
        self._store.update_run(running)
        self._processes[run.id] = process
        monitor = asyncio.create_task(
            self._monitor_process(run.id, process, stdout_path, stderr_path, bundle_path, stdout, stderr),
            name=f"codepilot-schedule-monitor-{run.id}",
        )
        self._monitor_tasks.add(monitor)
        monitor.add_done_callback(self._monitor_tasks.discard)

    def _fail_unstarted_run(self, run: ScheduleRun, error: str) -> None:
        self._store.update_run(
            run.model_copy(
                update={
                    "status": ScheduleRunStatus.FAILED,
                    "finished_at": to_iso(utc_now()),
                    "pid": None,
                    "error": error,
                }
            )
        )

    def _write_execution_bundle(self, run_id: str, task: ScheduleTask) -> Path:
        prompt_dir = self._store.workspace_dir / "schedule_prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{run_id}.{secrets.token_urlsafe(12)}.bundle.json"
        if self._profile_provider is not None:
            default_directory = getattr(self._profile_provider, "workspace_path", None)
            if default_directory is not None:
                from codepilot.session.working_directory import resolve_working_directory
                resolve_working_directory(self._profile_provider.settings, default_directory, task.working_dir)
            profile = self._profile_provider.get_profile_revision_snapshot(task.user_id, task.agent_id, task.revision_id)
        else:
            profile = self._agent_profiles.get(task.agent_name)
        if profile is None or profile.revision_id != task.revision_id:
            raise ValueError("定时任务引用的 Agent revision 不可用")
        prompt_path.write_text(
            json.dumps({"schema_version": 5, "prompt": task.prompt, "profile": profile.model_dump(mode="json")}, ensure_ascii=False),
            encoding="utf-8",
        )
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
                await self._mark_running_process_finished(
                    run_id,
                    ScheduleRunStatus.TIMEOUT,
                    f"执行超过 {self._worker_timeout_seconds} 秒，已终止；已发出的外部操作可能仍然生效。",
                )
                await self._terminate(process)
                return
            run = self._store.get_run(run_id)
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                return
            if run.status == ScheduleRunStatus.STOPPING:
                await self._mark_running_process_finished(run_id, ScheduleRunStatus.CANCELLED, "用户已停止本轮执行；已发出的外部操作可能仍然生效。")
                return
            if self._session_memory is not None and run.session_id:
                replay = await self._session_memory.replay(run.user_id, run.session_id)
                facts = (replay.get("session") or {}).get("data") or {}
                if facts.get("run_id") == run.id and facts.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
                    reason = facts.get("stop_reason")
                    await self.report(run.id, status=ScheduleRunStatus[facts["status"]], session_id=run.session_id,
                                      summary=None, error="已停止：达到轮数上限" if reason == "max_iterations" else None,
                                      report_seq=run.report_seq + 1, stop_reason=reason)
                    return
            if return_code == 0:
                await self._mark_running_process_finished(run_id, ScheduleRunStatus.FAILED, "worker 退出但未上报执行结果。")
            else:
                await self._mark_running_process_finished(
                    run_id,
                    ScheduleRunStatus.FAILED,
                    "定时进程异常退出，请检查服务运行日志。",
                )
        finally:
            # 等待进程树回收完成后才允许原会话接收人工新 Run。
            cleanup = self._terminations.get(process.pid)
            if cleanup is not None:
                await asyncio.shield(cleanup)
                self._terminations.pop(process.pid, None)
            stdout.close()
            stderr.close()
            self._delete_prompt_file(prompt_path)
            self._processes.pop(run_id, None)
            self.token_path(run_id).unlink(missing_ok=True)
            self.execution_profiles.pop(run_id, None)
            run = self._store.get_run(run_id)
            if run:
                await self._finish_session(run)
                self._store.update_run(run.model_copy(update={"worker_exited": True, "phase": "finished", "pid": None}))

    async def _finish_session(self, run: ScheduleRun) -> None:
        if self._session_memory is None or not run.session_id:
            return
        from codepilot.events import SessionLifecycleEvent
        status = run.status.name if run.status in {ScheduleRunStatus.COMPLETED, ScheduleRunStatus.FAILED, ScheduleRunStatus.CANCELLED} else "CANCELLED"
        # 只有确认 worker 退出后主进程才能写入终态，避免两个进程竞争会话文件。
        await self._session_memory.handle_domain_event(SessionLifecycleEvent(
            user_id=run.user_id, agent_id=run.agent_id, session_id=run.session_id,
            run_id=run.id, revision_id=run.revision_id, created_at=to_iso(utc_now()), status=status,
            data={"stop_reason": run.stop_reason or ("timeout" if run.status == ScheduleRunStatus.TIMEOUT else None),
                  "provider": run.provider, "model": run.model},
        ))

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
                        "external_effect_uncertain": status in {ScheduleRunStatus.TIMEOUT, ScheduleRunStatus.INTERRUPTED, ScheduleRunStatus.CANCELLED},
                    }
                )
            )
