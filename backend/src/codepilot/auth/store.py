from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.auth.models import AuthenticatedSession, AuthTokens, UserPrincipal, UserRole

AUTH_SCHEMA_VERSION = 1
SCRYPT_PARAMS = {"name": "scrypt", "version": 1, "n": 2**15, "r": 8, "p": 3, "dklen": 32}
MAX_PASSWORD_CHARS = 1024
MIN_PASSWORD_CHARS = 4


class AuthStoreError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class AuthStore:
    """单进程认证仓库；密码慢哈希只在账号变更和登录时执行。"""

    def __init__(self, path: Path, *, idle_timeout_seconds: int, absolute_timeout_seconds: int) -> None:
        self.path = path.expanduser().resolve()
        self.idle_timeout_seconds = idle_timeout_seconds
        self.absolute_timeout_seconds = absolute_timeout_seconds
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._initialize()

    def has_users(self) -> bool:
        with self._connect() as connection:
            return bool(connection.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    def create_user(self, username: str, password: str, *, role: UserRole) -> UserPrincipal:
        display, normalized = normalize_username(username)
        validate_password(password)
        salt = secrets.token_bytes(16)
        password_hash = hash_password(password, salt, SCRYPT_PARAMS)
        now = int(time.time())
        user_id = str(uuid4())
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO users
                       (user_id, username, username_normalized, role, enabled, password_hash, password_salt,
                        password_params, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                    (user_id, display, normalized, role.value, password_hash, salt, json.dumps(SCRYPT_PARAMS), now, now),
                )
                self._audit(connection, "user.created", actor_user_id=user_id, target_user_id=user_id, data={"role": role.value})
        except sqlite3.IntegrityError as exc:
            raise AuthStoreError("用户名已存在", code="username_conflict") from exc
        return UserPrincipal(user_id=user_id, username=display, role=role)

    def verify_password(self, username: str, password: str) -> UserPrincipal | None:
        try:
            _, normalized = normalize_username(username)
        except AuthStoreError:
            return None
        if not isinstance(password, str) or len(password) > MAX_PASSWORD_CHARS:
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE username_normalized = ?", (normalized,)).fetchone()
        if row is None:
            # 未知用户也执行一次固定参数计算，减少用户名枚举的时序差异。
            hash_password(password or "invalid-password", b"\0" * 16, SCRYPT_PARAMS)
            return None
        params = json.loads(row["password_params"])
        candidate = hash_password(password, bytes(row["password_salt"]), params)
        if not secrets.compare_digest(candidate, bytes(row["password_hash"])) or not bool(row["enabled"]):
            return None
        return _principal_from_row(row)

    def create_session(self, principal: UserPrincipal) -> AuthTokens:
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = int(time.time())
        absolute_expires_at = now + self.absolute_timeout_seconds
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO auth_sessions
                   (session_id, token_hash, csrf_hash, user_id, created_at, last_seen_at,
                    idle_expires_at, absolute_expires_at, revoked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    str(uuid4()),
                    _token_hash(session_token),
                    _token_hash(csrf_token),
                    principal.user_id,
                    now,
                    now,
                    now + self.idle_timeout_seconds,
                    absolute_expires_at,
                ),
            )
            self._audit(connection, "session.created", actor_user_id=principal.user_id, target_user_id=principal.user_id)
        return AuthTokens(
            session_token=session_token,
            csrf_token=csrf_token,
            absolute_expires_at=absolute_expires_at,
        )

    def authenticate(self, session_token: str) -> AuthenticatedSession | None:
        if not session_token or len(session_token) > 256:
            return None
        now = int(time.time())
        token_hash = _token_hash(session_token)
        with self._connect() as connection:
            row = connection.execute(
                """SELECT s.*, u.username, u.role, u.enabled
                   FROM auth_sessions s JOIN users u ON u.user_id = s.user_id
                   WHERE s.token_hash = ?""",
                (token_hash,),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or not bool(row["enabled"])
                or int(row["idle_expires_at"]) <= now
                or int(row["absolute_expires_at"]) <= now
            ):
                return None
            # 最多五分钟写一次活跃时间，避免每个 API 请求都触发 SQLite 写锁。
            if int(row["last_seen_at"]) <= now - 300:
                next_idle = min(now + self.idle_timeout_seconds, int(row["absolute_expires_at"]))
                connection.execute(
                    "UPDATE auth_sessions SET last_seen_at = ?, idle_expires_at = ? WHERE session_id = ?",
                    (now, next_idle, row["session_id"]),
                )
        principal = UserPrincipal(user_id=row["user_id"], username=row["username"], role=UserRole(row["role"]))
        return AuthenticatedSession(
            session_id=row["session_id"],
            principal=principal,
            csrf_hash=row["csrf_hash"],
            absolute_expires_at=int(row["absolute_expires_at"]),
        )

    def validate_csrf(self, authenticated: AuthenticatedSession, *, cookie_token: str, header_token: str) -> bool:
        if not cookie_token or not header_token or len(cookie_token) > 256 or len(header_token) > 256:
            return False
        return secrets.compare_digest(cookie_token, header_token) and secrets.compare_digest(
            _token_hash(header_token), authenticated.csrf_hash
        )

    def revoke_session(self, session_id: str, *, actor_user_id: str | None = None) -> None:
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute("SELECT user_id FROM auth_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                return
            connection.execute("UPDATE auth_sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL", (now, session_id))
            self._audit(connection, "session.revoked", actor_user_id=actor_user_id or row["user_id"], target_user_id=row["user_id"])

    def revoke_user_sessions(self, user_id: str, *, actor_user_id: str | None = None) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now, user_id))
            self._audit(connection, "sessions.revoked", actor_user_id=actor_user_id, target_user_id=user_id)

    def set_password(self, user_id: str, password: str, *, actor_user_id: str | None = None) -> None:
        validate_password(password)
        salt = secrets.token_bytes(16)
        digest = hash_password(password, salt, SCRYPT_PARAMS)
        now = int(time.time())
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is None:
                raise AuthStoreError("用户不存在", code="user_not_found")
            connection.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, password_params = ?, updated_at = ? WHERE user_id = ?",
                (digest, salt, json.dumps(SCRYPT_PARAMS), now, user_id),
            )
            connection.execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now, user_id))
            self._audit(connection, "user.password_changed", actor_user_id=actor_user_id, target_user_id=user_id)

    def set_enabled(self, user_id: str, enabled: bool, *, actor_user_id: str | None = None) -> None:
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                raise AuthStoreError("用户不存在", code="user_not_found")
            if not enabled and row["role"] == UserRole.ADMIN.value:
                active_admins = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role = ? AND enabled = 1", (UserRole.ADMIN.value,)
                ).fetchone()[0]
                if active_admins <= 1:
                    raise AuthStoreError("不能禁用最后一个管理员", code="last_admin_forbidden")
            connection.execute("UPDATE users SET enabled = ?, updated_at = ? WHERE user_id = ?", (int(enabled), now, user_id))
            if not enabled:
                connection.execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now, user_id))
            self._audit(
                connection,
                "user.enabled" if enabled else "user.disabled",
                actor_user_id=actor_user_id,
                target_user_id=user_id,
            )

    def find_user(self, identifier: str) -> UserPrincipal:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE user_id = ? OR username_normalized = ?",
                (identifier, identifier.casefold()),
            ).fetchone()
        if row is None:
            raise AuthStoreError("用户不存在", code="user_not_found")
        return _principal_from_row(row)

    def find_enabled_user(self, identifier: str) -> UserPrincipal:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE (user_id = ? OR username_normalized = ?) AND enabled = 1",
                (identifier, identifier.casefold()),
            ).fetchone()
        if row is None:
            raise AuthStoreError("用户不存在或已禁用", code="user_disabled")
        return _principal_from_row(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id, username, role, enabled, created_at, updated_at FROM users ORDER BY username_normalized"
            ).fetchall()
        return [dict(row) for row in rows]

    def user_enabled_states(self) -> dict[str, bool]:
        with self._connect() as connection:
            rows = connection.execute("SELECT user_id, enabled FROM users").fetchall()
        return {str(row["user_id"]): bool(row["enabled"]) for row in rows}

    def record_audit(
        self,
        event_type: str,
        *,
        actor_user_id: str | None = None,
        target_user_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            self._audit(
                connection,
                event_type,
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                data=data,
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_normalized TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    password_hash BLOB NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_params TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(user_id),
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    idle_expires_at INTEGER NOT NULL,
                    absolute_expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id);
                CREATE TABLE IF NOT EXISTS auth_audit_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    actor_user_id TEXT,
                    target_user_id TEXT,
                    created_at INTEGER NOT NULL,
                    data_json TEXT NOT NULL
                );
                """
            )
            existing = connection.execute("SELECT value FROM auth_meta WHERE key = 'schema_version'").fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO auth_meta(key, value) VALUES ('schema_version', ?)", (str(AUTH_SCHEMA_VERSION),)
                )
            elif int(existing["value"]) != AUTH_SCHEMA_VERSION:
                raise AuthStoreError("认证数据库版本不受支持", code="auth_schema_unsupported")
        self._secure_files()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()
            self._secure_files()

    def _secure_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.path}{suffix}")
            if path.exists():
                os.chmod(path, 0o600)

    def _audit(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        *,
        actor_user_id: str | None,
        target_user_id: str | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO auth_audit_events VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid4()), event_type, actor_user_id, target_user_id, int(time.time()), json.dumps(data or {})),
        )


def normalize_username(value: str) -> tuple[str, str]:
    display = str(value).strip()
    if not 3 <= len(display) <= 64 or any(char.isspace() for char in display):
        raise AuthStoreError("用户名长度必须为 3 到 64 且不能包含空白", code="username_invalid")
    return display, display.casefold()


def validate_password(value: str) -> None:
    if not isinstance(value, str) or not MIN_PASSWORD_CHARS <= len(value) <= MAX_PASSWORD_CHARS:
        raise AuthStoreError(
            f"密码长度必须为 {MIN_PASSWORD_CHARS} 到 {MAX_PASSWORD_CHARS}", code="password_invalid"
        )


def hash_password(password: str, salt: bytes, params: dict[str, Any]) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=int(params["n"]),
        r=int(params["r"]),
        p=int(params["p"]),
        maxmem=64 * 1024 * 1024,
        dklen=int(params["dklen"]),
    )


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _principal_from_row(row: sqlite3.Row) -> UserPrincipal:
    return UserPrincipal(user_id=row["user_id"], username=row["username"], role=UserRole(row["role"]))
