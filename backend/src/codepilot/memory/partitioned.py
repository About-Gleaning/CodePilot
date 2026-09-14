from __future__ import annotations

from pathlib import Path
from uuid import UUID

from codepilot.events import DomainEvent, StreamEvent
from codepilot.memory.jsonl import JsonlEventStore, JsonlSessionMemory


class UserPartitionedSessionMemory:
    """按可信 user_id 路由 Session JSONL，并保留记录级 owner 校验。"""

    def __init__(self, workspace_dir: Path) -> None:
        self._workspace_dir = workspace_dir.resolve()
        self._stores: dict[str, JsonlSessionMemory] = {}

    async def handle_domain_event(self, event: DomainEvent) -> None:
        user_id = _required_user_id(event.user_id)
        await self._store(user_id).handle_domain_event(event)

    async def replay(self, user_id: str, session_id: str | None = None) -> dict[str, object]:
        replay = await self._store(user_id).replay(session_id)
        data = ((replay.get("session") or {}).get("data") or {}) if isinstance(replay, dict) else {}
        owner = data.get("user_id") if isinstance(data, dict) else None
        if owner not in {None, "", user_id}:
            raise ValueError("Session owner 与用户分区不一致")
        return replay

    def list_sessions(self, user_id: str) -> list[dict[str, object]]:
        sessions = self._store(user_id).list_sessions()
        return [item for item in sessions if item.get("user_id") in {None, "", user_id}]

    def _store(self, user_id: str) -> JsonlSessionMemory:
        normalized = _required_user_id(user_id)
        store = self._stores.get(normalized)
        if store is None:
            store = JsonlSessionMemory(self._user_sessions_dir(normalized))
            self._stores[normalized] = store
        return store

    def _user_sessions_dir(self, user_id: str) -> Path:
        target = (self._workspace_dir / "users" / user_id / "sessions").resolve()
        if not target.is_relative_to(self._workspace_dir):
            raise ValueError("用户 Session 目录越界")
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        return target


class UserPartitionedEventStore:
    def __init__(self, workspace_dir: Path) -> None:
        self._workspace_dir = workspace_dir.resolve()
        self._stores: dict[str, JsonlEventStore] = {}

    async def append(self, event: StreamEvent) -> None:
        user_id = _required_user_id(event.user_id)
        await self._store(user_id).append(event)

    def replay(self, user_id: str, session_id: str | None = None, after_seq: int = 0) -> list[StreamEvent]:
        return self._store(user_id).replay(session_id=session_id, after_seq=after_seq)

    def latest_seq(self, user_id: str | None = None, session_id: str | None = None) -> int:
        if user_id is not None:
            return self._store(user_id).latest_seq(session_id)
        latest = 0
        users_dir = self._workspace_dir / "users"
        if not users_dir.is_dir():
            return latest
        for child in users_dir.iterdir():
            sessions_dir = child / "sessions"
            if child.is_dir() and sessions_dir.is_dir():
                latest = max(latest, JsonlEventStore(sessions_dir).latest_seq())
        return latest

    def known_user_ids(self) -> list[str]:
        users_dir = self._workspace_dir / "users"
        if not users_dir.is_dir():
            return []
        return sorted(path.name for path in users_dir.iterdir() if path.is_dir())

    def _store(self, user_id: str) -> JsonlEventStore:
        normalized = _required_user_id(user_id)
        store = self._stores.get(normalized)
        if store is None:
            sessions_dir = (self._workspace_dir / "users" / normalized / "sessions").resolve()
            if not sessions_dir.is_relative_to(self._workspace_dir):
                raise ValueError("用户 Event 目录越界")
            sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            store = JsonlEventStore(sessions_dir)
            self._stores[normalized] = store
        return store


def _required_user_id(value: str | None) -> str:
    try:
        normalized = str(UUID(value or ""))
    except ValueError as exc:
        raise ValueError("用户事件缺少可信 UUID user_id") from exc
    if normalized != value:
        raise ValueError("用户事件缺少可信 user_id")
    return normalized
