from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from codepilot.auth.models import AuthenticatedSession, AuthTokens, UserPrincipal
from codepilot.auth.store import AuthStore


class AuthService:
    def __init__(self, store: AuthStore) -> None:
        self.store = store
        self._password_slots = asyncio.Semaphore(2)
        self._login_failures: dict[str, deque[float]] = defaultdict(deque)
        self._disable_listeners: list[Callable[[str], Awaitable[None]]] = []
        self._monitor_task: asyncio.Task[None] | None = None
        self._enabled_states: dict[str, bool] = {}

    async def login(self, username: str, password: str, *, client_key: str) -> tuple[UserPrincipal, AuthTokens] | None:
        limiter_key = f"{username.casefold()}:{client_key}"
        now = time.monotonic()
        failures = self._login_failures[limiter_key]
        while failures and failures[0] <= now - 15 * 60:
            failures.popleft()
        if len(failures) >= 5:
            await asyncio.to_thread(self.store.record_audit, "login.rate_limited", data={"client": client_key[:64]})
            return None
        async with self._password_slots:
            principal = await asyncio.to_thread(self.store.verify_password, username, password)
        if principal is None:
            failures.append(now)
            await asyncio.to_thread(self.store.record_audit, "login.failed", data={"client": client_key[:64]})
            return None
        self._login_failures.pop(limiter_key, None)
        tokens = await asyncio.to_thread(self.store.create_session, principal)
        return principal, tokens

    async def authenticate(self, session_token: str) -> AuthenticatedSession | None:
        return await asyncio.to_thread(self.store.authenticate, session_token)

    async def logout(self, session_id: str, *, actor_user_id: str) -> None:
        await asyncio.to_thread(self.store.revoke_session, session_id, actor_user_id=actor_user_id)

    def add_disable_listener(self, listener: Callable[[str], Awaitable[None]]) -> None:
        self._disable_listeners.append(listener)

    async def start(self, initial_states: dict[str, bool] | None = None) -> None:
        self._enabled_states = dict(initial_states) if initial_states is not None else await asyncio.to_thread(self.store.user_enabled_states)
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_users(), name="codepilot-auth-user-monitor")

    async def shutdown(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _monitor_users(self) -> None:
        while True:
            await asyncio.sleep(2)
            current = await asyncio.to_thread(self.store.user_enabled_states)
            disabled = [user_id for user_id, enabled in current.items() if not enabled and self._enabled_states.get(user_id, True)]
            self._enabled_states = current
            for user_id in disabled:
                await asyncio.gather(*(listener(user_id) for listener in self._disable_listeners), return_exceptions=True)
