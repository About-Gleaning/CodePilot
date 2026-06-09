from __future__ import annotations

from codepilot.events import SessionLifecycleEvent
from codepilot.memory.projections import build_session_summary
from codepilot.memory.records import domain_event_to_record
from codepilot.tools.results import ToolResultBuilder


def test_tool_result_builder_maps_error_to_tool_part() -> None:
    builder = ToolResultBuilder()
    result = builder.error_result("bash", "ToolTimeoutError", "工具执行超时")

    part = builder.completed_part("call_1", "bash", result, tool_args={"cmd": "sleep 10"})

    assert part.call_id == "call_1"
    assert part.state.status == "error"
    assert part.state.input == {"cmd": "sleep 10"}
    assert part.state.error is not None
    assert part.state.error.code == "ToolTimeoutError"


def test_lifecycle_event_record_remains_compact() -> None:
    record = domain_event_to_record(
        SessionLifecycleEvent(
            session_id="session_1",
            status="COMPLETED",
            created_at="2026-04-30T00:00:00Z",
            data={
                "agent_name": "build",
                "provider": "openai",
                "model": "gpt-5.3-codex",
                "metadata": {"ignored": True},
            },
        )
    )

    assert record["record_type"] == "session_finished"
    assert record["data"] == {
        "status": "COMPLETED",
        "agent_name": "build",
        "provider": "openai",
        "model": "gpt-5.3-codex",
        "updated_at": "2026-04-30T00:00:00Z",
    }


def test_build_session_summary_uses_first_user_preview() -> None:
    records = [
        {
            "record_type": "session_meta",
            "session_id": "session_1",
            "created_at": "2026-04-30T00:00:00Z",
            "updated_at": "2026-04-30T00:00:00Z",
            "data": {"title": "标题", "workspace_id": "ws_1", "workspace_path": "/tmp/codepilot"},
        },
        {
            "record_type": "message",
            "session_id": "session_1",
            "created_at": "2026-04-30T00:00:01Z",
            "data": {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "请检查后端结构"}],
            },
        },
        {
            "record_type": "session_finished",
            "session_id": "session_1",
            "created_at": "2026-04-30T00:00:02Z",
            "data": {"status": "COMPLETED", "agent_name": "build", "updated_at": "2026-04-30T00:00:02Z"},
        },
    ]

    summary = build_session_summary(records)

    assert summary is not None
    assert summary["preview"] == "请检查后端结构"
    assert summary["message_count"] == 1
    assert summary["status"] == "COMPLETED"
