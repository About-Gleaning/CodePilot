from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any

from codepilot.scheduler.models import ScheduleRunStatus, to_iso, utc_now
from codepilot.scheduler.runner import ScheduleRunner
from codepilot.scheduler.store import ScheduleStore


class UserScheduleCoordinator:
    """按用户分区调度状态，并通过 run_id 反查可信 worker 归属。"""

    def __init__(self, *, settings: Any, workspace: Any, profile_provider: Any) -> None:
        self.settings = settings
        self.workspace = workspace
        self.profile_provider = profile_provider
        self._stores: dict[str, ScheduleStore] = {}
        self._runners: dict[str, ScheduleRunner] = {}
        self._lock = asyncio.Lock()
        self.token_file = workspace.workspace_dir / "schedule-worker-token"
        if not self.token_file.exists():
            self.token_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)

    async def start(self, enabled_user_ids: set[str]) -> None:
        users_dir = self.workspace.workspace_dir / "users"
        if users_dir.is_dir():
            for path in users_dir.iterdir():
                if path.is_dir():
                    if path.name in enabled_user_ids:
                        await self.runner(path.name).start()
                    else:
                        await self.disable_user(path.name)

    async def shutdown(self) -> None:
        await asyncio.gather(*(runner.shutdown() for runner in self._runners.values()), return_exceptions=True)

    def store(self, user_id: str) -> ScheduleStore:
        store = self._stores.get(user_id)
        if store is None:
            user_workspace = self.workspace.for_user(user_id)
            store = ScheduleStore(user_workspace.user_runtime_dir, token_file=self.token_file, owner_user_id=user_id)
            self._stores[user_id] = store
        return store

    def runner(self, user_id: str) -> ScheduleRunner:
        runner = self._runners.get(user_id)
        if runner is None:
            runner = ScheduleRunner(
                store=self.store(user_id),
                settings=self.settings,
                workspace=self.workspace,
                agent_profiles={},
                user_id=user_id,
                profile_provider=self.profile_provider,
            )
            self._runners[user_id] = runner
        return runner

    async def ensure_started(self, user_id: str) -> ScheduleRunner:
        runner = self.runner(user_id)
        await runner.start()
        return runner

    def token(self) -> str:
        return self.token_file.read_text(encoding="utf-8").strip()

    async def report(
        self,
        run_id: str,
        *,
        status: ScheduleRunStatus,
        session_id: str | None,
        summary: str | None,
        error: str | None,
    ) -> Any:
        async with self._lock:
            for user_id in self._known_user_ids():
                if self.store(user_id).get_run(run_id) is not None:
                    return await self.runner(user_id).report(
                        run_id,
                        status=status,
                        session_id=session_id,
                        summary=summary,
                        error=error,
                    )
        raise ValueError(f"run `{run_id}` 不存在")

    async def disable_user(self, user_id: str) -> None:
        runner = self._runners.get(user_id)
        if runner is not None:
            await runner.shutdown(
                status=ScheduleRunStatus.CANCELLED,
                reason="用户已禁用，Schedule worker 已终止。",
            )
        store = self.store(user_id)
        tasks = store.list_tasks()
        updated_at = to_iso(utc_now())
        updated = [
            task.model_copy(update={"enabled": False, "next_run_at": None, "updated_at": updated_at})
            if task.enabled else task
            for task in tasks
        ]
        if updated != tasks:
            store.save_tasks(updated)

    def _known_user_ids(self) -> list[str]:
        result = set(self._stores)
        users_dir = self.workspace.workspace_dir / "users"
        if users_dir.is_dir():
            result.update(path.name for path in users_dir.iterdir() if path.is_dir())
        return sorted(result)
