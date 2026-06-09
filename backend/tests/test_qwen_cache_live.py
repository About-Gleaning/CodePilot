from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from codepilot.llm import LiteLLMClient, LiteLLMStreamResult
from codepilot.session import LLMState, SessionState, SessionStatus


class RecordingStreamBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish_stream_event(self, event: object) -> object:
        self.events.append(event)
        return event


def _load_live_env() -> None:
    load_dotenv(Path(__file__).parents[1] / ".env", override=False)


def _cache_read(result: LiteLLMStreamResult) -> int:
    if result.tokens is None or result.tokens.cache is None:
        return 0
    return result.tokens.cache.read or 0


def _format_usage(label: str, result: LiteLLMStreamResult) -> str:
    tokens = result.tokens
    cache = tokens.cache if tokens else None
    return (
        f"{label}: "
        f"input={tokens.input if tokens else None}, "
        f"output={tokens.output if tokens else None}, "
        f"reasoning={tokens.reasoning if tokens else None}, "
        f"cache.read={cache.read if cache else None}, "
        f"cache.write={cache.write if cache else None}"
    )


def _long_cache_probe_prompt() -> str:
    # Qwen 隐式缓存对短输入不稳定，使用稳定长文本提高真实命中概率。
    paragraph = (
        "请只回答“收到”。以下是用于测试上下文缓存的固定文本，不需要总结。"
        "缓存测试要求两次请求的输入内容完全一致，因此本段文本会重复多次。"
        "文本包含产品需求、约束、边界条件、验收标准和实现说明，目的是形成足够长的提示词。"
        "如果模型支持隐式缓存，第二次相同请求可能在 usage.prompt_tokens_details.cached_tokens 中返回命中 token。"
    )
    return "\n".join(f"{index:03d}. {paragraph}" for index in range(90))


async def test_qwen_kimi_k25_repeated_prompt_reports_cache_usage() -> None:
    _load_live_env()
    if os.environ.get("RUN_QWEN_CACHE_LIVE_TESTS") != "1":
        pytest.skip("设置 RUN_QWEN_CACHE_LIVE_TESTS=1 后才执行 qwen/kimi-k2.5 缓存 live 测试")
    if not os.environ.get("QWEN_API_KEY") or not os.environ.get("QWEN_BASE_URL"):
        pytest.skip("缺少 QWEN_API_KEY 或 QWEN_BASE_URL，跳过 qwen/kimi-k2.5 缓存 live 测试")

    client = LiteLLMClient()
    session = SessionState(
        session_id="session_qwen_cache_live",
        workspace_id="ws_qwen_cache_live",
        workspace_path="/tmp/codepilot",
        agent_name="build",
        provider="qwen",
        model="kimi-k2.5",
        status=SessionStatus.RUNNING,
        created_at="2026-06-04T00:00:00Z",
        updated_at="2026-06-04T00:00:00Z",
    )
    llm_state = LLMState(
        provider="qwen",
        model="kimi-k2.5",
        max_tokens=32,
        metadata={"litellm_model_prefix": "openai/"},
    )
    provider_messages = [{"role": "user", "content": _long_cache_probe_prompt()}]

    first = await client.stream_chat(
        session=session,
        llm_state=llm_state,
        provider_messages=provider_messages,
        tools=[],
        event_bus=RecordingStreamBus(),
    )
    second = await client.stream_chat(
        session=session,
        llm_state=llm_state,
        provider_messages=provider_messages,
        tools=[],
        event_bus=RecordingStreamBus(),
    )

    print(_format_usage("first", first))
    print(_format_usage("second", second))
    print(f"second_cache_hit={_cache_read(second) > 0}")

    assert first.text
    assert second.text
    assert first.tokens is not None
    assert second.tokens is not None
