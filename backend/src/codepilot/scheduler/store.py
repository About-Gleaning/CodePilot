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
    def __init__(self, workspace_dir: Path, *, token_file: Path | None = None, owner_user_id: str | None = None) -> None:
        self.workspace_dir = workspace_dir
        self.schedules_file = workspace_dir / "schedules.json"
        self.runs_file = workspace_dir / "schedule-runs.jsonl"
        self.token_file = token_file or workspace_dir / "schedule_worker_token"
        self.run_logs_dir = workspace_dir / "schedule-logs"
        self.owner_user_id = owner_user_id
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.run_logs_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_token_file()
        self._runs: dict[str, ScheduleRun] = {}
        self._runs_offset = 0
        self._runs_inode: int | None = None
        self._session_runs: dict[str, str] = {}
        self._active: dict[str, ScheduleRun] = {}

    def list_tasks(self) -> list[ScheduleTask]:
        if not self.schedules_file.exists():
            return []
        payload = json.loads(self.schedules_file.read_text(encoding="utf-8") or "[]")
        if not isinstance(payload, list):
            raise ValueError("schedules.json 必须是数组")
        tasks = [ScheduleTask.model_validate(item) for item in payload]
        if self.owner_user_id and any(task.user_id != self.owner_user_id for task in tasks):
            raise ValueError("Schedule owner 与用户分区不一致")
        return tasks

    def save_tasks(self, tasks: list[ScheduleTask]) -> None:
        data = [task.model_dump() for task in tasks]
        _atomic_write(self.schedules_file, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

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
        self._refresh_runs()
        self.runs_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.runs_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as file:
            file.write(json.dumps(run.model_dump(), ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())
        self._runs[run.id] = run.model_copy(deep=True)
        self._index_active(self._runs[run.id])
        if run.session_id:
            self._session_runs[run.session_id] = run.id
        self._runs_offset = self.runs_file.stat().st_size
        self._runs_inode = self.runs_file.stat().st_ino

    def list_run_snapshots(self) -> list[ScheduleRun]:
        if not self.runs_file.exists():
            return []
        runs: list[ScheduleRun] = []
        for line in self.runs_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                run = ScheduleRun.model_validate(json.loads(line))
                if self.owner_user_id and run.user_id != self.owner_user_id:
                    raise ValueError("ScheduleRun owner 与用户分区不一致")
                runs.append(run)
        return runs

    def list_runs(self) -> list[ScheduleRun]:
        self._refresh_runs()
        return sorted(self._runs.values(), key=lambda item: item.scheduled_at, reverse=True)

    def get_run(self, run_id: str) -> ScheduleRun | None:
        self._refresh_runs()
        return self._runs.get(run_id)

    def _refresh_runs(self) -> None:
        """单写者追加日志按偏移增量索引；截断或替换后重新建立索引。"""
        if not self.runs_file.exists():
            return
        stat = self.runs_file.stat()
        if stat.st_ino != self._runs_inode or stat.st_size < self._runs_offset:
            self._runs.clear()
            self._session_runs.clear()
            self._active.clear()
            self._runs_offset = 0
            self._runs_inode = stat.st_ino
        with self.runs_file.open("rb") as file:
            file.seek(self._runs_offset)
            for line in file:
                if not line.endswith(b"\n"):
                    raise ValueError("定时执行记录末行不完整，禁止继续写入")
                run = ScheduleRun.model_validate(json.loads(line))
                if self.owner_user_id and run.user_id != self.owner_user_id:
                    raise ValueError("ScheduleRun owner 与用户分区不一致")
                self._runs[run.id] = run
                self._index_active(run)
                if run.session_id:
                    self._session_runs[run.session_id] = run.id
                self._runs_offset += len(line)

    def session_run(self, session_id: str) -> ScheduleRun | None:
        self._refresh_runs()
        return self._runs.get(self._session_runs.get(session_id, ""))

    def update_run(self, run: ScheduleRun) -> None:
        self.append_run(run)

    def recent_runs(self, limit: int = 20) -> list[ScheduleRun]:
        return self.list_runs()[:limit]

    def active_runs(self) -> list[ScheduleRun]:
        self._refresh_runs()
        return list(self._active.values())

    def _index_active(self, run: ScheduleRun) -> None:
        # 调度每秒只检查活动执行，历史增长不增加调度循环的遍历成本。
        if run.status not in TERMINAL_RUN_STATUSES or not run.worker_exited:
            self._active[run.id] = run
        else:
            self._active.pop(run.id, None)

    def cancel_pending_runs_for_task(self, task_id: str) -> list[ScheduleRun]:
        cancelled: list[ScheduleRun] = []
        for run in self.active_runs():
            if run.task_id != task_id or run.status != ScheduleRunStatus.PENDING:
                continue
            updated = run.model_copy(
                update={
                    "status": ScheduleRunStatus.CANCELLED,
                    "finished_at": to_iso(utc_now()),
                    "error": "任务已停用或删除，未启动的执行已取消。",
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


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


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


def worker_descendants(pid: int) -> dict[int, str]:
    """停止边界记录子进程出生身份，包含另建进程组的 Bash 和 Hook。"""
    table = _process_table()
    children: dict[int, list[int]] = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    pending = [pid]
    result: dict[int, str] = {}
    while pending:
        for child in children.get(pending.pop(), []):
            result[child] = table[child][1]
            pending.append(child)
    return result


def reap_worker_descendants(identities: dict[int, str]) -> None:
    if not identities:
        return
    table = _process_table()
    for pid, birth in identities.items():
        if table.get(pid, (None, None))[1] != birth:
            continue
        try:
            # 只回收出生身份仍匹配的子进程，不向可能复用的进程组广播。
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def _process_table() -> dict[int, tuple[int, str]]:
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart="], capture_output=True, text=True, timeout=3, check=True)
    table = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3:
            table[int(fields[0])] = (int(fields[1]), fields[2])
    return table
