from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from codepilot.events import EventBus, HumanInteractionEvent, MessageCreatedEvent, SessionCompactedEvent, SessionLifecycleEvent, SessionMetaEvent
from codepilot.llm.client import LiteLLMClient
from codepilot.memory import JsonlSessionMemory
from codepilot.session import Message, SessionRunner, SessionState, SessionStatus, TextPart, ToolPart, build_assistant_message_info, build_user_message_info
from codepilot.session.message import StepFinishPart, ToolPartState
from codepilot.session.title import SessionTitleService
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


def build_question_message(session_id: str, message_id: str) -> Message:
    message = Message(
        info=build_assistant_message_info(
            message_id=message_id,
            session_id=session_id,
            created_at_ms=1_746_000_000_001,
            parent_id="msg_user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd="/tmp/codepilot",
            root="/tmp/codepilot",
        ),
        parts=[
            ToolPart(
                call_id="call_question_1",
                tool="question",
                state=ToolPartState(status="pending", input={"questions": []}),
            ),
            StepFinishPart(reason="tool_pending"),
        ],
    )
    message.info.finish = "tool_pending"
    return message


def build_approval_message(session_id: str, message_id: str) -> Message:
    message = Message(
        info=build_assistant_message_info(
            message_id=message_id,
            session_id=session_id,
            created_at_ms=1_746_000_000_001,
            parent_id="msg_user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd="/tmp/codepilot",
            root="/tmp/codepilot",
        ),
        parts=[
            ToolPart(
                call_id="call_approval_1",
                tool="bash_tool",
                state=ToolPartState(status="pending", input={"command": "rm -rf x"}),
            ),
            StepFinishPart(reason="tool_pending"),
        ],
    )
    message.info.finish = "tool_pending"
    return message


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
    first_message_id = messages[0].info.id if messages else ""
    await memory.handle_domain_event(
        SessionMetaEvent(
            session_id=session.session_id,
            created_at=session.created_at,
            data={
                "title": session.title or first_message_id or session.session_id,
                "workspace_id": session.workspace_id,
                "workspace_path": session.workspace_path,
                "initial_user_message_id": first_message_id,
                "updated_at": session.updated_at,
            },
        )
    )
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
    status_value = (status or session.status).value
    record_type = "session_status_changed"
    if status_value == SessionStatus.RUNNING.value:
        record_type = "session_started"
    if status_value in {SessionStatus.COMPLETED.value, SessionStatus.CANCELLED.value}:
        record_type = "session_finished"
    if status_value == SessionStatus.FAILED.value:
        record_type = "session_failed"
    data = {
        "status": status_value,
        "agent_name": session.agent_name,
        "provider": session.provider,
        "model": session.model,
        "updated_at": created_at,
    }
    return {
        "record_type": record_type,
        "session_id": session.session_id,
        "created_at": created_at,
        "data": data,
    }


def meta_record(session: SessionState, created_at: str, title: str = "默认标题", initial_message_id: str = "msg_1") -> dict:
    return {
        "record_type": "session_meta",
        "session_id": session.session_id,
        "created_at": created_at,
        "updated_at": created_at,
        "data": {
            "title": title,
            "workspace_id": session.workspace_id,
            "workspace_path": session.workspace_path,
            "initial_user_message_id": initial_message_id,
        },
    }


def message_record(message: Message, created_at: str) -> dict:
    return {
        "record_type": "message",
        "session_id": message.info.session_id,
        "message_id": message.info.id,
        "created_at": created_at,
        "data": message.model_dump(),
    }


class StubTitleLLMClient:
    """固定返回标题，避免测试依赖真实 LLM 网络与密钥。"""

    def __init__(self, response: str) -> None:
        self.response = response

    async def complete_text(self, **_: object) -> str:
        return self.response


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


