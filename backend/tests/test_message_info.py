from __future__ import annotations

import pytest
from pydantic import ValidationError

from codepilot.llm import LiteLLMClient
from codepilot.session.message import Message


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
