from __future__ import annotations

"""定时任务持久化。

任务配置使用整体 JSON 原子替换；运行记录使用 JSONL 追加状态快照。这样既能
安全更新任务列表，也能保留 run 状态变化轨迹，便于服务重启后恢复最新状态。
"""

import json
import os
import secrets
import subprocess
from pathlib import Path

from codepilot.scheduler.models import ScheduleRun, ScheduleRunStatus, ScheduleTask, TERMINAL_RUN_STATUSES, to_iso, utc_now


class ScheduleStore:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.schedules_file = workspace_dir / "schedules.json"
        self.runs_file = workspace_dir / "schedule_runs.jsonl"
        self.token_file = workspace_dir / "schedule_worker_token"
        self.run_logs_dir = workspace_dir / "logs" / "schedule_runs"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.run_logs_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_token_file()

    def list_tasks(self) -> list[ScheduleTask]:
        if not self.schedules_file.exists():
            return []
        payload = json.loads(self.schedules_file.read_text(encoding="utf-8") or "[]")
        if not isinstance(payload, list):
            raise ValueError("schedules.json 必须是数组")
        return [ScheduleTask.model_validate(item) for item in payload]

    def save_tasks(self, tasks: list[ScheduleTask]) -> None:
        data = [task.model_dump() for task in tasks]
        temp_path = self.schedules_file.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.schedules_file)

    def upsert_task(self, task: ScheduleTask) -> None:
        tasks = [item for item in self.list_tasks() if item.id != task.id]
        tasks.append(task)
        self.save_tasks(sorted(tasks, key=lambda item: item.created_at))

    def delete_task(self, task_id: str) -> bool:
        tasks = self.list_tasks()
        kept = [item for item in tasks if item.id != task_id]
        if len(kept) == len(tasks):
            return False
        self.save_tasks(kept)
        return True

    def get_task(self, task_id: str) -> ScheduleTask | None:
        return next((task for task in self.list_tasks() if task.id == task_id), None)

    def append_run(self, run: ScheduleRun) -> None:
        self.runs_file.parent.mkdir(parents=True, exist_ok=True)
        with self.runs_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(run.model_dump(), ensure_ascii=False) + "\n")

    def list_run_snapshots(self) -> list[ScheduleRun]:
        if not self.runs_file.exists():
            return []
        runs: list[ScheduleRun] = []
        for line in self.runs_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                runs.append(ScheduleRun.model_validate(json.loads(line)))
        return runs

    def list_runs(self) -> list[ScheduleRun]:
        latest: dict[str, ScheduleRun] = {}
        for run in self.list_run_snapshots():
            latest[run.id] = run
        return sorted(latest.values(), key=lambda item: item.scheduled_at, reverse=True)

    def get_run(self, run_id: str) -> ScheduleRun | None:
        return next((run for run in self.list_runs() if run.id == run_id), None)

    def update_run(self, run: ScheduleRun) -> None:
        self.append_run(run)

    def recent_runs(self, limit: int = 20) -> list[ScheduleRun]:
        return self.list_runs()[:limit]

    def active_runs(self) -> list[ScheduleRun]:
        return [
            run
            for run in self.list_runs()
            if run.status in {ScheduleRunStatus.PENDING, ScheduleRunStatus.RUNNING}
        ]

    def cancel_pending_runs_for_task(self, task_id: str) -> list[ScheduleRun]:
        cancelled: list[ScheduleRun] = []
        for run in self.list_runs():
            if run.task_id != task_id or run.status != ScheduleRunStatus.PENDING:
                continue
            updated = run.model_copy(
                update={
                    "status": ScheduleRunStatus.CANCELLED,
                    "finished_at": to_iso(utc_now()),
                    "error": "任务已删除，未启动的运行记录已取消。",
                }
            )
            self.update_run(updated)
            cancelled.append(updated)
        return cancelled

    def recover_running_runs(self) -> list[ScheduleRun]:
        """服务启动时把已失去进程的 running run 标记为 interrupted。"""
        recovered: list[ScheduleRun] = []
        for run in self.list_runs():
            if run.status != ScheduleRunStatus.RUNNING:
                continue
            if run.pid and _is_matching_schedule_worker(run.pid, run.id):
                continue
            updated = run.model_copy(
                update={
                    "status": ScheduleRunStatus.INTERRUPTED,
                    "finished_at": to_iso(utc_now()),
                    "error": "服务重启后未确认对应 worker 进程仍在运行，已标记为 interrupted。",
                }
            )
            self.update_run(updated)
            recovered.append(updated)
        return recovered

    def token(self) -> str:
        return self.token_file.read_text(encoding="utf-8").strip()

    def _ensure_token_file(self) -> None:
        if self.token_file.exists():
            return
        self.token_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        try:
            self.token_file.chmod(0o600)
        except OSError:
            # chmod 失败不影响本地开发可用性；文件仍位于 CodePilot 私有运行目录下。
            pass


def _is_matching_schedule_worker(pid: int, run_id: str) -> bool:
    argv = _process_command_line(pid)
    if not argv or "codepilot.scheduler.worker" not in argv:
        return False
    try:
        run_id_index = argv.index("--run-id") + 1
    except ValueError:
        return False
    return run_id_index < len(argv) and argv[run_id_index] == run_id


def _process_command_line(pid: int) -> list[str] | None:
    if pid <= 0:
        return None
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes()
        except OSError:
            return None
        return [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # ps 兜底无法可靠还原带空格参数；这里只用于确认 worker 模块和 run_id 这类稳定标识。
    return result.stdout.strip().split()
