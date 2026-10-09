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
        if any(item.hook_id == hook.hook_id for hooks in self._hooks.values() for item in hooks):
            raise ValueError("Hook ID 重复")
        self._hooks[hook.hook_type].append(hook)
        self._hooks[hook.hook_type].sort(key=lambda item: (item.order, item.hook_id))

    def get_hooks(self, hook_type: HookType) -> list[BaseHook]:
        """返回指定生命周期节点下所有已启用的 Hook。"""
        return [hook for hook in self._hooks.get(hook_type, []) if hook.enabled]

    async def run(self, hook_type: HookType, ctx: HookContext) -> HookResult:
        """顺序执行匹配的 Hook，并合并它们对运行时的影响。"""
        merged = HookResult()
        for hook in self.get_hooks(hook_type):
            selected = getattr(ctx.agent, "hook_ids", None)
            if hook.selectable and selected is not None and hook.hook_id not in selected:
                continue
            if not self._matches(hook, ctx):
                # 仅执行命中当前 Agent 或工具范围的 Hook，避免插件误作用到无关流程。
                continue
            if self._already_ran_once(hook, ctx):
                continue
            self._mark_once_if_needed(hook, ctx)

            await self._emit_event(ctx, "hook_started", {"hook_id": hook.hook_id, "hook_type": hook.hook_type.value, "version": hook.resource_version})
            try:
                hook = self._instance(hook, ctx)
                ctx.metadata["hook_parameters"] = getattr(ctx.agent, "hook_parameters", {}).get(hook.hook_id, {})
                hook_result = await self._execute_with_timeout(hook, ctx)
                self._validate_result(hook, hook_result, ctx)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("hook execute failed", hook_id=hook.hook_id, hook_type=hook.hook_type.value)
                await self._emit_event(
                    ctx,
                    "hook_failed",
                    {"hook_id": hook.hook_id, "hook_type": hook.hook_type.value, "error": "Hook 执行失败"},
                )
                from codepilot.hooks.protocol import HookOutcomeUncertain
                if isinstance(exc, HookOutcomeUncertain) or (isinstance(exc, TimeoutError) and getattr(hook, "protocol_version", 0)):
                    hook_result = HookResult(fail_session=True, error=HookError(
                        code="hook_outcome_uncertain", message="Hook 外部执行结果不确定，禁止自动重试"))
                else:
                    hook_result = self._handle_error(hook, ValueError("Hook 执行失败"))
            else:
                await self._emit_event(ctx, "hook_finished", {"hook_id": hook.hook_id, "hook_type": hook.hook_type.value, "version": hook.resource_version})

            merged = self._merge_results(merged, hook_result)
            if merged.requires_human_input and ctx.runtime and ctx.runtime.hook_approval:
                # 原调用栈就是存活 Run 的恢复位置，不重新进入模型或重放已完成的 Hook。
                merged = await ctx.runtime.hook_approval(merged)
            if merged.stop_loop or merged.fail_session or merged.requires_human_input:
                # 一旦 Hook 已经改变主流程走向，后续 Hook 不再继续叠加副作用。
                break
        return merged

    def _instance(self, hook: BaseHook, ctx: HookContext) -> BaseHook:
        if not hook.selectable or ctx.runtime is None:
            return hook
        key = (ctx.agent.context_id or "main", hook.hook_id)
        if key not in ctx.runtime.hook_instances:
            instance = hook.model_copy(deep=True)
            parameters = getattr(ctx.agent, "hook_parameters", {}).get(hook.hook_id, {})
            validate_parameters(parameters, hook.parameter_definitions)
            if hasattr(instance, "content"):
                instance.content = render_parameters(instance.content, parameters)
            if hasattr(instance, "config") and "argv" in instance.config:
                argv = instance.config["argv"]
                instance.config["argv"] = [argv[0], *(render_parameters(value, parameters) for value in argv[1:])]
            ctx.runtime.hook_instances[key] = instance
        return ctx.runtime.hook_instances[key]

    def _validate_result(self, hook: BaseHook, result: HookResult, ctx: HookContext) -> None:
        if result.context_patch:
            # 插件状态只能进入专属命名空间，不能覆盖身份、授权、凭证或恢复标记。
            if not hook.allow_modify_context:
                raise ValueError("Hook 不允许修改上下文")
            if hook.selectable:
                result.context_patch = {f"hook_data:{hook.hook_id}": result.context_patch}
        if result.messages_to_append and not hook.allow_emit_message:
            raise ValueError("Hook 不允许追加消息")
        if result.events_to_emit and not hook.allow_emit_event:
            raise ValueError("Hook 不允许发送事件")
        for message in result.messages_to_append:
            message.info.session_id = ctx.session.session_id
            message.info.context_id = ctx.agent.context_id
            message.info.agent_kind = ctx.agent.kind
            message.info.parent_call_id = ctx.agent.parent_call_id
        for event in result.events_to_emit:
            event.session_id = ctx.session.session_id
            if hook.selectable:
                event.data = {"hook_id": hook.hook_id, "name": event.event_type, "payload": event.data}
                event.event_type = "hook_custom"
            event.data = {**event.data, "context_id": ctx.agent.context_id, "agent_kind": ctx.agent.kind,
                          "parent_call_id": ctx.agent.parent_call_id}

    async def _execute_with_timeout(self, hook: BaseHook, ctx: HookContext) -> HookResult:
        """按 Hook 配置决定是否启用超时保护后再执行。"""
        stop = getattr(ctx.runtime, "stop_event", None)
        if stop is not None and stop.is_set():
            return HookResult(stop_loop=True)
        execution = asyncio.create_task(asyncio.wait_for(hook.execute(ctx), timeout=hook.timeout_seconds or None))
        stopped = asyncio.create_task(stop.wait()) if stop is not None else None
        try:
            if stopped is None:
                return await execution
            await asyncio.wait({execution, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if execution.done():
                return await execution
            return HookResult(stop_loop=True)
        finally:
            # 必须等待插件取消清理完成，Command 的进程组不能在此处成为后台任务。
            for task in (execution, stopped):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (execution, stopped) if task is not None), return_exceptions=True)

    def _handle_error(self, hook: BaseHook, exc: Exception) -> HookResult:
        """把异常转换成统一的 HookResult，并套用当前 Hook 的错误策略。"""
        error = HookError(code="hook_error", message=str(exc))
        if hook.on_error == HookErrorPolicy.BREAK_LOOP:
            if getattr(hook, "protocol_version", 0):
                error = HookError(code="hook_stopped", message="Hook 错误策略要求停止")
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

    def _already_ran_once(self, hook: BaseHook, ctx: HookContext) -> bool:
        """检查一次性 Hook 是否已经在当前 session 中调度过。"""
        if not hook.run_once_per_session:
            return False
        once_state = ctx.session.metadata.get("hook_once")
        key = f"{ctx.agent.context_id or 'main'}:{hook.hook_id}"
        return isinstance(once_state, dict) and bool(once_state.get(key) or (ctx.agent.context_id == "main" and once_state.get(hook.hook_id)))

    def _mark_once_if_needed(self, hook: BaseHook, ctx: HookContext) -> None:
        """一次性 Hook 在调用前即标记，确保失败后也不会在后续用户消息重试。"""
        if not hook.run_once_per_session:
            return
        once_state = ctx.session.metadata.setdefault("hook_once", {})
        if isinstance(once_state, dict):
            once_state[f"{ctx.agent.context_id or 'main'}:{hook.hook_id}"] = utc_now_iso()

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
                data={**data, "context_id": ctx.agent.context_id, "agent_kind": ctx.agent.kind,
                      "parent_call_id": ctx.agent.parent_call_id,
                      "run_id": getattr(ctx.runtime.run_ref, "run_id", None)},
            )
        )


def validate_parameters(values: dict, definitions: dict, *, allow_missing: bool = False) -> None:
    if set(values) - set(definitions):
        raise ValueError("Hook 参数未由管理员开放")
    for name, definition in definitions.items():
        value = values.get(name)
        if value is None:
            if definition.get("required") and not allow_missing:
                raise ValueError("Hook 缺少必填参数")
            continue
        expected = {"string": str, "integer": int, "boolean": bool}[definition["type"]]
        if type(value) is not expected or len(str(value)) > 2000:
            raise ValueError("Hook 参数类型或长度无效")
        if definition.get("choices") and value not in definition["choices"]:
            raise ValueError("Hook 参数不在允许选项中")


def render_parameters(template: str, parameters: dict) -> str:
    for name, value in parameters.items():
        template = template.replace("{{" + name + "}}", str(value))
    return template
