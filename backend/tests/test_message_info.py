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
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=128, temperature=0),
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
