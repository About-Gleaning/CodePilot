from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol

from litellm import token_counter

from codepilot.config.settings import AppSettings, ContextModelThresholdSettings
from codepilot.llm import LiteLLMClient
from codepilot.session.message import Message, TextPart, ToolPart, build_user_message_info
from codepilot.session.state import LLMState, SessionState
from codepilot.utils import new_message_id, utc_now_iso, utc_now_millis


TOOL_RESULT_PLACEHOLDER = "[Old tool result content cleared]"
SUMMARY_METADATA_KEY = "context_summary"
COMPRESSION_METADATA_KEY = "context_compression"


class ContextCompressionError(RuntimeError):
    """上下文压缩失败时抛出，调用方负责中断本轮推理。"""


@dataclass(slots=True)
class CompressionResult:
    changed: bool = False
    before_tokens: int = 0
    after_tokens: int = 0
    before_message_count: int = 0
    after_message_count: int = 0
    strategies: list[str] = field(default_factory=list)
    compacted_until_message_id: str | None = None
    summary_message_id: str | None = None

    def to_event_data(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "before_message_count": self.before_message_count,
            "after_message_count": self.after_message_count,
            "strategies": self.strategies,
            "compacted_until_message_id": self.compacted_until_message_id,
            "summary_message_id": self.summary_message_id,
        }


class CompressionStrategy(Protocol):
    name: str

    async def apply(self, ctx: "CompressionContext") -> bool:
        raise NotImplementedError


@dataclass(slots=True)
class CompressionContext:
    session: SessionState
    config: AppSettings
    llm_state: LLMState
    llm_client: LiteLLMClient
    messages: list[Message]
    token_estimator: "TokenEstimator"


