from __future__ import annotations

import asyncio
from pathlib import Path

from codepilot.config.settings import AppSettings, ContextModelThresholdSettings
from codepilot.context import ContextCompressor, TOOL_RESULT_PLACEHOLDER
from codepilot.events import SessionCompactedEvent
from codepilot.memory import JsonlSessionMemory
from codepilot.session import LLMState, Message, SessionState, SessionStatus, TextPart, ToolPart, build_assistant_message_info, build_user_message_info
from codepilot.utils import utc_now_iso


class FixedTokenEstimator:
    def __init__(self, count: int) -> None:
        self.count = count

    def count_messages(self, _llm_state: LLMState, _messages: list[dict[str, object]]) -> int:
        return self.count


class StubLLMClient:
    def __init__(self) -> None:
        self.summary_calls = 0

    def build_provider_messages(self, messages: list[Message]) -> list[dict[str, object]]:
        return [{"role": message.info.role, "content": message.text_content()} for message in messages]

    async def complete_text(self, **_kwargs: object) -> str:
        self.summary_calls += 1
        return "用户要求实现上下文压缩，并保留最新轮次。"


def build_session(messages: list[Message]) -> SessionState:
    return SessionState(
        session_id="session_1",
        workspace_id="ws_1",
        workspace_path="/tmp/codepilot",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        status=SessionStatus.RUNNING,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        messages=messages,
    )


def user_message(message_id: str, text: str) -> Message:
    return Message(
        info=build_user_message_info(
            message_id=message_id,
            session_id="session_1",
            created_at_ms=1_746_000_000_000,
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
        ),
        parts=[TextPart(text=text)],
    )


def assistant_message(message_id: str, text: str, tool_output: dict[str, object] | None = None) -> Message:
    parts: list[object] = [TextPart(text=text)]
    if tool_output is not None:
        parts.append(
            ToolPart(
                call_id=f"call_{message_id}",
                tool="echo_tool",
                state={
                    "status": "completed",
                    "input": {"text": text},
                    "output": tool_output,
                },
            )
        )
    return Message(
        info=build_assistant_message_info(
            message_id=message_id,
            session_id="session_1",
            created_at_ms=1_746_000_000_001,
            parent_id="user_1",
            agent="build",
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd="/tmp/codepilot",
            root="/tmp/codepilot",
        ),
        parts=parts,
    )


def compression_settings() -> AppSettings:
    settings = AppSettings()
    return settings.model_copy(
        update={
            "context": settings.context.model_copy(
                update={
                    "compression_enabled": True,
                    "latest_rounds_to_keep": 1,
                    "model_thresholds": {"default": ContextModelThresholdSettings(trigger_tokens=10)},
                }
            )
        }
    )


def test_context_compressor_rebuilds_messages_with_summary_and_latest_round() -> None:
    async def run_case() -> None:
        session = build_session(
            [
                user_message("user_1", "第一轮需求"),
                assistant_message("assistant_1", "第一轮回答"),
                user_message("user_2", "第二轮需求"),
                assistant_message("assistant_2", "第二轮回答"),
            ]
        )
        client = StubLLMClient()
        compressor = ContextCompressor(token_estimator=FixedTokenEstimator(100))

        result = await compressor.compress(
            session=session,
            config=compression_settings(),
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096, temperature=0),
            llm_client=client,  # type: ignore[arg-type]
        )

        assert result.changed is True
        assert client.summary_calls == 1
        assert [message.info.id for message in session.messages[1:]] == ["user_2", "assistant_2"]
        assert "历史上下文摘要" in session.messages[0].text_content()
        assert session.metadata["context_compression"]["compacted_until_message_id"] == "assistant_1"

    asyncio.run(run_case())


def test_tool_result_placeholder_keeps_latest_result() -> None:
    async def run_case() -> None:
        session = build_session(
            [
                user_message("user_1", "第一轮需求"),
                assistant_message("assistant_1", "第一轮回答", {"status": "ok", "tool_name": "echo_tool", "output": "旧结果"}),
                user_message("user_2", "第二轮需求"),
                assistant_message("assistant_2", "第二轮回答", {"status": "ok", "tool_name": "echo_tool", "output": "新结果"}),
            ]
        )
        settings = compression_settings()
        settings.context.latest_rounds_to_keep = 10
        settings.context.strategies.llm_summary.enabled = False
        settings.context.strategies.tool_result_placeholder.keep_latest_tool_results = 1
        compressor = ContextCompressor(token_estimator=FixedTokenEstimator(100))

        result = await compressor.compress(
            session=session,
            config=settings,
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096, temperature=0),
            llm_client=StubLLMClient(),  # type: ignore[arg-type]
        )

        assert result.changed is True
        old_tool = session.messages[1].tool_parts()[0]
        new_tool = session.messages[3].tool_parts()[0]
        assert old_tool.state.output["output"] == TOOL_RESULT_PLACEHOLDER
        assert old_tool.metadata["tool_result_compacted"] is True
        assert new_tool.state.output["output"] == "新结果"

    asyncio.run(run_case())


def test_session_compacted_record_replaces_replayed_messages(tmp_path: Path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        compacted_message = user_message("user_summary", "历史上下文摘要")

        await memory.handle_domain_event(
            SessionCompactedEvent(
                session_id="session_1",
                created_at=utc_now_iso(),
                data={
                    "messages": [compacted_message.model_dump()],
                    "metadata": {"context_compression": {"summary_message_id": "user_summary"}},
                },
            )
        )

        replay = await memory.replay("session_1")

        assert [message["info"]["id"] for message in replay["messages"]] == ["user_summary"]
        assert replay["session"]["data"]["metadata"]["context_compression"]["summary_message_id"] == "user_summary"

    asyncio.run(run_case())

