from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def utc_now_millis() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def new_session_id() -> str:
    return f"sess_{uuid4().hex}"


def new_message_id() -> str:
    return f"msg_{uuid4().hex}"
