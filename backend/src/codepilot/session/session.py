from __future__ import annotations

"""运行时主循环。

这个文件现在只负责会话级编排：
1. 维护 session 级别的开始、结束与最大轮次循环。
2. 把单轮执行委托给 TurnExecutor，保持主流程短小清晰。
3. 把人工审批委托给 ApprovalCoordinator，统一审批事件与拒绝消息处理。
"""

from dataclasses import dataclass
from typing import Any

from codepilot.events import SessionLifecycleEvent, StreamEvent
from codepilot.hooks import HookManager, HookType, RuntimeHandles
from codepilot.llm import LiteLLMClient
from codepilot.session.agents import AgentProfile
from codepilot.session.flow import ApprovalCoordinator, SessionMessageAppender, TurnExecutor, TurnResult
from codepilot.session.message import AssistantMessageInfo, ToolPart
from codepilot.session.state import AgentState, ApprovalResult, LLMState, PendingApproval, SessionState, SessionStatus
from codepilot.tools import ToolDispatcher, ToolRegistry
from codepilot.utils import utc_now_iso, utc_now_millis


@dataclass(slots=True)
class _RunContext:
    """保存一次会话运行中的稳定依赖，避免内部方法反复传长参数列表。"""

    session: SessionState
    workspace: Any
    agent_profile: AgentProfile
    runtime: RuntimeHandles
    config: Any
    approval_event: Any
    approval_result_holder: dict[str, ApprovalResult | None]
    stop_event: Any
    agent_state: AgentState
    llm_state: LLMState


