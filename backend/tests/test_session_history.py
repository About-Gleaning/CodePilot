from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from codepilot.events import MessageCreatedEvent, SessionCompactedEvent, SessionLifecycleEvent
from codepilot.memory import JsonlSessionMemory
from codepilot.session import Message, SessionRunner, SessionState, SessionStatus, TextPart, build_user_message_info
from codepilot.utils import utc_now_iso


def build_message(session_id: str, message_id: str, text: str) -> Message:
    return Message(
        info=build_user_message_info(
            message_id=message_id,
            session_id=session_id,
            created_at_ms=1_746_000_000_000,
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
        ),
        parts=[TextPart(text=text)],
    )


def build_session(session_id: str, status: SessionStatus = SessionStatus.COMPLETED) -> SessionState:
    return SessionState(
        session_id=session_id,
        workspace_id="ws_1",
        workspace_path="/tmp/codepilot",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        status=status,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        messages=[],
    )


async def persist_session(memory: JsonlSessionMemory, session: SessionState, messages: list[Message]) -> None:
    await memory.handle_domain_event(
        SessionLifecycleEvent(
            session_id=session.session_id,
            status=session.status.value,
            created_at=session.created_at,
            data=session.model_dump(exclude={"messages"}),
        )
    )
    for message in messages:
        await memory.handle_domain_event(
            MessageCreatedEvent(
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={"record_type": "message"},
                message=message,
            )
        )


