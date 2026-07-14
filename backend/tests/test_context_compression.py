from __future__ import annotations

import asyncio
import json
from pathlib import Path

from codepilot.config.settings import AppSettings, ContextModelThresholdSettings
from codepilot.context import ContextCompressor, TOOL_RESULT_PLACEHOLDER
from codepilot.events import MessageCreatedEvent, SessionCompactedEvent, SessionMetaEvent
from codepilot.llm import LiteLLMClient
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
        self.summary_messages: list[dict[str, object]] = []
        self._message_builder = LiteLLMClient()

    def build_provider_messages(self, messages: list[Message]) -> list[dict[str, object]]:
        return self._message_builder.build_provider_messages(messages)  # type: ignore[return-value]

    async def complete_text(self, **kwargs: object) -> str:
        self.summary_calls += 1
        self.summary_messages = list(kwargs.get("messages") or [])
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


def user_message(
    message_id: str,
    text: str,
    *,
    agent: str = "build",
    agent_kind: str = "agent",
    context_id: str = "main",
    parent_call_id: str | None = None,
) -> Message:
    return Message(
        info=build_user_message_info(
            message_id=message_id,
            session_id="session_1",
            created_at_ms=1_746_000_000_000,
            agent=agent,
            agent_kind=agent_kind,  # type: ignore[arg-type]
            context_id=context_id,
            parent_call_id=parent_call_id,
            provider_id="openai",
            model_id="gpt-5.3-codex",
        ),
        parts=[TextPart(text=text)],
    )


def assistant_message(
    message_id: str,
    text: str,
    tool_output: dict[str, object] | None = None,
    *,
    agent: str = "build",
    agent_kind: str = "agent",
    context_id: str = "main",
    parent_call_id: str | None = None,
) -> Message:
    parts: list[object] = [TextPart(text=text)]
    if tool_output is not None:
        parts.append(
            ToolPart(
                call_id=f"call_{message_id}",
                tool="bash_tool",
                state={
                    "status": "completed",
                    "input": {"command": text},
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
            agent=agent,
            agent_kind=agent_kind,  # type: ignore[arg-type]
            context_id=context_id,
            parent_call_id=parent_call_id,
            provider_id="openai",
            model_id="gpt-5.3-codex",
            cwd="/tmp/codepilot",
            root="/tmp/codepilot",
        ),
        parts=parts,
    )


def summary_message(message_id: str, text: str) -> Message:
    message = user_message(message_id, text)
    message.parts[0].synthetic = True  # type: ignore[attr-defined]
    message.parts[0].metadata["context_summary"] = True  # type: ignore[attr-defined]
    return message


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
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
            llm_client=client,  # type: ignore[arg-type]
        )

        assert result.changed is True
        assert client.summary_calls == 1
        assert client.summary_messages[0]["role"] == "system"
        assert "历史对话压缩" in str(client.summary_messages[0]["content"])
        assert [message["role"] for message in client.summary_messages[1:]] == ["user", "assistant"]
        assert client.summary_messages[1]["content"] == "第一轮需求"
        assert client.summary_messages[2]["content"] == "第一轮回答"
        assert [message.info.id for message in session.messages[1:]] == ["user_2", "assistant_2"]
        assert "历史上下文摘要" in session.messages[0].text_content()
        assert session.metadata["context_compression"]["compacted_until_message_id"] == "assistant_1"

    asyncio.run(run_case())


def test_tool_result_placeholder_runs_before_summary() -> None:
    async def run_case() -> None:
        session = build_session(
            [
                user_message("user_1", "第一轮需求"),
                assistant_message("assistant_1", "第一轮回答", {"status": "ok", "tool_name": "bash_tool", "output": "旧结果"}),
                user_message("user_2", "第二轮需求"),
                assistant_message("assistant_2", "第二轮回答", {"status": "ok", "tool_name": "bash_tool", "output": "新结果"}),
            ]
        )
        settings = compression_settings()
        settings.context.strategies.tool_result_placeholder.keep_latest_tool_results = 1
        client = StubLLMClient()
        compressor = ContextCompressor(token_estimator=FixedTokenEstimator(100))

        result = await compressor.compress(
            session=session,
            config=settings,
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
            llm_client=client,  # type: ignore[arg-type]
        )

        assert result.strategies == ["tool_result_placeholder", "llm_summary"]
        summary_payload = json.dumps(client.summary_messages, ensure_ascii=False, default=str)
        assert TOOL_RESULT_PLACEHOLDER in summary_payload
        assert "旧结果" not in summary_payload

    asyncio.run(run_case())


def test_existing_summary_is_sent_as_first_old_message_when_recompressing() -> None:
    async def run_case() -> None:
        session = build_session(
            [
                summary_message("summary_1", "历史上下文摘要：\n旧摘要"),
                user_message("user_2", "第二轮需求"),
                assistant_message("assistant_2", "第二轮回答"),
                user_message("user_3", "第三轮需求"),
                assistant_message("assistant_3", "第三轮回答"),
            ]
        )
        client = StubLLMClient()
        compressor = ContextCompressor(token_estimator=FixedTokenEstimator(100))

        result = await compressor.compress(
            session=session,
            config=compression_settings(),
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
            llm_client=client,  # type: ignore[arg-type]
        )

        assert result.changed is True
        assert [message["role"] for message in client.summary_messages[:4]] == ["system", "user", "user", "assistant"]
        assert client.summary_messages[1]["content"] == "历史上下文摘要：\n旧摘要"
        assert client.summary_messages[2]["content"] == "第二轮需求"
        assert client.summary_messages[3]["content"] == "第二轮回答"

    asyncio.run(run_case())