class TokenEstimator:
    def count_messages(self, llm_state: LLMState, messages: list[dict[str, Any]]) -> int:
        try:
            return int(token_counter(model=self._resolve_model_name(llm_state), messages=messages))
        except Exception:  # noqa: BLE001
            # token_counter 可能因未知模型或本地模型价格表缺失失败；降级估算保证压缩流程可继续。
            raw = json.dumps(messages, ensure_ascii=False, default=str)
            return max(1, len(raw) // 4)

    def _resolve_model_name(self, llm_state: LLMState) -> str:
        prefix = llm_state.metadata.get("litellm_model_prefix", "")
        return f"{prefix}{llm_state.model}"


class ContextCompressor:
    def __init__(self, token_estimator: TokenEstimator | None = None) -> None:
        self._token_estimator = token_estimator or TokenEstimator()

    async def compress(
        self,
        *,
        session: SessionState,
        config: AppSettings,
        llm_state: LLMState,
        llm_client: LiteLLMClient,
    ) -> CompressionResult:
        settings = config.context
        provider_messages = llm_client.build_provider_messages(session.messages)
        before_tokens = self._token_estimator.count_messages(llm_state, provider_messages)
        result = CompressionResult(
            before_tokens=before_tokens,
            after_tokens=before_tokens,
            before_message_count=len(session.messages),
            after_message_count=len(session.messages),
        )
        if not settings.compression_enabled:
            return result

        threshold = self._resolve_threshold(settings.model_thresholds, llm_state.model)
        trigger_tokens = self._resolve_trigger_tokens(threshold)
        if trigger_tokens is None or before_tokens < trigger_tokens:
            return result

        ctx = CompressionContext(
            session=session,
            config=config,
            llm_state=llm_state,
            llm_client=llm_client,
            messages=deepcopy(session.messages),
            token_estimator=self._token_estimator,
        )
        strategies: list[CompressionStrategy] = []
        if settings.strategies.llm_summary.enabled:
            strategies.append(LLMSummaryCompressionStrategy())
        if settings.strategies.tool_result_placeholder.enabled:
            strategies.append(ToolResultPlaceholderStrategy())

        compacted_until_message_id: str | None = None
        summary_message_id: str | None = None
        for strategy in strategies:
            changed = await strategy.apply(ctx)
            if changed:
                result.changed = True
                result.strategies.append(strategy.name)
                metadata = ctx.session.metadata.get(COMPRESSION_METADATA_KEY) or {}
                compacted_until_message_id = metadata.get("compacted_until_message_id") or compacted_until_message_id
                summary_message_id = metadata.get("summary_message_id") or summary_message_id

        if not result.changed:
            return result

        session.messages = ctx.messages
        provider_messages_after = llm_client.build_provider_messages(session.messages)
        result.after_tokens = self._token_estimator.count_messages(llm_state, provider_messages_after)
        result.after_message_count = len(session.messages)
        result.compacted_until_message_id = compacted_until_message_id
        result.summary_message_id = summary_message_id
        session.metadata[COMPRESSION_METADATA_KEY] = {
            **(session.metadata.get(COMPRESSION_METADATA_KEY) or {}),
            "last_compacted_at": utc_now_iso(),
            "summary_message_id": summary_message_id,
            "compacted_until_message_id": compacted_until_message_id,
            "before_tokens": result.before_tokens,
            "after_tokens": result.after_tokens,
            "before_message_count": result.before_message_count,
            "after_message_count": result.after_message_count,
            "strategies": list(result.strategies),
        }
        session.updated_at = utc_now_iso()
        return result

    def _resolve_threshold(
        self,
        thresholds: dict[str, ContextModelThresholdSettings],
        model: str,
    ) -> ContextModelThresholdSettings:
        return thresholds.get(model) or thresholds.get("default") or ContextModelThresholdSettings()

    def _resolve_trigger_tokens(self, threshold: ContextModelThresholdSettings) -> int | None:
        candidates: list[int] = []
        if threshold.trigger_tokens:
            candidates.append(threshold.trigger_tokens)
        if threshold.trigger_ratio and threshold.context_window_tokens:
            candidates.append(int(threshold.trigger_ratio * threshold.context_window_tokens))
        return min(candidates) if candidates else None


class LLMSummaryCompressionStrategy:
    name = "llm_summary"

    async def apply(self, ctx: CompressionContext) -> bool:
        keep_rounds = max(0, ctx.config.context.latest_rounds_to_keep)
        keep_start = self._find_keep_start(ctx.messages, keep_rounds)
        summary_index = self._find_summary_index(ctx.messages)
        candidate_start = summary_index + 1 if summary_index is not None else 0
        if keep_start <= candidate_start:
            return False

        candidate_messages = ctx.messages[candidate_start:keep_start]
        if not candidate_messages:
            return False

        existing_summary = ctx.messages[summary_index] if summary_index is not None else None
        summary_text = await self._build_summary(ctx, existing_summary, candidate_messages)
        compacted_until = candidate_messages[-1].info.id
        summary_message = self._build_summary_message(ctx.session, ctx.llm_state, summary_text)
        preserved_tail = ctx.messages[keep_start:]
        ctx.messages = [summary_message, *preserved_tail]
        ctx.session.metadata[COMPRESSION_METADATA_KEY] = {
            **(ctx.session.metadata.get(COMPRESSION_METADATA_KEY) or {}),
            "summary_message_id": summary_message.info.id,
            "compacted_until_message_id": compacted_until,
        }
        return True

    def _find_keep_start(self, messages: list[Message], keep_rounds: int) -> int:
        if keep_rounds <= 0:
            return len(messages)
        user_indices = [index for index, message in enumerate(messages) if message.info.role == "user" and not self._is_summary_message(message)]
        if len(user_indices) <= keep_rounds:
            return 0
        return user_indices[-keep_rounds]

    def _find_summary_index(self, messages: list[Message]) -> int | None:
        for index, message in enumerate(messages):
            if self._is_summary_message(message):
                return index
        return None

    def _is_summary_message(self, message: Message) -> bool:
        return any(isinstance(part, TextPart) and part.metadata.get(SUMMARY_METADATA_KEY) for part in message.parts)

    async def _build_summary(
        self,
        ctx: CompressionContext,
        existing_summary: Message | None,
        candidate_messages: list[Message],
    ) -> str:
        source = {
            "existing_summary": existing_summary.text_content() if existing_summary else "",
            "messages_to_compact": [
                {"role": message.info.role, "content": message.text_content(), "parts": [part.model_dump() for part in message.parts]}
                for message in candidate_messages
            ],
        }
        prompt = (
            "请把以下历史对话压缩为一份可供后续 Agent 推理使用的中文上下文摘要。"
            "保留用户目标、关键约束、重要决策、工具调用结论、未完成事项和风险。"
            "不要编造未出现的信息。\n\n"
            f"{json.dumps(source, ensure_ascii=False, default=str)}"
        )
        max_tokens = ctx.config.context.strategies.llm_summary.summary_max_tokens
        try:
            summary = await ctx.llm_client.complete_text(
                llm_state=ctx.llm_state,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise ContextCompressionError(f"上下文摘要生成失败：{exc}") from exc
        if not summary.strip():
            raise ContextCompressionError("上下文摘要生成失败：模型返回空摘要")
        return summary.strip()

    def _build_summary_message(self, session: SessionState, llm_state: LLMState, summary_text: str) -> Message:
        return Message(
            info=build_user_message_info(
                message_id=new_message_id(),
                session_id=session.session_id,
                created_at_ms=utc_now_millis(),
                agent=session.agent_name,
                provider_id=llm_state.provider,
                model_id=llm_state.model,
            ),
            parts=[
                TextPart(
                    text=f"历史上下文摘要：\n{summary_text}",
                    synthetic=True,
                    metadata={SUMMARY_METADATA_KEY: True},
                )
            ],
        )


class ToolResultPlaceholderStrategy:
    name = "tool_result_placeholder"

    async def apply(self, ctx: CompressionContext) -> bool:
        keep_latest = max(0, ctx.config.context.strategies.tool_result_placeholder.keep_latest_tool_results)
        tool_parts = [
            part
            for message in ctx.messages
            for part in message.parts
            if isinstance(part, ToolPart) and part.state.status in {"completed", "error"} and part.state.output is not None
        ]
        changed = False
        for part in reversed(tool_parts[: max(0, len(tool_parts) - keep_latest)]):
            if part.metadata.get("tool_result_compacted"):
                continue
            output = part.state.output
            if isinstance(output, dict):
                compacted = {
                    "status": output.get("status", part.state.status),
                    "tool_name": output.get("tool_name", part.tool),
                    "output": TOOL_RESULT_PLACEHOLDER,
                    "compacted": True,
                }
                if "error_type" in output:
                    compacted["error_type"] = output["error_type"]
                if "error_message" in output:
                    compacted["error_message"] = output["error_message"]
                part.state.output = compacted
            part.metadata["tool_result_compacted"] = True
            changed = True
        return changed
