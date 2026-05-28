from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from litellm import acompletion

from codepilot.events import StreamEvent
from codepilot.logging import get_logger
from codepilot.session import LLMState, Message, SessionState, ToolPart
from codepilot.utils import utc_now_iso

TOOL_RESULT_PLACEHOLDER = "[Old tool result content cleared]"


@dataclass(slots=True)
class LiteLLMStreamResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_response: dict[str, Any] = field(default_factory=dict)


class LiteLLMClient:
    def __init__(self) -> None:
        self._logger = get_logger("codepilot.llm")

    async def stream_chat(
        self,
        session: SessionState,
        llm_state: LLMState,
        provider_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        event_bus: Any,
    ) -> LiteLLMStreamResult:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_map: dict[int, dict[str, Any]] = defaultdict(lambda: {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})

        stream = await acompletion(
            model=self._resolve_model_name(llm_state),
            messages=provider_messages,
            stream=True,
            tools=tools or None,
            max_tokens=llm_state.max_tokens,
            temperature=llm_state.temperature,
            **self._build_provider_kwargs(llm_state),
        )

        async for chunk in stream:
            payload = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
            choices = payload.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("content"):
                content = delta["content"]
                content_parts.append(content)
                await event_bus.publish_stream_event(
                    StreamEvent(
                        event_type="llm_delta",
                        session_id=session.session_id,
                        created_at=utc_now_iso(),
                        data={"text": content},
                    )
                )
            reasoning_text = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
            for tool_call in delta.get("tool_calls") or []:
                index = tool_call.get("index", 0)
                existing = tool_call_map[index]
                if tool_call.get("id"):
                    existing["id"] = tool_call["id"]
                function = tool_call.get("function") or {}
                if function.get("name"):
                    existing["function"]["name"] += function["name"]
                if function.get("arguments"):
                    existing["function"]["arguments"] += function["arguments"]

        tool_calls: list[dict[str, Any]] = []
        for item in tool_call_map.values():
            if not item["function"]["name"]:
                continue
            try:
                arguments = json.loads(item["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {"raw_arguments": item["function"]["arguments"]}
            tool_calls.append(
                {
                    "tool_call_id": item["id"],
                    "tool_name": item["function"]["name"],
                    "arguments": arguments,
                }
            )
        return LiteLLMStreamResult(text="".join(content_parts), reasoning="".join(reasoning_parts), tool_calls=tool_calls)

    async def complete_text(
        self,
        llm_state: LLMState,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str:
        response = await acompletion(
            model=self._resolve_model_name(llm_state),
            messages=messages,
            stream=False,
            max_tokens=max_tokens,
            temperature=llm_state.temperature,
            **self._build_provider_kwargs(llm_state),
        )
        payload = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    def build_provider_messages(self, messages: list[Message], system_prompt: str | None = None) -> list[dict[str, Any]]:
        provider_messages: list[dict[str, Any]] = []
        if system_prompt:
            provider_messages.append({"role": "system", "content": system_prompt})
        for message in messages:
            role = message.info.role
            content = message.text_content()
            tool_parts = message.tool_parts()
            if role == "assistant" and tool_parts:
                provider_messages.append(
                    {
                        "role": "assistant",
                        "content": content or "",
                        "tool_calls": [self._build_provider_tool_call(part) for part in tool_parts],
                    }
                )
                provider_messages.extend(self._build_provider_tool_results(tool_parts))
                continue
            provider_messages.append({"role": role, "content": content})
        return provider_messages

    def _resolve_model_name(self, llm_state: LLMState) -> str:
        prefix = llm_state.metadata.get("litellm_model_prefix", "")
        return f"{prefix}{llm_state.model}"

    def _build_provider_kwargs(self, llm_state: LLMState) -> dict[str, Any]:
        if llm_state.provider == "qwen":
            return {
                "api_key": os.environ.get("QWEN_API_KEY"),
                "api_base": os.environ.get("QWEN_BASE_URL"),
            }
        return {}

    def _build_provider_tool_call(self, part: ToolPart) -> dict[str, Any]:
        return {
            "id": part.call_id,
            "type": "function",
            "function": {
                "name": part.tool,
                "arguments": json.dumps(part.state.input, ensure_ascii=False),
            },
        }

    def _build_provider_tool_results(self, tool_parts: list[ToolPart]) -> list[dict[str, Any]]:
        provider_messages: list[dict[str, Any]] = []
        for part in tool_parts:
            if part.state.status not in {"completed", "error"}:
                continue
            payload = part.state.output or {}
            content = TOOL_RESULT_PLACEHOLDER if part.metadata.get("tool_result_compacted") else json.dumps(payload, ensure_ascii=False)
            provider_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": part.call_id,
                    "name": part.tool,
                    "content": content,
                }
            )
        return provider_messages
