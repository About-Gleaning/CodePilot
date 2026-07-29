from __future__ import annotations

"""跨进程 workspace 写入租约。"""

import asyncio
import fcntl
import json
import os
from pathlib import Path
from typing import Any


class WorkspaceWriteBusy(RuntimeError):
    """workspace 已被另一个 Run 持有写入租约。"""


class WorkspaceWriteLeaseManager:
    def __init__(self, workspace_dir: Path) -> None:
        self.path = Path(workspace_dir) / "workspace-write.lock"
        self._guard = asyncio.Lock()
        self._owner_key: tuple[str, str, str] | None = None
        self._fd: int | None = None

    async def acquire(self, run_ref: Any) -> None:
        key = _run_key(run_ref)
        async with self._guard:
            if self._owner_key == key:
                return
            if self._owner_key is not None:
                raise WorkspaceWriteBusy("workspace 写入租约正被其他 Run 使用")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(fd)
                raise WorkspaceWriteBusy("workspace 写入租约正被其他进程使用") from exc
            try:
                payload = {
                    "agent_id": key[0],
                    "session_id": key[1],
                    "run_id": key[2],
                    "pid": os.getpid(),
                }
                os.ftruncate(fd, 0)
                os.write(fd, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                os.fsync(fd)
                os.chmod(self.path, 0o600)
            except Exception:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                raise
            self._owner_key = key
            self._fd = fd

    async def release(self, run_ref: Any) -> None:
        key = _run_key(run_ref)
        async with self._guard:
            if self._owner_key != key or self._fd is None:
                return
            fd = self._fd
            self._fd = None
            self._owner_key = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


_MANAGERS: dict[Path, WorkspaceWriteLeaseManager] = {}


def get_workspace_write_lease_manager(workspace_dir: Path) -> WorkspaceWriteLeaseManager:
    key = Path(workspace_dir).resolve()
    manager = _MANAGERS.get(key)
    if manager is None:
        manager = WorkspaceWriteLeaseManager(key)
        _MANAGERS[key] = manager
    return manager


def _run_key(run_ref: Any) -> tuple[str, str, str]:
    if run_ref is None:
        raise ValueError("workspace 变更工具缺少 RunRef")
    return str(run_ref.agent_id), str(run_ref.session_id), str(run_ref.run_id)
