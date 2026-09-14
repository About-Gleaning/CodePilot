from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from codepilot.auth import AuthService, AuthStore, AuthStoreError, UserRole


def build_store(tmp_path: Path) -> AuthStore:
    return AuthStore(tmp_path / "auth.sqlite3", idle_timeout_seconds=43_200, absolute_timeout_seconds=604_800)


def test_auth_store_creates_secure_database_and_case_insensitive_unique_user(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    principal = store.create_user("Alice", "correct horse battery", role=UserRole.ADMIN)

    assert principal.username == "Alice"
    assert principal.role == UserRole.ADMIN
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert os.stat(store.path.parent).st_mode & 0o777 == 0o700
    with pytest.raises(AuthStoreError) as error:
        store.create_user("alice", "another correct password", role=UserRole.USER)
    assert error.value.code == "username_conflict"


def test_password_hash_is_versioned_and_plaintext_is_not_stored(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    password = "correct horse battery"
    principal = store.create_user("alice", password, role=UserRole.USER)

    assert store.verify_password("ALICE", password) == principal
    assert store.verify_password("alice", "incorrect password") is None
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT password_hash, password_params FROM users").fetchone()
    assert password.encode() not in bytes(row[0])
    assert '"name": "scrypt"' in row[1]


def test_password_minimum_length_is_four_characters(tmp_path: Path) -> None:
    store = build_store(tmp_path)

    principal = store.create_user("alice", "1234", role=UserRole.USER)

    assert store.verify_password("alice", "1234") == principal
    with pytest.raises(AuthStoreError) as error:
        store.create_user("bob", "123", role=UserRole.USER)
    assert error.value.code == "password_invalid"


def test_session_uses_digest_and_validates_bound_csrf(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    principal = store.create_user("alice", "correct horse battery", role=UserRole.USER)
    tokens = store.create_session(principal)
    authenticated = store.authenticate(tokens.session_token)

    assert authenticated is not None
    assert authenticated.principal == principal
    assert store.validate_csrf(
        authenticated,
        cookie_token=tokens.csrf_token,
        header_token=tokens.csrf_token,
    )
    assert not store.validate_csrf(authenticated, cookie_token=tokens.csrf_token, header_token="forged")
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT token_hash, csrf_hash FROM auth_sessions").fetchone()
    assert row[0] != tokens.session_token
    assert row[1] != tokens.csrf_token


def test_disabling_user_revokes_sessions_and_preserves_last_admin(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    admin = store.create_user("admin", "correct horse battery", role=UserRole.ADMIN)
    user = store.create_user("alice", "another correct password", role=UserRole.USER)
    tokens = store.create_session(user)

    store.set_enabled(user.user_id, False, actor_user_id=admin.user_id)
    assert store.authenticate(tokens.session_token) is None
    with pytest.raises(AuthStoreError) as disabled:
        store.find_enabled_user(user.user_id)
    assert disabled.value.code == "user_disabled"
    with pytest.raises(AuthStoreError) as error:
        store.set_enabled(admin.user_id, False, actor_user_id=admin.user_id)
    assert error.value.code == "last_admin_forbidden"


def test_password_change_revokes_existing_sessions(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    user = store.create_user("alice", "correct horse battery", role=UserRole.USER)
    tokens = store.create_session(user)

    store.set_password(user.user_id, "new correct horse battery")

    assert store.authenticate(tokens.session_token) is None
    assert store.verify_password("alice", "new correct horse battery") == user


def test_auth_service_limits_repeated_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = build_store(tmp_path)
        store.create_user("alice", "correct horse battery", role=UserRole.USER)
        service = AuthService(store)
        for _ in range(5):
            assert await service.login("alice", "incorrect password", client_key="client") is None
        assert await service.login("alice", "correct horse battery", client_key="client") is None

    asyncio.run(scenario())