def test_context_compressor_only_replaces_target_context_messages() -> None:
    async def run_case() -> None:
        session = build_session(
            [
                user_message("user_1", "第一轮需求"),
                assistant_message("assistant_1", "第一轮回答"),
                user_message(
                    "sub_user_1",
                    "读取 README",
                    agent="explore",
                    agent_kind="subagent",
                    context_id="ctx_sub",
                    parent_call_id="call_task_1",
                ),
                assistant_message(
                    "sub_assistant_1",
                    "README 结论",
                    agent="explore",
                    agent_kind="subagent",
                    context_id="ctx_sub",
                    parent_call_id="call_task_1",
                ),
                user_message(
                    "sub_user_2",
                    "读取配置",
                    agent="explore",
                    agent_kind="subagent",
                    context_id="ctx_sub",
                    parent_call_id="call_task_1",
                ),
                assistant_message(
                    "sub_assistant_2",
                    "配置结论",
                    agent="explore",
                    agent_kind="subagent",
                    context_id="ctx_sub",
                    parent_call_id="call_task_1",
                ),
                user_message("user_2", "第二轮需求"),
                assistant_message("assistant_2", "第二轮回答"),
            ]
        )
        session.metadata["agent_context_id"] = "ctx_sub"
        session.metadata["agent_kind"] = "subagent"
        session.metadata["parent_call_id"] = "call_task_1"
        compressor = ContextCompressor(token_estimator=FixedTokenEstimator(100))

        result = await compressor.compress(
            session=session,
            config=compression_settings(),
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
            llm_client=StubLLMClient(),  # type: ignore[arg-type]
            context_id="ctx_sub",
        )

        assert result.changed is True
        assert result.context_id == "ctx_sub"
        assert [message.info.id for message in session.messages if message.info.context_id == "main"] == [
            "user_1",
            "assistant_1",
            "user_2",
            "assistant_2",
        ]
        sub_context_ids = [message.info.context_id for message in session.messages if message.info.context_id == "ctx_sub"]
        assert sub_context_ids == ["ctx_sub", "ctx_sub", "ctx_sub"]
        assert "历史上下文摘要" in next(message for message in session.messages if message.info.context_id == "ctx_sub").text_content()

    asyncio.run(run_case())


def test_tool_result_placeholder_keeps_latest_result() -> None:
    async def run_case() -> None:
        session = build_session(
            [
                user_message("user_1", "第一轮需求"),
                assistant_message("assistant_1", "第一轮回答", {"status": "ok", "tool_name": "bash_tool", "output": "旧结果"}),
                user_message("user_2", "第二轮需求"),
                assistant_message("assistant_2", "第二轮回答", {"status": "ok", "tool_name": "bash_tool", "output": "新结果"}),
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
            llm_state=LLMState(provider="openai", model="gpt-5.3-codex", max_tokens=4096),
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
            SessionMetaEvent(
                session_id="session_1",
                created_at=utc_now_iso(),
                data={
                    "title": "上下文压缩",
                    "workspace_id": "ws_1",
                    "workspace_path": "/tmp/codepilot",
                    "initial_user_message_id": "user_1",
                    "updated_at": utc_now_iso(),
                },
            )
        )
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
        compacted_record = next(record for record in replay["records"] if record["record_type"] == "session_compacted")
        assert compacted_record["data"]["metadata"]["context_compression"]["summary_message_id"] == "user_summary"

    asyncio.run(run_case())


def test_context_scoped_session_compacted_record_replaces_only_matching_context(tmp_path: Path) -> None:
    async def run_case() -> None:
        memory = JsonlSessionMemory(tmp_path)
        main_message = user_message("user_main", "主任务")
        sub_message = user_message(
            "sub_user",
            "子任务",
            agent="explore",
            agent_kind="subagent",
            context_id="ctx_sub",
            parent_call_id="call_task_1",
        )
        compacted_sub_message = user_message(
            "sub_summary",
            "子任务摘要",
            agent="explore",
            agent_kind="subagent",
            context_id="ctx_sub",
            parent_call_id="call_task_1",
        )

        await memory.handle_domain_event(
            SessionMetaEvent(
                session_id="session_1",
                created_at=utc_now_iso(),
                data={
                    "title": "上下文压缩",
                    "workspace_id": "ws_1",
                    "workspace_path": "/tmp/codepilot",
                    "initial_user_message_id": "user_main",
                    "updated_at": utc_now_iso(),
                },
            )
        )
        for message in [main_message, sub_message]:
            await memory.handle_domain_event(
                MessageCreatedEvent(
                    session_id="session_1",
                    created_at=utc_now_iso(),
                    data={"record_type": "message"},
                    message=message,
                )
            )
        await memory.handle_domain_event(
            SessionCompactedEvent(
                session_id="session_1",
                created_at=utc_now_iso(),
                data={
                    "scope": "context",
                    "context_id": "ctx_sub",
                    "messages": [compacted_sub_message.model_dump()],
                    "metadata": {"context_compression": {"summary_message_id": "sub_summary"}},
                },
            )
        )

        replay = await memory.replay("session_1")

        assert [message["info"]["id"] for message in replay["messages"]] == ["user_main", "sub_summary"]

    asyncio.run(run_case())
