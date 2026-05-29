"""系统内置会话标题生成 Hook。"""

from __future__ import annotations

from codepilot.events import SessionLifecycleEvent, StreamEvent
from codepilot.hooks.base import BaseHook
from codepilot.hooks.contracts import HookContext, HookResult
from codepilot.llm import LiteLLMClient
from codepilot.session import TextPart
from codepilot.session.state import LLMState
from codepilot.utils import utc_now_iso


class SessionTitleHook(BaseHook):
    """在新会话第一条用户消息进入 Agent 前生成短标题。"""

    provider: str = "qwen"
    model: str = "qwen3.5-flash"
    title_limit: int = 20

    async def execute(self, ctx: HookContext) -> HookResult:
        """生成会话标题；失败时只记录空结果，不影响主流程。"""
        try:
            if not self._should_generate(ctx):
                return HookResult()
            title = await self._generate_title(ctx)
            if not title:
                return HookResult()
            ctx.session.title = title
            ctx.session.updated_at = utc_now_iso()
            await self._publish_title_events(ctx, title)
        except Exception:  # noqa: BLE001
            # 标题只是历史列表展示增强，任何异常都不能阻断主会话进入 Agent。
            return HookResult()
        return HookResult()

    def _should_generate(self, ctx: HookContext) -> bool:
        """只允许新会话的第一条真实用户消息触发标题生成。"""
        if ctx.session.title:
            return False
        user_messages = [
            message
            for message in ctx.session.messages
            if message.info.role == "user" and not any(isinstance(part, TextPart) and part.synthetic for part in message.parts)
        ]
        return len(user_messages) == 1 and bool(user_messages[0].text_content().strip())

    async def _generate_title(self, ctx: HookContext) -> str:
        provider = ctx.config.llm_runtime.activated_providers.get(self.provider)
        if provider is None:
            return ""
        source_text = ctx.session.messages[-1].text_content().strip()
        llm_state = LLMState(
            provider=self.provider,
            model=self.model,
            max_tokens=64,
            temperature=0,
            metadata={"litellm_model_prefix": provider.litellm_model_prefix},
        )
        response = await LiteLLMClient().complete_text(
            llm_state=llm_state,
            messages=[
                {
                    "role": "system",
                    "content": "你是会话标题生成器。只输出一个不超过20字符的中文短标题，不要解释、引号、Markdown 或句号。",
                },
                {
                    "role": "user",
                    "content": f"请为下面用户输入生成标题：\n{source_text}",
                },
            ],
            max_tokens=64,
        )
        return self._clean_title(response)

    def _clean_title(self, value: str) -> str:
        normalized = " ".join(str(value or "").split())
        title = normalized.strip("`*_#[]()（）「」『』《》“”\"'：:，,。.!！?？、-— ")
        return title[: self.title_limit]

    async def _publish_title_events(self, ctx: HookContext, title: str) -> None:
        if ctx.runtime is None:
            return
        await ctx.runtime.event_bus.publish_domain_event(
            SessionLifecycleEvent(
                session_id=ctx.session.session_id,
                status=ctx.session.status.value,
                created_at=utc_now_iso(),
                data=ctx.session.model_dump(exclude={"messages"}),
            )
        )
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="session_title_updated",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={"title": title},
            )
        )