def write_jsonl(path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")


def session_record(session: SessionState, created_at: str, status: SessionStatus | None = None) -> dict:
    data = session.model_dump(exclude={"messages"})
    if status is not None:
        data["status"] = status.value
    return {
        "record_type": "session_finished",
        "session_id": session.session_id,
        "created_at": created_at,
        "data": data,
    }


def message_record(message: Message, created_at: str) -> dict:
    return {
        "record_type": "message",
        "session_id": message.info.session_id,
        "message_id": message.info.id,
        "created_at": created_at,
        "data": message.model_dump(),
    }


def test_list_sessions_returns_summaries_sorted_by_latest_update(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        first = build_session("session_1")
        second = build_session("session_2")

        await persist_session(memory, first, [build_message("session_1", "msg_1", "第一段需求")])
        await persist_session(memory, second, [build_message("session_2", "msg_2", "第二段需求")])
        (tmp_path / "2026-05-29-session_3.events.jsonl").write_text("{}", encoding="utf-8")

        summaries = memory.list_sessions()

        assert [item["session_id"] for item in summaries] == ["session_2", "session_1"]
        assert summaries[0]["message_count"] == 1
        assert summaries[0]["preview"] == "第二段需求"

    asyncio.run(run_case())


def test_list_sessions_returns_generated_title(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1")
        session.title = "修复历史标题"

        await persist_session(memory, session, [build_message("session_1", "msg_1", "用户输入很长")])

        summaries = memory.list_sessions()

        assert summaries[0]["title"] == "修复历史标题"
        assert summaries[0]["preview"] == "用户输入很长"

    asyncio.run(run_case())


def test_list_sessions_merges_same_session_across_days(tmp_path) -> None:
    session = build_session("session_1")
    first_message = build_message("session_1", "msg_1", "第一天需求")
    second_message = build_message("session_1", "msg_2", "第二天继续")
    write_jsonl(
        tmp_path / "2026-05-28-session_1.jsonl",
        [
            session_record(session, "2026-05-28T10:00:00Z", SessionStatus.RUNNING),
            message_record(first_message, "2026-05-28T10:01:00Z"),
        ],
    )
    write_jsonl(
        tmp_path / "2026-05-29-session_1.jsonl",
        [
            message_record(second_message, "2026-05-29T10:01:00Z"),
            session_record(session, "2026-05-29T10:02:00Z", SessionStatus.COMPLETED),
        ],
    )

    summaries = JsonlSessionMemory(tmp_path).list_sessions()

    assert [item["session_id"] for item in summaries] == ["session_1"]
    assert summaries[0]["message_count"] == 2
    assert summaries[0]["preview"] == "第一天需求"
    assert summaries[0]["status"] == SessionStatus.COMPLETED.value


def test_replay_merges_same_session_across_days(tmp_path) -> None:
    session = build_session("session_1")
    first_message = build_message("session_1", "msg_1", "第一天需求")
    second_message = build_message("session_1", "msg_2", "第二天继续")
    write_jsonl(
        tmp_path / "2026-05-28-session_1.jsonl",
        [
            session_record(session, "2026-05-28T10:00:00Z", SessionStatus.RUNNING),
            message_record(first_message, "2026-05-28T10:01:00Z"),
        ],
    )
    write_jsonl(
        tmp_path / "2026-05-29-session_1.jsonl",
        [
            message_record(second_message, "2026-05-29T10:01:00Z"),
            session_record(session, "2026-05-29T10:02:00Z", SessionStatus.COMPLETED),
        ],
    )

    replay = asyncio.run(JsonlSessionMemory(tmp_path).replay("session_1"))

    assert replay["session"]["session_id"] == "session_1"
    assert [Message.model_validate(item).text_content() for item in replay["messages"]] == ["第一天需求", "第二天继续"]
    assert len(replay["records"]) == 4


def test_replay_without_session_id_uses_latest_record_time(tmp_path) -> None:
    old_named_session = build_session("session_1")
    newer_named_session = build_session("session_2")
    write_jsonl(
        tmp_path / "2026-05-28-session_1.jsonl",
        [
            session_record(old_named_session, "2026-05-28T10:00:00Z", SessionStatus.RUNNING),
            message_record(build_message("session_1", "msg_1", "实际最新会话"), "2026-05-30T10:00:00Z"),
        ],
    )
    write_jsonl(
        tmp_path / "2026-05-29-session_2.jsonl",
        [
            session_record(newer_named_session, "2026-05-29T10:00:00Z", SessionStatus.COMPLETED),
            message_record(build_message("session_2", "msg_2", "文件名更新但内容较旧"), "2026-05-29T10:01:00Z"),
        ],
    )

    replay = asyncio.run(JsonlSessionMemory(tmp_path).replay())

    assert [Message.model_validate(item).text_content() for item in replay["messages"]] == ["实际最新会话"]


def test_handle_domain_event_reuses_existing_session_file(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1")
        old_path = tmp_path / "2026-05-28-session_1.jsonl"
        write_jsonl(old_path, [session_record(session, "2026-05-28T10:00:00Z")])

        await memory.handle_domain_event(
            MessageCreatedEvent(
                session_id="session_1",
                created_at="2026-05-29T10:00:00Z",
                data={"record_type": "message"},
                message=build_message("session_1", "msg_1", "跨天继续"),
            )
        )

        assert [path.name for path in tmp_path.glob("*.jsonl")] == ["2026-05-28-session_1.jsonl"]
        assert len(old_path.read_text(encoding="utf-8").splitlines()) == 2

    asyncio.run(run_case())


def test_list_sessions_uses_compacted_messages_for_count(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1")
        compacted_message = build_message("session_1", "msg_summary", "历史上下文摘要")

        await persist_session(memory, session, [build_message("session_1", "msg_1", "原始需求")])
        await memory.handle_domain_event(
            SessionCompactedEvent(
                session_id="session_1",
                created_at=utc_now_iso(),
                data={"messages": [compacted_message.model_dump()], "metadata": {"compressed": True}},
            )
        )

        summaries = memory.list_sessions()

        assert summaries[0]["message_count"] == 1
        assert summaries[0]["preview"] == "原始需求"

    asyncio.run(run_case())


def test_session_runner_loads_replayed_session_and_normalizes_stale_status() -> None:
    runner = SessionRunner(
        workspace=SimpleNamespace(workspace_id="ws_1", workspace_path="/tmp/codepilot"),
        config=SimpleNamespace(agent=SimpleNamespace(default_agent_name="build")),
        event_bus=SimpleNamespace(),
        hook_manager=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        agent_profiles={},
    )
    session = build_session("session_1", status=SessionStatus.RUNNING)
    message = build_message("session_1", "msg_1", "继续这个会话")

    loaded = runner.load_session(
        "session_1",
        {
            "session": {"data": session.model_dump(exclude={"messages"})},
            "messages": [message.model_dump()],
            "records": [],
        },
    )

    assert loaded.session_id == "session_1"
    assert loaded.status == SessionStatus.CANCELLED
    assert [item.text_content() for item in loaded.messages] == ["继续这个会话"]


def test_session_runner_rejects_load_when_current_session_is_running() -> None:
    runner = SessionRunner(
        workspace=SimpleNamespace(workspace_id="ws_1", workspace_path="/tmp/codepilot"),
        config=SimpleNamespace(agent=SimpleNamespace(default_agent_name="build")),
        event_bus=SimpleNamespace(),
        hook_manager=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        agent_profiles={},
    )
    runner._session = build_session("active_session", status=SessionStatus.RUNNING)

    with pytest.raises(ValueError, match="不能加载历史会话"):
        runner.load_session(
            "session_1",
            {
                "session": {"data": build_session("session_1").model_dump(exclude={"messages"})},
                "messages": [],
                "records": [],
            },
        )
