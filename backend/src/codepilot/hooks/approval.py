"""系统内置审批 Hook。"""

from __future__ import annotations

from uuid import uuid4

from codepilot.hooks.base import BaseHook
from codepilot.hooks.contracts import HookContext, HookResult
from codepilot.session import ApprovalRequest
from codepilot.utils import utc_now_iso


class ApprovalHook(BaseHook):
    """在命中特定标记时触发一次人工审批。"""

    marker: str = "[[approve]]"

    async def execute(self, ctx: HookContext) -> HookResult:
        """检测当前消息是否命中审批标记，并按需返回审批请求。"""
        if not ctx.current_message:
            return HookResult()
        if ctx.session.metadata.get("approval_done"):
            return HookResult()
        text = ctx.current_message.text_content()
        if self.marker not in text:
            return HookResult()
        return HookResult(
            requires_human_input=True,
            human_request=ApprovalRequest(
                approval_id=f"approval_{uuid4().hex}",
                reason="命中了审批 Hook，请确认是否继续执行。",
                action={"type": "hook_gate", "marker": self.marker},
                created_at=utc_now_iso(),
            ),
            context_patch={"approval_done": True},
        )
