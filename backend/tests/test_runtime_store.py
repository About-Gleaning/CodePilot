from __future__ import annotations

import json
from pathlib import Path

import pytest

from codepilot.events import StreamEvent
from codepilot.memory import JsonlEventStore
from codepilot.session.runtime_store import (
    JsonlRunStore,
    RuntimeControlEventStore,
    RuntimeStoreCorrupt,
    encode_runtime_cursor,
)
from codepilot.session.state import RunRef, RunState


def run_state() -> RunState:
    return RunState(
        ref=RunRef(agent_id="agent-a", session_id="session-a", run_id="run-a", revision_id="revision-a"),
        client_request_id="request-a",
        request_fingerprint="fingerprint-a",
        created_at="2026-07-29T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_truncated_last_line_is_archived_and_valid_prefix_recovers(tmp_path: Path) -> None:
    store = JsonlRunStore(tmp_path)
    await store.append(run_state())
    with store.path.open("ab") as file:
        file.write(b'{"record_type":"run_state"')
    runs, idempotency = await store.recover()
    assert len(runs) == 1
    assert "request-a" in idempotency
    assert list((tmp_path / ".corrupt").glob("agent-runs.jsonl.*.corrupt"))
    assert json.loads(store.path.read_text(encoding="utf-8").splitlines()[0])["record_type"] == "run_state"


@pytest.mark.asyncio
async def test_corrupt_middle_line_fails_closed(tmp_path: Path) -> None:
    store = JsonlRunStore(tmp_path)
    await store.append(run_state())
    with store.path.open("ab") as file:
        file.write(b"not-json\n")
        file.write(b'{"record_type":"ignored"}\n')
    with pytest.raises(RuntimeStoreCorrupt, match="中间"):
        await store.recover()


@pytest.mark.asyncio
async def test_runtime_cursor_replays_exact_persisted_position(tmp_path: Path) -> None:
    store = RuntimeControlEventStore(tmp_path)
    await store.recover()
    for index in range(2):
        await store.append(
            StreamEvent(
                seq=index + 1,
                event_type="agent_running",
                agent_id=f"agent-{index}",
                created_at="2026-07-29T00:00:00Z",
            )
        )
    replay = await store.replay(encode_runtime_cursor(1))
    assert [(seq, event.agent_id) for seq, _, event in replay] == [(2, "agent-1")]
    with pytest.raises(ValueError, match="未来"):
        await store.replay(encode_runtime_cursor(3))


def test_event_store_does_not_treat_session_id_as_glob(tmp_path: Path) -> None:
    path = tmp_path / "2026-07-29-session-safe.events.jsonl"
    path.write_text(
        json.dumps(
            StreamEvent(
                seq=1,
                event_type="safe",
                session_id="session-safe",
                created_at="2026-07-29T00:00:00Z",
            ).model_dump()
        )
        + "\n",
        encoding="utf-8",
    )
    store = JsonlEventStore(tmp_path)
    assert store.replay(session_id="*") == []