class AgentLoop:
    """封装单次 Agent 会话的执行主循环。

    这个类负责会话级别的整体编排，不直接展开每一轮的细节执行。
    它更像一个“总调度器”：
    - 先处理会话开始前后的 Hook。
    - 再按最大轮次推进每一轮执行。
    - 需要人工审批时统一暂停并等待结果。
    - 最后把会话结束事件统一发出去。
    """

    def __init__(
        self,
        llm_client: LiteLLMClient,
        tool_registry: ToolRegistry,
        tool_dispatcher: ToolDispatcher,
        hook_manager: HookManager,
    ) -> None:
        """初始化会话循环依赖。

        这里会把会话运行拆成几个更小的协作对象：
        - `SessionMessageAppender` 负责补消息。
        - `ApprovalCoordinator` 负责人工审批。
        - `TurnExecutor` 负责单轮执行。

        这样做的好处是会话层只关注“怎么编排”，不用关心每一步的实现细节。
        """
        self._hook_manager = hook_manager
        self._message_appender = SessionMessageAppender()
        self._approval_coordinator = ApprovalCoordinator(message_appender=self._message_appender)
        self._turn_executor = TurnExecutor(
            llm_client=llm_client,
            tool_registry=tool_registry,
            tool_dispatcher=tool_dispatcher,
            hook_manager=hook_manager,
            message_appender=self._message_appender,
        )

    async def run(
        self,
        session: SessionState,
        workspace: Any,
        agent_profile: AgentProfile,
        runtime: RuntimeHandles,
        config: Any,
        approval_event: Any,
        approval_result_holder: dict[str, ApprovalResult | None],
        stop_event: Any,
    ) -> SessionState:
        """执行一次完整会话，直到完成、失败、取消或被人工拒绝。

        这是会话层的主入口。它会先完成会话级 Hook，再进入按轮次推进的主循环。
        单轮内部如何调用模型、工具和局部 Hook，不在这里展开，而是交给 `TurnExecutor`。
        """
        ctx = _RunContext(
            session=session,
            workspace=workspace,
            agent_profile=agent_profile,
            runtime=runtime,
            config=config,
            approval_event=approval_event,
            approval_result_holder=approval_result_holder,
            stop_event=stop_event,
            agent_state=self._build_agent_state(session, agent_profile),
            llm_state=self._build_llm_state(session, config),
        )

        try:
            should_continue = await self._run_session_before(ctx)
            if should_continue:
                await self._run_iterations(ctx)
        finally:
            # SESSION_AFTER 放在 finally 中，保证无论是成功、失败还是取消，都能执行收尾 Hook。
            await self._turn_executor._run_hook(
                HookType.SESSION_AFTER,
                session=session,
                workspace=workspace,
                agent_state=ctx.agent_state,
                llm_state=ctx.llm_state,
                runtime=runtime,
                config=config,
            )
        return await self._finish(session, runtime)

    def _build_agent_state(self, session: SessionState, agent_profile: AgentProfile) -> AgentState:
        return AgentState(
            name=agent_profile.name,
            role="main" if agent_profile.name != "subagent" else "subagent",
            depth=int(session.metadata.get("agent_depth", 0)),
            parent_agent_id=session.metadata.get("parent_agent_id"),
            can_call_subagent=agent_profile.can_call_subagent,
        )

    def _build_llm_state(self, session: SessionState, config: Any) -> LLMState:
        return LLMState(
            provider=session.provider,
            model=session.model,
            max_tokens=config.llm.max_tokens,
            temperature=config.llm.temperature,
            metadata={
                "litellm_model_prefix": config.llm_runtime.activated_providers[session.provider].litellm_model_prefix,
            },
        )

    async def _run_session_before(self, ctx: _RunContext) -> bool:
        """执行会话前置 Hook，并返回是否应该进入主循环。"""
        # SESSION_BEFORE 是整场会话的入口钩子，适合做全局预处理或提前人工确认。
        session_before = await self._turn_executor._run_hook(
            HookType.SESSION_BEFORE,
            session=ctx.session,
            workspace=ctx.workspace,
            agent_state=ctx.agent_state,
            llm_state=ctx.llm_state,
            runtime=ctx.runtime,
            config=ctx.config,
        )
        if ctx.session.status == SessionStatus.FAILED:
            return False

        if session_before.requires_human_input and session_before.human_request:
            result = await self._wait_for_approval(
                ctx,
                PendingApproval(request=session_before.human_request, source="hook"),
            )
            if self._is_rejected(result):
                return False
            ctx.session.status = SessionStatus.RUNNING

        if session_before.stop_loop:
            ctx.session.status = SessionStatus.COMPLETED
            return False

        return ctx.session.status not in {SessionStatus.FAILED, SessionStatus.CANCELLED}

    async def _run_iterations(self, ctx: _RunContext) -> None:
        """推进主循环，并在循环结束后统一发布 loop_finished。"""
        await self._publish_loop_started(ctx)
        iteration = 1
        while True:
            if iteration > ctx.agent_profile.max_iterations:
                await self._append_max_iterations_message(ctx)
                break

            if ctx.stop_event.is_set():
                ctx.session.status = SessionStatus.CANCELLED
                break

            await self._publish_iteration_started(ctx, iteration)
            turn_result = await self._execute_turn(ctx, iteration)
            should_continue = await self._handle_turn_result(ctx, turn_result, iteration)
            if not should_continue:
                break

            await self._publish_iteration_finished(ctx, iteration)
            iteration += 1

        if ctx.session.status == SessionStatus.RUNNING:
            # 如果循环自然跑完且没有显式失败或取消，就视为正常完成。
            ctx.session.status = SessionStatus.COMPLETED
        await self._publish_loop_finished(ctx)

    async def _append_max_iterations_message(self, ctx: _RunContext) -> None:
        """超过最大轮次时沉淀一条 assistant 消息，明确解释推理为何停止。"""
        max_iterations = ctx.agent_profile.max_iterations
        assistant_message = self._turn_executor._build_assistant_message(
            session=ctx.session,
            agent_state=ctx.agent_state,
            text=f"已超过最大轮推理次数限制（{max_iterations} 轮），停止推理。",
            reasoning="",
            tool_calls=[],
        )
        self._turn_executor._append_step_finish(assistant_message, reason="max_iterations")
        await self._message_appender.append(ctx.session, assistant_message, ctx.runtime)
        await self._turn_executor._publish_assistant_message_completed(ctx.session, assistant_message, ctx.runtime)

    async def _execute_turn(self, ctx: _RunContext, iteration: int) -> TurnResult:
        return await self._turn_executor.execute(
            session=ctx.session,
            workspace=ctx.workspace,
            agent_state=ctx.agent_state,
            agent_profile=ctx.agent_profile,
            llm_state=ctx.llm_state,
            runtime=ctx.runtime,
            config=ctx.config,
            iteration=iteration,
        )

    async def _handle_turn_result(self, ctx: _RunContext, turn_result: TurnResult, iteration: int) -> bool:
        """根据单轮结果决定继续、完成或退出主循环。"""
        if turn_result.status in {"completed", "stopped"}:
            ctx.session.status = SessionStatus.COMPLETED
            return False
        if turn_result.status == "failed":
            ctx.session.status = SessionStatus.FAILED
            return False
        if turn_result.status != "needs_approval" or turn_result.pending_approval is None:
            return True

        return await self._handle_pending_approval(
            ctx,
            turn_result.pending_approval,
            iteration,
            stop_after_approval=turn_result.stop_after_approval,
        )

    async def _handle_pending_approval(
        self,
        ctx: _RunContext,
        approval: PendingApproval,
        iteration: int,
        *,
        stop_after_approval: bool = False,
    ) -> bool:
        """处理单轮执行产生的人工审批，并按原逻辑恢复可能挂起的工具调用。"""
        result = await self._wait_for_approval(ctx, approval)
        if self._is_rejected(result):
            return False
        if stop_after_approval:
            return False
        if approval.resume_item is None:
            return True

        await self._resume_approved_tool_call(ctx, approval)
        return await self._run_loop_after_approved_tool(ctx, iteration)

    async def _wait_for_approval(self, ctx: _RunContext, approval: PendingApproval) -> ApprovalResult | None:
        return await self._approval_coordinator.wait(
            session=ctx.session,
            approval=approval,
            agent_state=ctx.agent_state,
            runtime=ctx.runtime,
            approval_event=ctx.approval_event,
            approval_result_holder=ctx.approval_result_holder,
        )

    def _is_rejected(self, result: ApprovalResult | None) -> bool:
        return result is not None and not result.approved

    async def _resume_approved_tool_call(self, ctx: _RunContext, approval: PendingApproval) -> None:
        # 审批通过后继续执行挂起工具，并把结果回填到最后一条 assistant 消息里。
        approved_tool_part = await self._turn_executor.tool_dispatcher.execute_approved_tool_call(
            session=ctx.session,
            workspace=ctx.workspace,
            agent=ctx.agent_state,
            item=approval.resume_item,
            runtime=ctx.runtime,
            config=ctx.config,
        )
        self._merge_approved_tool_result(ctx.session, approved_tool_part)
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="assistant_message_completed",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={"message": ctx.session.messages[-1].model_dump()},
            )
        )

    async def _run_loop_after_approved_tool(self, ctx: _RunContext, iteration: int) -> bool:
        loop_after = await self._turn_executor.run_loop_after(
            HookType.LOOP_AFTER,
            session=ctx.session,
            workspace=ctx.workspace,
            agent_state=ctx.agent_state,
            llm_state=ctx.llm_state,
            runtime=ctx.runtime,
            config=ctx.config,
            metadata={"iteration": iteration},
        )
        if loop_after.requires_human_input and loop_after.human_request:
            result = await self._wait_for_approval(
                ctx,
                PendingApproval(request=loop_after.human_request, source="hook"),
            )
            if self._is_rejected(result):
                return False
        if ctx.session.status == SessionStatus.FAILED:
            return False
        return not loop_after.stop_loop

    async def _publish_loop_started(self, ctx: _RunContext) -> None:
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(event_type="loop_started", session_id=ctx.session.session_id, created_at=utc_now_iso(), data={})
        )

    async def _publish_iteration_started(self, ctx: _RunContext, iteration: int) -> None:
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="loop_iteration_started",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={"iteration": iteration},
            )
        )

    async def _publish_iteration_finished(self, ctx: _RunContext, iteration: int) -> None:
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="loop_iteration_finished",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={"iteration": iteration},
            )
        )

    async def _publish_loop_finished(self, ctx: _RunContext) -> None:
        ctx.session.updated_at = utc_now_iso()
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="loop_finished",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={"status": ctx.session.status.value},
            )
        )

    async def _finish(self, session: SessionState, runtime: RuntimeHandles) -> SessionState:
        """统一发送会话结束事件，并返回最终会话对象。

        会话主流程里有很多提前返回点，这个方法把“结束时必须做的事情”收口到一起，
        避免不同分支各自拼装结束事件，减少遗漏风险。
        """
        session.updated_at = utc_now_iso()
        await runtime.event_bus.publish_domain_event(
            SessionLifecycleEvent(
                session_id=session.session_id,
                status=session.status.value,
                created_at=utc_now_iso(),
                data=session.model_dump(exclude={"messages"}),
            )
        )
        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="session_finished" if session.status != SessionStatus.FAILED else "session_failed",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={"status": session.status.value},
            )
        )
        return session

    def _merge_approved_tool_result(self, session: SessionState, approved_tool_part: ToolPart) -> None:
        """把审批后执行得到的工具结果合并回最后一条 assistant 消息。

        正常情况下，模型先产出一条带工具调用的 assistant 消息，
        工具结果会在后续步骤补进去。这里专门处理“工具因为审批而延后执行”的场景。

        合并策略很简单：
        - 如果已经存在同 `call_id` 的工具片段，就原地替换。
        - 如果还不存在，就把结果追加到消息末尾。
        """
        if not session.messages:
            return
        latest_message = session.messages[-1]
        if latest_message.info.role != "assistant":
            return
        merged_parts: list[object] = []
        replaced = False
        for part in latest_message.parts:
            # 通过 call_id 精确匹配待替换的工具片段，避免误改同一条消息中的其他工具结果。
            if isinstance(part, ToolPart) and part.call_id == approved_tool_part.call_id:
                merged_parts.append(approved_tool_part)
                replaced = True
                continue
            merged_parts.append(part)
        if not replaced:
            # 理论上大多数时候会命中替换分支；如果没命中，说明结果尚未写入过，直接追加即可。
            merged_parts.append(approved_tool_part)
        latest_message.parts = merged_parts
        assert isinstance(latest_message.info, AssistantMessageInfo)
        # 工具结果补齐后，把消息完成时间和结束原因同步更新，方便前端正确展示状态。
        latest_message.info.time.completed = utc_now_millis()
        latest_message.info.finish = "tool_completed"
