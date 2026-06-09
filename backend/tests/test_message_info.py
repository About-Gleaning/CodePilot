from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from codepilot.llm import LiteLLMClient
from codepilot.session import LLMState, SessionState, SessionStatus
from codepilot.session.message import Message


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
