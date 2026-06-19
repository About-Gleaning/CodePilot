from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from codepilot.llm import LiteLLMClient
from codepilot.session import LLMState, SessionState, SessionStatus
from codepilot.session.flow import _latest_user_message_datetime
from codepilot.session.message import FilePart, FileSource, Message, TextPart, ToolPart, ToolPartState, build_assistant_message_info, build_user_message_info
from codepilot.session.system_prompt import build_runtime_context_prompt


class FakeStream:
    def __init__(self, chunks: list[dict[str, object]]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield SimpleNamespace(model_dump=lambda chunk=chunk: chunk)


class RecordingStreamBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish_stream_event(self, event: object) -> object:
        self.events.append(event)
        return event


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.records.append({"event": event, **kwargs})


def test_message_info_discriminates_user_and_assistant_by_role() -> None:
    user_message = Message.model_validate(
        {
            "info": {
                "id": "msg_user_1",
                "session_id": "session_1",
                "role": "user",
                "time": {"created": 1_746_000_000_000},
                "agent": "build",
                "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
            },
            "parts": [{"type": "text", "text": "hello"}],
        }
    )
    assistant_message = Message.model_validate(
        {
            "info": {
                "id": "msg_assistant_1",
                "session_id": "session_1",
                "role": "assistant",
                "time": {"created": 1_746_000_000_001, "completed": 1_746_000_000_999},
                "parent_id": "msg_user_1",
                "agent": "build",
                "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
                "path": {"cwd": "/tmp/codepilot", "root": "/tmp/codepilot"},
                "finish": "completed",
            },
            "parts": [{"type": "text", "text": "done"}],
        }
    )

    assert user_message.info.role == "user"
    assert user_message.info.time.created == 1_746_000_000_000
    assert assistant_message.info.role == "assistant"
    assert assistant_message.info.parent_id == "msg_user_1"
    assert assistant_message.info.time.completed == 1_746_000_000_999


def test_user_message_forbids_assistant_only_fields() -> None:
    with pytest.raises(ValidationError):
        Message.model_validate(
            {
                "info": {
                    "id": "msg_user_1",
                    "session_id": "session_1",
                    "role": "user",
                    "time": {"created": 1_746_000_000_000},
                    "agent": "build",
                    "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
                    "parent_id": "msg_other",
                },
                "parts": [{"type": "text", "text": "hello"}],
            }
        )


def test_assistant_message_requires_parent_model_and_path() -> None:
    with pytest.raises(ValidationError):
        Message.model_validate(
            {
                "info": {
                    "id": "msg_assistant_1",
                    "session_id": "session_1",
                    "role": "assistant",
                    "time": {"created": 1_746_000_000_001},
                    "agent": "build",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        )


def test_message_info_time_requires_millisecond_integer() -> None:
    with pytest.raises(ValidationError):
        Message.model_validate(
            {
                "info": {
                    "id": "msg_user_1",
                    "session_id": "session_1",
                    "role": "user",
                    "time": {"created": "2026-04-30T00:00:00Z"},
                    "agent": "build",
                    "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
                },
                "parts": [{"type": "text", "text": "hello"}],
            }
        )


def test_litellm_provider_message_builder_accepts_new_info_model_shape() -> None:
    client = LiteLLMClient()
    messages = [
        Message.model_validate(
            {
                "info": {
                    "id": "msg_user_1",
                    "session_id": "session_1",
                    "role": "user",
                    "time": {"created": 1_746_000_000_000},
                    "agent": "build",
                    "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
                },
                "parts": [{"type": "text", "text": "hello"}],
            }
        )
    ]

    assert client.build_provider_messages(messages) == [{"role": "user", "content": "hello"}]


def test_litellm_provider_message_builder_prepends_system_prompt() -> None:
    client = LiteLLMClient()
    messages = [
        Message.model_validate(
            {
                "info": {
                    "id": "msg_user_1",
                    "session_id": "session_1",
                    "role": "user",
                    "time": {"created": 1_746_000_000_000},
                    "agent": "build",
                    "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
                },
                "parts": [{"type": "text", "text": "hello"}],
            }
        )
    ]

    assert client.build_provider_messages(messages, system_prompt="system rules") == [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "hello"},
    ]


def test_litellm_provider_message_builder_adds_user_image_blocks(tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-bytes")
    client = LiteLLMClient()
    message = Message(
        info=build_user_message_info(
            message_id="msg_user_1",
            session_id="session_1",
            created_at_ms=1_746_000_000_000,
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
        ),
        parts=[
            TextPart(text="看图"),
            FilePart(mime="image/png", filename="sample.png", source=FileSource(type="file", value=str(image_path))),
        ],
    )

    provider_messages = client.build_provider_messages([message])

    content = provider_messages[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_litellm_provider_message_builder_suppresses_rich_content_after_recoverable_error(tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-bytes")
    client = LiteLLMClient()
    user_message = Message(
        info=build_user_message_info(
            message_id="msg_user_1",
            session_id="session_1",
            created_at_ms=1_746_000_000_000,
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
        ),
        parts=[
            TextPart(text="看图"),
            FilePart(mime="image/png", filename="sample.png", source=FileSource(type="file", value=str(image_path))),
        ],
    )
    recoverable_error = Message(
        info=build_assistant_message_info(
            message_id="msg_assistant_1",
            session_id="session_1",
            created_at_ms=1_746_000_000_001,
            parent_id="msg_user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd=str(tmp_path),
            root=str(tmp_path),
        ),
        parts=[TextPart(text="LLM 调用发生非致命错误。")],
    )
    recoverable_error.info.finish = "llm_error_recoverable"

    provider_messages = client.build_provider_messages([user_message, recoverable_error])

    assert provider_messages[0] == {"role": "user", "content": "看图"}
    assert provider_messages[1] == {"role": "assistant", "content": "LLM 调用发生非致命错误。"}


def test_litellm_provider_message_builder_adds_tool_image_message(tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-bytes")
    client = LiteLLMClient()
    message = Message(
        info=build_assistant_message_info(
            message_id="msg_assistant_1",
            session_id="session_1",
            created_at_ms=1_746_000_000_001,
            parent_id="msg_user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd=str(tmp_path),
            root=str(tmp_path),
        ),
        parts=[
            TextPart(text="我来读取图片"),
            ToolPart(
                call_id="call_read_1",
                tool="read_file",
                state=ToolPartState(
                    status="completed",
                    input={"file_path": str(image_path)},
                    output={
                        "status": "ok",
                        "tool_name": "read_file",
                        "attachments": [
                            {
                                "type": "image",
                                "mime": "image/png",
                                "filename": "sample.png",
                                "source_path": str(image_path),
                            }
                        ],
                    },
                ),
            ),
        ],
    )

    provider_messages = client.build_provider_messages([message])

    assert [item["role"] for item in provider_messages] == ["assistant", "tool", "user"]
    content = provider_messages[2]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_litellm_provider_message_builder_suppresses_tool_image_after_recoverable_error(tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-bytes")
    client = LiteLLMClient()
    tool_message = Message(
        info=build_assistant_message_info(
            message_id="msg_assistant_1",
            session_id="session_1",
            created_at_ms=1_746_000_000_001,
            parent_id="msg_user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd=str(tmp_path),
            root=str(tmp_path),
        ),
        parts=[
            ToolPart(
                call_id="call_read_1",
                tool="read_file",
                state=ToolPartState(
                    status="completed",
                    input={"file_path": str(image_path)},
                    output={
                        "status": "ok",
                        "tool_name": "read_file",
                        "attachments": [
                            {
                                "type": "image",
                                "mime": "image/png",
                                "filename": "sample.png",
                                "source_path": str(image_path),
                            }
                        ],
                    },
                ),
            )
        ],
    )
    recoverable_error = Message(
        info=build_assistant_message_info(
            message_id="msg_assistant_2",
            session_id="session_1",
            created_at_ms=1_746_000_000_002,
            parent_id="msg_user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd=str(tmp_path),
            root=str(tmp_path),
        ),
        parts=[TextPart(text="LLM 调用发生非致命错误。")],
    )
    recoverable_error.info.finish = "llm_error_recoverable"

    provider_messages = client.build_provider_messages([tool_message, recoverable_error])

    assert [message["role"] for message in provider_messages] == ["assistant", "tool", "assistant"]
    assert all("image_url" not in json.dumps(message, ensure_ascii=False) for message in provider_messages)


def test_litellm_provider_message_builder_wraps_latest_user_with_runtime_context() -> None:
    client = LiteLLMClient()
    messages = [
        Message.model_validate(
            {
                "info": {
                    "id": "msg_user_1",
                    "session_id": "session_1",
                    "role": "user",
                    "time": {"created": 1_746_000_000_000},
                    "agent": "build",
                    "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
                },
                "parts": [{"type": "text", "text": "历史问题"}],
            }
        ),
        Message.model_validate(
            {
                "info": {
                    "id": "msg_assistant_1",
                    "session_id": "session_1",
                    "role": "assistant",
                    "time": {"created": 1_746_000_000_001},
                    "parent_id": "msg_user_1",
                    "agent": "build",
                    "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
                    "path": {"cwd": "/tmp/codepilot", "root": "/tmp/codepilot"},
                },
                "parts": [{"type": "text", "text": "历史回答"}],
            }
        ),
        Message.model_validate(
            {
                "info": {
                    "id": "msg_user_2",
                    "session_id": "session_1",
                    "role": "user",
                    "time": {"created": 1_746_000_000_002},
                    "agent": "build",
                    "model": {"provider_id": "openai", "model_id": "gpt-5.3-codex"},
                },
                "parts": [{"type": "text", "text": "当前问题"}],
            }
        ),
    ]

    provider_messages = client.build_provider_messages(
        messages,
        system_prompt="system rules",
        runtime_context="<runtime_context>\ncurrent_time: 2026-06-15 星期一 18:00:00\n</runtime_context>",
    )

    assert provider_messages[0] == {"role": "system", "content": "system rules"}
    assert provider_messages[1] == {"role": "user", "content": "历史问题"}
    assert provider_messages[2] == {"role": "assistant", "content": "历史回答"}
    assert provider_messages[3]["role"] == "user"
    assert provider_messages[3]["content"] == (
        "<runtime_context>\n"
        "current_time: 2026-06-15 星期一 18:00:00\n"
        "</runtime_context>\n\n"
        "<user_request>\n"
        "当前问题\n"
        "</user_request>"
    )
    assert messages[-1].text_content() == "当前问题"


def test_latest_user_message_time_keeps_runtime_context_stable_for_same_turn() -> None:
    old_user = Message(
        info=build_user_message_info(
            message_id="msg_user_1",
            session_id="session_1",
            created_at_ms=1_746_000_000_000,
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
        ),
        parts=[TextPart(text="历史问题")],
    )
    assistant = Message(
        info=build_assistant_message_info(
            message_id="msg_assistant_1",
            session_id="session_1",
            created_at_ms=1_746_000_000_001,
            parent_id="msg_user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd="/tmp/codepilot",
            root="/tmp/codepilot",
        ),
        parts=[TextPart(text="历史回答")],
    )
    latest_user = Message(
        info=build_user_message_info(
            message_id="msg_user_2",
            session_id="session_1",
            created_at_ms=1_746_000_123_000,
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
        ),
        parts=[TextPart(text="当前问题")],
    )

    latest_time = _latest_user_message_datetime([old_user, assistant, latest_user, assistant])

    assert latest_time is not None
    assert latest_time.timestamp() == pytest.approx(1_746_000_123)

    session = SessionState(
        session_id="session_1",
        workspace_id="ws_1",
        workspace_path="/tmp/codepilot",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        status=SessionStatus.RUNNING,
        created_at="2026-04-30T00:00:00Z",
        updated_at="2026-04-30T00:00:00Z",
    )
    workspace = SimpleNamespace(workspace_path="/tmp/codepilot")
    llm_state = LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=128)
    agent_state = SimpleNamespace(name="build")

    first_context = build_runtime_context_prompt(
        session=session,
        workspace=workspace,
        agent_state=agent_state,
        llm_state=llm_state,
        now=latest_time,
    )
    second_context = build_runtime_context_prompt(
        session=session,
        workspace=workspace,
        agent_state=agent_state,
        llm_state=llm_state,
        now=latest_time,
    )

    assert first_context == second_context
    assert "current_time:" in first_context


def test_litellm_provider_message_builder_rejects_pending_tool_parts() -> None:
    client = LiteLLMClient()
    message = Message(
        info=build_assistant_message_info(
            message_id="msg_assistant_1",
            session_id="session_1",
            created_at_ms=1_746_000_000_000,
            parent_id="msg_user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd="/tmp/codepilot",
            root="/tmp/codepilot",
        ),
        parts=[
            ToolPart(
                call_id="call_1",
                tool="bash_tool",
                state=ToolPartState(status="pending", input={"command": "pwd"}),
            )
        ],
    )

    with pytest.raises(ValueError, match="工具调用尚未全部闭环"):
        client.build_provider_messages([message])


def test_litellm_stream_chat_extracts_usage_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_kwargs: object) -> FakeStream:
        return FakeStream(
            [
                {"choices": [{"delta": {"content": "done"}}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "prompt_tokens_details": {"cached_tokens": 5},
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            ]
        )

    monkeypatch.setattr("codepilot.llm.client.acompletion", fake_acompletion)
    client = LiteLLMClient()
    session = SessionState(
        session_id="session_1",
        workspace_id="ws_1",
        workspace_path="/tmp/codepilot",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        status=SessionStatus.RUNNING,
        created_at="2026-04-30T00:00:00Z",
        updated_at="2026-04-30T00:00:00Z",
    )

    result = asyncio.run(
        client.stream_chat(
            session=session,
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=128),
            provider_messages=[{"role": "user", "content": "hello"}],
            tools=[],
            event_bus=RecordingStreamBus(),
        )
    )

    assert result.text == "done"
    assert result.tokens is not None
    assert result.tokens.input == 11
    assert result.tokens.output == 7
    assert result.tokens.reasoning == 2
    assert result.tokens.cache is not None
    assert result.tokens.cache.read == 5
    assert result.tokens.cache.write == 0


def test_litellm_provider_kwargs_enable_reasoning_effort() -> None:
    client = LiteLLMClient()

    openai_thinking = {
        "kind": "reasoning_effort",
        "allowed_values": ["none", "low", "medium", "high"],
        "default_value": "medium",
    }
    qwen_thinking = {
        "kind": "extra_body_boolean",
        "extra_body_key": "enable_thinking",
        "allowed_values": ["on", "off"],
        "default_value": "on",
    }
    openai_enabled = client._build_provider_kwargs(
        LLMState(
            provider="openai",
            model="any-openai-model",
            max_tokens=128,
            metadata={"thinking_value": "medium", "thinking": openai_thinking},
        )
    )
    openai_none = client._build_provider_kwargs(
        LLMState(
            provider="openai",
            model="any-openai-model",
            max_tokens=128,
            metadata={"thinking_value": "none", "thinking": openai_thinking},
        )
    )
    openai_disabled = client._build_provider_kwargs(
        LLMState(
            provider="openai",
            model="any-openai-model",
            max_tokens=128,
            metadata={"thinking": openai_thinking},
        )
    )
    qwen_enabled = client._build_provider_kwargs(
        LLMState(
            provider="qwen",
            model="any-qwen-model",
            max_tokens=128,
            metadata={"thinking_value": "on", "thinking": qwen_thinking},
        )
    )
    qwen_disabled = client._build_provider_kwargs(
        LLMState(
            provider="qwen",
            model="any-qwen-model",
            max_tokens=128,
            metadata={"thinking_value": "off", "thinking": qwen_thinking},
        )
    )

    assert openai_enabled["reasoning_effort"] == "medium"
    assert openai_none["reasoning_effort"] == "none"
    assert "reasoning_effort" not in openai_disabled
    assert qwen_enabled["extra_body"] == {"enable_thinking": True}
    assert qwen_disabled["extra_body"] == {"enable_thinking": False}
    assert "reasoning_effort" not in qwen_enabled


def test_litellm_request_log_is_disabled_by_default() -> None:
    client = LiteLLMClient()
    logger = RecordingLogger()
    client._logger = logger

    client._log_request_if_enabled(endpoint="stream_chat", request={"model": "gpt-test"})

    assert logger.records == []


def test_litellm_complete_text_logs_redacted_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}

    async def fake_acompletion(**kwargs: object) -> object:
        captured_kwargs.update(kwargs)
        return SimpleNamespace(model_dump=lambda: {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setenv("QWEN_API_KEY", "sk-qwen")
    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.example.com/v1")
    monkeypatch.setattr("codepilot.llm.client.acompletion", fake_acompletion)
    client = LiteLLMClient(log_requests=True)
    logger = RecordingLogger()
    client._logger = logger

    result = asyncio.run(
        client.complete_text(
            llm_state=LLMState(
                provider="qwen",
                model="qwen3.5-flash",
                max_tokens=128,
                metadata={"litellm_model_prefix": "openai/"},
            ),
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=64,
        )
    )

    assert result == "ok"
    assert captured_kwargs["api_key"] == "sk-qwen"
    assert logger.records[0]["event"] == "llm api request"
    assert logger.records[0]["endpoint"] == "complete_text"
    logged_request = logger.records[0]["request"]
    assert isinstance(logged_request, dict)
    assert logged_request["api_key"] == "***REDACTED***"
    assert logged_request["api_base"] == "https://qwen.example.com/v1"
    assert logged_request["messages"] == [{"role": "user", "content": "hello"}]


def test_litellm_provider_kwargs_support_deepseek_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    kwargs = LiteLLMClient()._build_provider_kwargs(
        LLMState(provider="deepseek", model="deepseek-v4-pro", max_tokens=128)
    )

    assert kwargs == {
        "api_key": "sk-deepseek",
        "api_base": "https://api.deepseek.com",
    }


def test_litellm_provider_kwargs_support_deepseek_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.example.com/v1")
    monkeypatch.setenv("QWEN_API_KEY", "sk-qwen")
    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.example.com/v1")

    kwargs = LiteLLMClient()._build_provider_kwargs(
        LLMState(provider="deepseek", model="deepseek-v4-flash", max_tokens=128)
    )

    assert kwargs == {
        "api_key": "sk-deepseek",
        "api_base": "https://deepseek.example.com/v1",
    }


def test_litellm_stream_chat_publishes_reasoning_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}

    async def fake_acompletion(**kwargs: object) -> FakeStream:
        captured_kwargs.update(kwargs)
        return FakeStream(
            [
                {"choices": [{"delta": {"reasoning_content": "先分析"}}]},
                {"choices": [{"delta": {"content": "done"}}]},
            ]
        )

    monkeypatch.setattr("codepilot.llm.client.acompletion", fake_acompletion)
    client = LiteLLMClient()
    session = SessionState(
        session_id="session_1",
        workspace_id="ws_1",
        workspace_path="/tmp/codepilot",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        status=SessionStatus.RUNNING,
        created_at="2026-04-30T00:00:00Z",
        updated_at="2026-04-30T00:00:00Z",
        metadata={"agent_kind": "subagent", "agent_context_id": "ctx_1", "parent_call_id": "call_1"},
    )
    event_bus = RecordingStreamBus()

    result = asyncio.run(
        client.stream_chat(
            session=session,
            llm_state=LLMState(
                provider="openai",
                model="gpt-5.3-codex",
                max_tokens=128,
                metadata={
                    "thinking_value": "medium",
                    "thinking": {
                        "kind": "reasoning_effort",
                        "allowed_values": ["low", "medium", "high"],
                        "default_value": "medium",
                    },
                },
            ),
            provider_messages=[{"role": "user", "content": "hello"}],
            tools=[],
            event_bus=event_bus,
        )
    )

    assert captured_kwargs["reasoning_effort"] == "medium"
    assert "temperature" not in captured_kwargs
    assert result.text == "done"
    assert result.reasoning == "先分析"
    assert [event.event_type for event in event_bus.events] == ["llm_reasoning_delta", "llm_delta"]
    assert event_bus.events[0].data == {
        "agent_kind": "subagent",
        "context_id": "ctx_1",
        "parent_call_id": "call_1",
        "text": "先分析",
    }
