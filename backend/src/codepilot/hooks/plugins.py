"""插件 Hook 基础类型。

这里放置四类插件 Hook 的基础实现：
- `PromptPluginHook` 用于向会话补充提示消息。
- `CommandPluginHook` 预留命令类插件入口。
- `HttpPluginHook` 预留 HTTP 类插件入口。
- `AgentPluginHook` 预留 Agent 类插件入口。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.hooks.base import BaseHook
from codepilot.hooks.contracts import HookContext, HookResult
from codepilot.session import Message, TextPart, build_assistant_message_info, build_user_message_info
from codepilot.utils import utc_now_millis


class PromptPluginHook(BaseHook):
    """把配置中的提示内容追加为一条 synthetic 会话消息。"""

    role: str = "system"
    content: str = ""

    async def execute(self, ctx: HookContext) -> HookResult:
        """根据配置角色构造消息，并把它追加到会话历史中。"""
        # 会话历史只允许 user/assistant 两种角色，system 配置统一降级为 user 消息。
        role = "assistant" if self.role == "assistant" else "user"
        message_id = f"msg_{uuid4().hex}"
        message = Message(
            info=(
                build_assistant_message_info(
                    message_id=message_id,
                    session_id=ctx.session.session_id,
                    created_at_ms=utc_now_millis(),
                    parent_id=self._find_latest_user_message_id(ctx),
                    agent=ctx.agent.name,
                    provider_id=ctx.session.provider,
                    model_id=ctx.session.model,
                    cwd=str(Path.cwd()),
                    root=ctx.session.workspace_path,
                )
                if role == "assistant"
                else build_user_message_info(
                    message_id=message_id,
                    session_id=ctx.session.session_id,
                    created_at_ms=utc_now_millis(),
                    agent=ctx.agent.name,
                    provider_id=ctx.session.provider,
                    model_id=ctx.session.model,
                )
            ),
            parts=[TextPart(text=self.content, synthetic=True)],
        )
        return HookResult(messages_to_append=[message])

    def _find_latest_user_message_id(self, ctx: HookContext) -> str:
        """查找最新用户消息，作为 synthetic assistant 消息的父节点。"""
        # Hook 追加 assistant 文本时，保持和主执行链相同的父消息关联规则。
        for message in reversed(ctx.session.messages):
            if message.info.role == "user":
                return message.info.id
        raise ValueError("Hook 追加 assistant 消息时缺少用户父消息")


class CommandPluginHook(BaseHook):
    """预留给命令类插件的 Hook 实现入口。"""

    config: dict[str, Any] = {}

    async def execute(self, ctx: HookContext) -> HookResult:
        """暂不处理命令插件，保持空结果以维持统一调用契约。"""
        return HookResult()


class HttpPluginHook(BaseHook):
    """预留给 HTTP 类插件的 Hook 实现入口。"""

    config: dict[str, Any] = {}

    async def execute(self, ctx: HookContext) -> HookResult:
        """暂不处理 HTTP 插件，保持空结果以维持统一调用契约。"""
        return HookResult()


class AgentPluginHook(BaseHook):
    """预留给 Agent 类插件的 Hook 实现入口。"""

    config: dict[str, Any] = {}

    async def execute(self, ctx: HookContext) -> HookResult:
        """暂不处理 Agent 插件，保持空结果以维持统一调用契约。"""
        return HookResult()
