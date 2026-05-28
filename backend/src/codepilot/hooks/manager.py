"""Hook 调度器。

这个模块负责按生命周期节点注册、筛选并顺序执行 Hook，
同时统一处理超时、异常策略、事件上报和多个 Hook 结果的合并。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from codepilot.events import StreamEvent
from codepilot.hooks.base import BaseHook, HookErrorPolicy, HookType
from codepilot.hooks.contracts import HookContext, HookError, HookResult
from codepilot.logging import get_logger
from codepilot.session import ApprovalRequest
from codepilot.utils import utc_now_iso


class HookManager:
    """维护 Hook 注册表，并按统一规则驱动 Hook 链执行。"""

    def __init__(self) -> None:
        """初始化按 Hook 类型分组的注册表与日志器。"""
        self._hooks: dict[HookType, list[BaseHook]] = defaultdict(list)
        self._logger = get_logger("codepilot.hooks")

    def register(self, hook: BaseHook) -> None:
        """注册一个 Hook，并按执行顺序重新排序同类 Hook。"""
        self._hooks[hook.hook_type].append(hook)
        self._hooks[hook.hook_type].sort(key=lambda item: item.order)

    def get_hooks(self, hook_type: HookType) -> list[BaseHook]:
        """返回指定生命周期节点下所有已启用的 Hook。"""
        return [hook for hook in self._hooks.get(hook_type, []) if hook.enabled]

    async def run(self, hook_type: HookType, ctx: HookContext) -> HookResult:
        """顺序执行匹配的 Hook，并合并它们对运行时的影响。"""
        merged = HookResult()
        for hook in self.get_hooks(hook_type):
            if not self._matches(hook, ctx):
                # 仅执行命中当前 Agent 或工具范围的 Hook，避免插件误作用到无关流程。
                continue

            await self._emit_event(ctx, "hook_started", {"hook_id": hook.hook_id, "hook_type": hook.hook_type.value})
            try:
                hook_result = await self._execute_with_timeout(hook, ctx)
            except Exception as exc:  # noqa: BLE001
                self._logger.exception("hook execute failed", hook_id=hook.hook_id, hook_type=hook.hook_type.value, error=str(exc))
                await self._emit_event(
                    ctx,
                    "hook_failed",
                    {"hook_id": hook.hook_id, "hook_type": hook.hook_type.value, "error": str(exc)},
                )
                hook_result = self._handle_error(hook, exc)
            else:
                await self._emit_event(ctx, "hook_finished", {"hook_id": hook.hook_id, "hook_type": hook.hook_type.value})

            merged = self._merge_results(merged, hook_result)
            if merged.stop_loop or merged.fail_session or merged.requires_human_input:
                # 一旦 Hook 已经改变主流程走向，后续 Hook 不再继续叠加副作用。
                break
        return merged

    async def _execute_with_timeout(self, hook: BaseHook, ctx: HookContext) -> HookResult:
        """按 Hook 配置决定是否启用超时保护后再执行。"""
        if hook.timeout_seconds:
            return await asyncio.wait_for(hook.execute(ctx), timeout=hook.timeout_seconds)
        return await hook.execute(ctx)

    def _handle_error(self, hook: BaseHook, exc: Exception) -> HookResult:
        """把异常转换成统一的 HookResult，并套用当前 Hook 的错误策略。"""
        error = HookError(code="hook_error", message=str(exc))
        if hook.on_error == HookErrorPolicy.BREAK_LOOP:
            return HookResult(status="error", stop_loop=True, error=error)
        if hook.on_error == HookErrorPolicy.FAIL_SESSION:
            return HookResult(status="error", fail_session=True, error=error)
        if hook.on_error == HookErrorPolicy.REQUIRE_HUMAN:
            return HookResult(
                status="need_human",
                requires_human_input=True,
                human_request=ApprovalRequest(
                    approval_id=f"approval_hook_{utc_now_iso().replace(':', '').replace('-', '')}",
                    reason=f"Hook 执行失败，需要人工确认：{hook.hook_id}",
                    created_at=utc_now_iso(),
                ),
                error=error,
            )
        return HookResult(status="error", error=error)

    def _matches(self, hook: BaseHook, ctx: HookContext) -> bool:
        """判断 Hook 是否适用于当前 Agent 与工具调用上下文。"""
        if hook.applies_to_agents and ctx.agent.name not in hook.applies_to_agents:
            return False
        if hook.applies_to_tools and ctx.tool_call:
            tool_name = ctx.tool_call.get("tool_name")
            if tool_name not in hook.applies_to_tools:
                return False
        return True

    def _merge_results(self, left: HookResult, right: HookResult) -> HookResult:
        """按既定优先级合并两个 HookResult，保留累计副作用。"""
        return HookResult(
            status=right.status if right.status != "ok" else left.status,
            messages_to_append=[*left.messages_to_append, *right.messages_to_append],
            events_to_emit=[*left.events_to_emit, *right.events_to_emit],
            context_patch={**left.context_patch, **right.context_patch},
            stop_loop=left.stop_loop or right.stop_loop,
            fail_session=left.fail_session or right.fail_session,
            requires_human_input=left.requires_human_input or right.requires_human_input,
            human_request=right.human_request or left.human_request,
            error=right.error or left.error,
        )

    async def _emit_event(self, ctx: HookContext, event_type: str, data: dict[str, object]) -> None:
        """在存在运行时句柄时，把 Hook 生命周期事件发布到事件总线。"""
        if ctx.runtime is None:
            return
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type=event_type,
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data=data,
            )
        )
