from __future__ import annotations

import json
import logging

import pytest

from codepilot.config import AppSettings, build_llm_runtime_settings
from codepilot.config.settings import LLMProviderSettings, LLMSettings, LoggingSettings
from codepilot.events import SessionLifecycleEvent
from codepilot.logging import configure_logging, get_logger
from codepilot.memory.projections import build_session_summary
from codepilot.memory.records import domain_event_to_record
from codepilot.runtime import build_title_service
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


def test_build_title_service_uses_configured_llm_model() -> None:
    llm_settings = LLMSettings(
        providers={
            "qwen": LLMProviderSettings(
                label="Qwen",
                models=["qwen3.5-flash", "kimi-k2.5"],
                litellm_model_prefix="openai/",
            )
        },
        log_requests=True,
        title_provider="qwen",
        title_model="kimi-k2.5",
    )
    settings = AppSettings(llm=llm_settings)
    runtime = build_llm_runtime_settings(settings.llm, environ={"QWEN_API_KEY": "sk-qwen", "QWEN_BASE_URL": "https://qwen.example.com/v1"})
    settings = settings.model_copy(update={"llm_runtime": runtime})

    service = build_title_service(settings)

    assert service.provider == "qwen"
    assert service.model == "kimi-k2.5"
    assert service.litellm_model_prefix == "openai/"
    assert service._llm_client._log_requests is False


def test_build_title_service_rejects_inactive_provider() -> None:
    settings = AppSettings(llm=LLMSettings(title_provider="qwen", title_model="qwen3.5-flash"))

    with pytest.raises(ValueError, match="标题生成 provider `qwen` 未激活或不存在"):
        build_title_service(settings)


def test_configure_logging_writes_runtime_logs_to_readable_jsonl_file(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LoggingSettings(level="INFO", format="json", redact_secrets=True), tmp_path)

    get_logger("codepilot.llm").info(
        "llm api request",
        request={"messages": [{"role": "user", "content": "你好，检查日志"}], "api_key": "sk-test"},
        api_key="sk-test",
    )
    logging.getLogger("third_party").warning("标准库运行日志")
    for handler in logging.getLogger().handlers:
        handler.flush()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    log_files = list(tmp_path.glob("*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "你好，检查日志" in content
    assert "\\u4f60\\u597d" not in content
    assert "***REDACTED***" in content

    records = [json.loads(line) for line in content.splitlines()]
    assert records[0]["event"] == "llm api request"
    assert records[0]["api_key"] == "***REDACTED***"
    assert records[1]["event"] == "标准库运行日志"
