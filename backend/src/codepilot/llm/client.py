from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litellm import acompletion

from codepilot.events import StreamEvent
from codepilot.logging import get_logger
from codepilot.session import LLMState, Message, SessionState, ToolPart
from codepilot.session.attachments import SUPPORTED_IMAGE_MIMES, AttachmentError, image_file_to_data_url
from codepilot.session.message import FilePart
from codepilot.session.message import AssistantMessageTokens, MessageTokenCache
from codepilot.utils import utc_now_iso

TOOL_RESULT_PLACEHOLDER = "[Old tool result content cleared]"
_SENSITIVE_KEYS = {"api_key", "token", "password", "authorization", "cookie", "secret"}


@dataclass(slots=True)
class LiteLLMStreamResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: AssistantMessageTokens | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


class LiteLLMClient:
    def __init__(self, *, log_requests: bool = False) -> None:
        self._log_requests = log_requests
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

        request = self._build_stream_request(
            llm_state=llm_state,
            provider_messages=provider_messages,
            tools=tools,
        )
        self._log_request_if_enabled(endpoint="stream_chat", request=request)
        stream = await acompletion(**request)

        tokens: AssistantMessageTokens | None = None
        agent_event_data = self._agent_event_data(session)
        async for chunk in stream:
            payload = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
            if payload.get("usage"):
                tokens = self._extract_tokens(payload["usage"])
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
                        data={**agent_event_data, "text": content},
                    )
                )
            reasoning_text = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
                await event_bus.publish_stream_event(
                    StreamEvent(
                        event_type="llm_reasoning_delta",
                        session_id=session.session_id,
                        created_at=utc_now_iso(),
                        data={**agent_event_data, "text": reasoning_text},
                    )
                )
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
        return LiteLLMStreamResult(text="".join(content_parts), reasoning="".join(reasoning_parts), tool_calls=tool_calls, tokens=tokens)

    async def complete_text(
        self,
        llm_state: LLMState,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str:
        request = self._build_text_request(
            llm_state=llm_state,
            messages=messages,
            max_tokens=max_tokens,
        )
        self._log_request_if_enabled(endpoint="complete_text", request=request)
        response = await acompletion(**request)
        payload = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    def build_provider_messages(
        self,
        messages: list[Message],
        system_prompt: str | None = None,
        runtime_context: str | None = None,
    ) -> list[dict[str, Any]]:
        provider_messages: list[dict[str, Any]] = []
        if system_prompt:
            provider_messages.append({"role": "system", "content": system_prompt})
        runtime_context_user_index = self._latest_user_message_index(messages) if runtime_context else None
        suppress_rich_content = self._should_suppress_rich_content(messages)
        for message in messages:
            role = message.info.role
            content = message.text_content()
            tool_parts = message.tool_parts()
            if role == "assistant" and tool_parts:
                completed_tool_parts = [part for part in tool_parts if part.state.status in {"completed", "error"}]
                if len(completed_tool_parts) != len(tool_parts):
                    pending_ids = [part.call_id for part in tool_parts if part.state.status not in {"completed", "error"}]
                    raise ValueError(f"assistant 工具调用尚未全部闭环，不能发送给 LLM：{pending_ids}")
                provider_messages.append(
                    {
                        "role": "assistant",
                        "content": content or "",
                        "tool_calls": [self._build_provider_tool_call(part) for part in completed_tool_parts],
                    }
                )
                provider_messages.extend(self._build_provider_tool_results(completed_tool_parts, suppress_rich_content=suppress_rich_content))
                continue
            if runtime_context_user_index is not None and message is messages[runtime_context_user_index]:
                content = self._wrap_user_request_with_runtime_context(content, runtime_context or "")
            provider_messages.append(
                {
                    "role": role,
                    "content": self._build_provider_content(
                        message,
                        content,
                        suppress_rich_content=suppress_rich_content,
                    ),
                }
            )
        return provider_messages

    def _latest_user_message_index(self, messages: list[Message]) -> int | None:
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].info.role == "user":
                return index
        return None

    def _should_suppress_rich_content(self, messages: list[Message]) -> bool:
        """非致命 LLM 错误后的下一轮只发送文本，确保诊断消息能到达模型。"""
        latest_recoverable_error_index: int | None = None
        latest_user_index: int | None = None
        for index, message in enumerate(messages):
            if message.info.role == "user":
                latest_user_index = index
            if message.info.role == "assistant" and getattr(message.info, "finish", None) == "llm_error_recoverable":
                latest_recoverable_error_index = index
        return latest_recoverable_error_index is not None and (
            latest_user_index is None or latest_recoverable_error_index > latest_user_index
        )

    def _wrap_user_request_with_runtime_context(self, content: str, runtime_context: str) -> str:
        return f"{runtime_context.strip()}\n\n<user_request>\n{content}\n</user_request>"

    def _resolve_model_name(self, llm_state: LLMState) -> str:
        prefix = llm_state.metadata.get("litellm_model_prefix", "")
        return f"{prefix}{llm_state.model}"

    def _build_stream_request(
        self,
        *,
        llm_state: LLMState,
        provider_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "model": self._resolve_model_name(llm_state),
            "messages": provider_messages,
            "stream": True,
            "tools": tools or None,
            "max_tokens": llm_state.max_tokens,
            "stream_options": {"include_usage": True},
            **self._build_provider_kwargs(llm_state),
        }

    def _build_text_request(
        self,
        *,
        llm_state: LLMState,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> dict[str, Any]:
        return {
            "model": self._resolve_model_name(llm_state),
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            **self._build_provider_kwargs(llm_state),
        }

    def _log_request_if_enabled(self, *, endpoint: str, request: dict[str, Any]) -> None:
        if not self._log_requests:
            return
        # 请求体包含用户上下文和供应商参数，日志保留业务字段但必须递归脱敏凭证。
        self._logger.info("llm api request", endpoint=endpoint, request=self._redact_request(request))

    def _redact_request(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if key.lower() in _SENSITIVE_KEYS and item is not None:
                    redacted[key] = "***REDACTED***"
                    continue
                redacted[key] = self._redact_request(item)
            return redacted
        if isinstance(value, list):
            return [self._redact_request(item) for item in value]
        if isinstance(value, str) and value.startswith("data:image/"):
            return "[image data url redacted]"
        return value

    def _build_provider_kwargs(self, llm_state: LLMState) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if llm_state.provider == "qwen":
            kwargs.update(
                {
                    "api_key": os.environ.get("QWEN_API_KEY"),
                    "api_base": os.environ.get("QWEN_BASE_URL"),
                }
            )
        if llm_state.provider == "deepseek":
            kwargs.update(
                {
                    "api_key": os.environ.get("DEEPSEEK_API_KEY"),
                    "api_base": os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
                }
            )
        thinking_value = llm_state.metadata.get("thinking_value")
        thinking_settings = self._as_dict(llm_state.metadata.get("thinking"))
        if not thinking_value or not thinking_settings:
            return kwargs
        kind = thinking_settings.get("kind")
        if kind == "reasoning_effort":
            kwargs["reasoning_effort"] = thinking_value
        if kind == "extra_body_boolean":
            extra_body_key = thinking_settings.get("extra_body_key")
            if isinstance(extra_body_key, str) and extra_body_key:
                extra_body = self._as_dict(kwargs.get("extra_body"))
                extra_body[extra_body_key] = thinking_value == "on"
                kwargs["extra_body"] = extra_body
        return kwargs

    def _agent_event_data(self, session: SessionState) -> dict[str, Any]:
        """为实时流事件补齐 agent 归属，前端据此区分主 agent 与 subagent。"""
        return {
            "agent_kind": session.metadata.get("agent_kind") or "agent",
            "context_id": session.metadata.get("agent_context_id") or "main",
            "parent_call_id": session.metadata.get("parent_call_id"),
        }

    def _extract_tokens(self, usage: Any) -> AssistantMessageTokens:
        """从不同供应商的 usage 结构中提取统一 token 统计。"""
        data = self._as_dict(usage)
        prompt_details = self._as_dict(data.get("prompt_tokens_details") or data.get("input_tokens_details"))
        completion_details = self._as_dict(data.get("completion_tokens_details") or data.get("output_tokens_details"))
        return AssistantMessageTokens(
            input=self._first_int(data, "prompt_tokens", "input_tokens"),
            output=self._first_int(data, "completion_tokens", "output_tokens"),
            reasoning=self._first_int(completion_details, "reasoning_tokens"),
            cache=MessageTokenCache(
                read=self._first_int(
                    data,
                    "cache_read_input_tokens",
                    "cached_tokens",
                    fallback=self._first_int(prompt_details, "cached_tokens", "cache_read_input_tokens"),
                )
                or 0,
                write=self._first_int(data, "cache_creation_input_tokens", fallback=self._first_int(prompt_details, "cache_creation_input_tokens"))
                or 0,
            ),
        )

    def _first_int(self, data: dict[str, Any], *keys: str, fallback: int | None = None) -> int | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
        return fallback

    def _as_dict(self, value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return value if isinstance(value, dict) else {}

    def _build_provider_tool_call(self, part: ToolPart) -> dict[str, Any]:
        return {
            "id": part.call_id,
            "type": "function",
            "function": {
                "name": part.tool,
                "arguments": json.dumps(part.state.input, ensure_ascii=False),
            },
        }

    def _build_provider_tool_results(self, tool_parts: list[ToolPart], *, suppress_rich_content: bool = False) -> list[dict[str, Any]]:
        provider_messages: list[dict[str, Any]] = []
        image_messages: list[dict[str, Any]] = []
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
            if not suppress_rich_content:
                image_message = self._build_tool_image_message(part, payload)
                if image_message:
                    image_messages.append(image_message)
        provider_messages.extend(image_messages)
        return provider_messages

    def _build_provider_content(self, message: Message, text: str, *, suppress_rich_content: bool = False) -> str | list[dict[str, Any]]:
        if suppress_rich_content:
            return text
        image_blocks = [block for part in message.parts if isinstance(part, FilePart) for block in self._file_part_image_blocks(part)]
        if not image_blocks:
            return text
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        content.extend(image_blocks)
        return content

    def _file_part_image_blocks(self, part: FilePart) -> list[dict[str, Any]]:
        if part.mime not in SUPPORTED_IMAGE_MIMES:
            return []
        raw_path = part.source.value if part.source and part.source.type == "file" else ""
        if not raw_path:
            return []
        try:
            data_url = image_file_to_data_url(Path(raw_path), part.mime)
        except (OSError, AttachmentError):
            return []
        return [{"type": "image_url", "image_url": {"url": data_url}}]

    def _build_tool_image_message(self, part: ToolPart, payload: dict[str, Any]) -> dict[str, Any] | None:
        attachments = payload.get("attachments")
        if not isinstance(attachments, list):
            return None
        blocks: list[dict[str, Any]] = []
        names: list[str] = []
        for attachment in attachments:
            if not isinstance(attachment, dict) or attachment.get("type") != "image":
                continue
            mime = str(attachment.get("mime") or "")
            source_path = str(attachment.get("source_path") or "")
            if mime not in SUPPORTED_IMAGE_MIMES or not source_path:
                continue
            try:
                data_url = image_file_to_data_url(Path(source_path), mime)
            except (OSError, AttachmentError):
                continue
            names.append(str(attachment.get("filename") or Path(source_path).name))
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        if not blocks:
            return None
        title = "、".join(names) if names else part.tool
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{part.tool} 工具读取到以下图片附件：{title}。请结合图片内容继续完成任务。"},
                *blocks,
            ],
        }