def test_session_meta_is_first_record_and_lifecycle_is_compact(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1", status=SessionStatus.RUNNING)
        message = build_message("session_1", "msg_1", "默认标题来源")

        await persist_session(memory, session, [message])

        records = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        assert records[0]["record_type"] == "session_meta"
        assert records[0]["data"]["workspace_id"] == "ws_1"
        assert "agent_name" not in records[0]["data"]
        assert "provider" not in records[0]["data"]
        assert "model" not in records[0]["data"]
        assert "metadata" not in records[0]["data"]
        assert records[1]["record_type"] == "session_started"
        assert records[1]["data"] == {
            "status": "RUNNING",
            "agent_name": "build",
            "provider": "openai",
            "model": "gpt-5.3-codex",
            "updated_at": session.updated_at,
        }

    asyncio.run(run_case())


def test_session_meta_title_update_rewrites_first_record(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1", status=SessionStatus.RUNNING)

        await persist_session(memory, session, [build_message("session_1", "msg_1", "用户输入很长")])
        await memory.handle_domain_event(
            SessionMetaEvent(
                session_id=session.session_id,
                created_at="2026-05-29T10:02:00Z",
                data={"title": "LLM 生成标题", "updated_at": "2026-05-29T10:02:00Z"},
            )
        )

        records = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        assert records[0]["record_type"] == "session_meta"
        assert records[0]["data"]["title"] == "LLM 生成标题"
        assert [record["record_type"] for record in records].count("session_started") == 1
        assert memory.list_sessions()[0]["title"] == "LLM 生成标题"

    asyncio.run(run_case())


def test_human_interaction_question_resolved_completes_pending_tool_on_replay(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1", status=SessionStatus.RUNNING)
        message = build_question_message(session.session_id, "msg_question_1")

        await persist_session(memory, session, [message])
        await memory.handle_domain_event(
            HumanInteractionEvent(
                session_id=session.session_id,
                interaction_id="question_1",
                created_at="2026-05-29T10:03:00Z",
                data={
                    "kind": "question",
                    "status": "resolved",
                    "interaction_id": "question_1",
                    "message_id": "msg_question_1",
                    "call_id": "call_question_1",
                    "result": {
                        "question_id": "question_1",
                        "answers": {"target": {"values": ["backend"], "note": ""}},
                        "declined": False,
                        "created_at": "2026-05-29T10:03:00Z",
                    },
                    "tool_output": {
                        "status": "ok",
                        "tool_name": "question",
                        "question_id": "question_1",
                        "answers": {"target": {"values": ["backend"], "note": ""}},
                        "output": "用户已回答 question 工具提出的问题。",
                    },
                },
            )
        )

        replay = await memory.replay(session.session_id)

        replayed_message = replay["messages"][0]
        tool_part = replayed_message["parts"][0]
        assert tool_part["state"]["status"] == "completed"
        assert tool_part["state"]["output"]["answers"]["target"]["values"] == ["backend"]
        assert replayed_message["parts"][1]["reason"] == "tool_completed"
        assert replayed_message["info"]["finish"] == "tool_completed"

    asyncio.run(run_case())


def test_human_interaction_approval_rejected_closes_pending_tool_on_replay(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1", status=SessionStatus.CANCELLED)
        message = build_approval_message(session.session_id, "msg_approval_1")

        await persist_session(memory, session, [message])
        await memory.handle_domain_event(
            HumanInteractionEvent(
                session_id=session.session_id,
                interaction_id="approval_tool_call_1",
                created_at="2026-05-29T10:04:00Z",
                data={
                    "kind": "approval",
                    "status": "rejected",
                    "interaction_id": "approval_tool_call_1",
                    "message_id": "msg_approval_1",
                    "call_id": "call_approval_1",
                    "result": {
                        "approval_id": "approval_tool_call_1",
                        "approved": False,
                        "comment": "拒绝执行",
                        "created_at": "2026-05-29T10:04:00Z",
                    },
                    "tool_output": {
                        "status": "error",
                        "tool_name": "bash_tool",
                        "error_type": "ToolApprovalRejected",
                        "error_message": "用户拒绝执行该工具调用：拒绝执行",
                        "recoverable": True,
                        "approval_id": "approval_tool_call_1",
                        "approved": False,
                        "comment": "拒绝执行",
                    },
                },
            )
        )

        replay = await memory.replay(session.session_id)

        replayed_message = replay["messages"][0]
        tool_part = replayed_message["parts"][0]
        assert tool_part["state"]["status"] == "error"
        assert tool_part["state"]["error"]["code"] == "ToolApprovalRejected"
        assert tool_part["state"]["output"]["error_type"] == "ToolApprovalRejected"
        assert replayed_message["parts"][1]["reason"] == "tool_completed"
        assert replayed_message["info"]["finish"] == "tool_completed"

        LiteLLMClient().build_provider_messages([Message.model_validate(replayed_message)])

    asyncio.run(run_case())


def test_human_interaction_question_declined_closes_pending_tool_on_replay(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1", status=SessionStatus.COMPLETED)
        message = build_question_message(session.session_id, "msg_question_1")

        await persist_session(memory, session, [message])
        await memory.handle_domain_event(
            HumanInteractionEvent(
                session_id=session.session_id,
                interaction_id="question_1",
                created_at="2026-05-29T10:05:00Z",
                data={
                    "kind": "question",
                    "status": "declined",
                    "interaction_id": "question_1",
                    "message_id": "msg_question_1",
                    "call_id": "call_question_1",
                    "result": {
                        "question_id": "question_1",
                        "answers": {},
                        "declined": True,
                        "comment": "退出",
                        "created_at": "2026-05-29T10:05:00Z",
                    },
                    "tool_output": {
                        "status": "error",
                        "tool_name": "question",
                        "error_type": "QuestionDeclined",
                        "error_message": "用户拒绝回答 question 工具提出的问题：退出",
                        "recoverable": True,
                        "question_id": "question_1",
                        "declined": True,
                        "comment": "退出",
                    },
                },
            )
        )

        replay = await memory.replay(session.session_id)

        replayed_message = replay["messages"][0]
        tool_part = replayed_message["parts"][0]
        assert tool_part["state"]["status"] == "error"
        assert tool_part["state"]["error"]["code"] == "QuestionDeclined"
        assert tool_part["state"]["output"]["error_type"] == "QuestionDeclined"
        assert replayed_message["parts"][1]["reason"] == "tool_completed"
        assert replayed_message["info"]["finish"] == "tool_completed"

        LiteLLMClient().build_provider_messages([Message.model_validate(replayed_message)])

    asyncio.run(run_case())


def test_session_title_service_updates_jsonl_session_meta_title(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_case() -> None:
        monkeypatch.setattr("codepilot.session.title.LiteLLMClient", lambda: StubTitleLLMClient("LLM生成标题"))
        memory = JsonlSessionMemory(tmp_path)
        event_bus = EventBus()
        event_bus.subscribe_domain(memory.handle_domain_event)
        session = build_session("session_1", status=SessionStatus.RUNNING)
        message = build_message("session_1", "msg_1", "请帮我修复会话标题没有写回jsonl的问题")
        session.messages.append(message)

        await event_bus.publish_domain_event(
            SessionMetaEvent(
                session_id=session.session_id,
                created_at=session.created_at,
                data={
                    "title": "请帮我修复会话标题没有",
                    "workspace_id": session.workspace_id,
                    "workspace_path": session.workspace_path,
                    "initial_user_message_id": message.info.id,
                    "updated_at": session.updated_at,
                },
            )
        )

        await SessionTitleService().generate_for_session(session, event_bus)

        records = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        assert records[0]["record_type"] == "session_meta"
        assert records[0]["data"]["title"] == "LLM生成标题"
        assert [record["record_type"] for record in records].count("session_meta") == 1
        assert memory.list_sessions()[0]["title"] == "LLM生成标题"

    asyncio.run(run_case())


def test_domain_event_bus_persists_session_meta_before_lifecycle(tmp_path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        session = build_session("session_1", status=SessionStatus.RUNNING)
        bus = EventBus()
        bus.subscribe_domain(memory.handle_domain_event)

        await bus.publish_domain_event(
            SessionMetaEvent(
                session_id=session.session_id,
                created_at=session.created_at,
                data={
                    "title": "默认标题",
                    "workspace_id": session.workspace_id,
                    "workspace_path": session.workspace_path,
                    "initial_user_message_id": "msg_1",
                    "updated_at": session.updated_at,
                },
            )
        )
        await bus.publish_domain_event(
            SessionLifecycleEvent(
                session_id=session.session_id,
                status=session.status.value,
                created_at=session.created_at,
                data=session.model_dump(exclude={"messages"}),
            )
        )

        records = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        assert [record["record_type"] for record in records] == ["session_meta", "session_started"]

    asyncio.run(run_case())


def test_list_sessions_merges_same_session_across_days(tmp_path) -> None:
    session = build_session("session_1")
    first_message = build_message("session_1", "msg_1", "第一天需求")
    second_message = build_message("session_1", "msg_2", "第二天继续")
    write_jsonl(
        tmp_path / "2026-05-28-session_1.jsonl",
        [
            meta_record(session, "2026-05-28T10:00:00Z", title="跨天会话", initial_message_id="msg_1"),
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
            meta_record(session, "2026-05-28T10:00:00Z", title="跨天会话", initial_message_id="msg_1"),
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
    assert len(replay["records"]) == 5


def test_replay_without_session_id_uses_latest_record_time(tmp_path) -> None:
    old_named_session = build_session("session_1")
    newer_named_session = build_session("session_2")
    write_jsonl(
        tmp_path / "2026-05-28-session_1.jsonl",
        [
            meta_record(old_named_session, "2026-05-28T10:00:00Z", title="旧文件新内容", initial_message_id="msg_1"),
            session_record(old_named_session, "2026-05-28T10:00:00Z", SessionStatus.RUNNING),
            message_record(build_message("session_1", "msg_1", "实际最新会话"), "2026-05-30T10:00:00Z"),
        ],
    )
    write_jsonl(
        tmp_path / "2026-05-29-session_2.jsonl",
        [
            meta_record(newer_named_session, "2026-05-29T10:00:00Z", title="新文件旧内容", initial_message_id="msg_2"),
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
        write_jsonl(old_path, [meta_record(session, "2026-05-28T10:00:00Z"), session_record(session, "2026-05-28T10:00:00Z")])

        await memory.handle_domain_event(
            MessageCreatedEvent(
                session_id="session_1",
                created_at="2026-05-29T10:00:00Z",
                data={"record_type": "message"},
                message=build_message("session_1", "msg_1", "跨天继续"),
            )
        )

        assert [path.name for path in tmp_path.glob("*.jsonl")] == ["2026-05-28-session_1.jsonl"]
        assert len(old_path.read_text(encoding="utf-8").splitlines()) == 3

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
