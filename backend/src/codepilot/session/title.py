"""会话标题生成服务。"""

from __future__ import annotations

from typing import Any

from codepilot.events import SessionMetaEvent, StreamEvent
from codepilot.llm import LiteLLMClient
from codepilot.logging import get_logger
from codepilot.session.message import TextPart
from codepilot.session.state import LLMState, SessionState
from codepilot.utils import utc_now_iso


class SessionTitleService:
    """为新会话异步生成短标题，不参与 Hook 生命周期。"""

    def __init__(
        self,
        *,
        provider: str = "qwen",
        model: str = "qwen3.5-flash",
        litellm_model_prefix: str = "openai/",
        title_limit: int = 15,
    ) -> None:
        self.provider = provider
        self.model = model
        self.litellm_model_prefix = litellm_model_prefix
        self.title_limit = title_limit
        self._logger = get_logger("codepilot.session.title")

    async def generate_for_session(self, session: SessionState, event_bus: Any) -> None:
        """生成并发布会话标题；失败时只记录日志，不影响主会话执行。"""
        try:
            if not self._should_generate(session):
                return
            title = await self._generate_title(session)
            if not title:
                return
            session.title = title
            session.updated_at = utc_now_iso()
            await self._publish_title_events(session, event_bus, title)
        except Exception as exc:  # noqa: BLE001
            # 标题只影响历史列表展示，异常不能阻断 Agent 主流程。
            self._logger.warning("generate session title failed", session_id=session.session_id, error=str(exc))

    def _should_generate(self, session: SessionState) -> bool:
        """只允许新会话的第一条真实用户消息触发标题生成。"""
        user_messages = [
            message
            for message in session.messages
            if message.info.role == "user" and not any(isinstance(part, TextPart) and part.synthetic for part in message.parts)
        ]
        return len(user_messages) == 1 and bool(user_messages[0].text_content().strip())

    async def _generate_title(self, session: SessionState) -> str:
        source_text = session.messages[-1].text_content().strip()
        llm_state = LLMState(
            provider=self.provider,
            model=self.model,
            max_tokens=64,
            temperature=0,
            metadata={"litellm_model_prefix": self.litellm_model_prefix},
        )
        response = await LiteLLMClient().complete_text(
            llm_state=llm_state,
            messages=[
                {
                    "role": "system",
                    "content": "你是会话标题生成器。只输出一个不超过15字符的中文短标题，不要解释、引号、Markdown 或句号。",
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

    async def _publish_title_events(self, session: SessionState, event_bus: Any, title: str) -> None:
        await event_bus.publish_domain_event(
            SessionMetaEvent(
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={"title": title, "updated_at": session.updated_at},
            )
        )
        await event_bus.publish_stream_event(
            StreamEvent(
                event_type="session_title_updated",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={"title": title},
            )
        )
