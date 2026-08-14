from __future__ import annotations

"""运行时主循环。

这个文件现在只负责会话级编排：
1. 维护 session 级别的开始、结束与最大轮次循环。
2. 把单轮执行委托给 TurnExecutor，保持主流程短小清晰。
3. 把人工审批委托给 ApprovalCoordinator，统一审批事件与拒绝消息处理。
"""

from dataclasses import dataclass
from typing import Any

from codepilot.events import HumanInteractionEvent, SessionLifecycleEvent, StreamEvent
from codepilot.hooks import HookManager, HookType, RuntimeHandles
from codepilot.llm import LiteLLMClient
from codepilot.skills import SkillRegistry
from codepilot.session.agents import AgentProfile
from codepilot.session.interactions import ApprovalCoordinator, QuestionCoordinator, SessionMessageAppender, find_tool_message_id
from codepilot.session.message_ops import (
    build_question_tool_output,
    merge_approved_tool_result,
    merge_approved_tool_results,
    merge_question_result,
    summarize_question_answers,
)
from codepilot.session.flow import (
    TurnExecutor,
    TurnResult,
)
from codepilot.session.message import Message, TextPart, build_user_message_info
from codepilot.session.state import AgentState, ApprovalResult, LLMState, PendingApproval, PendingQuestion, QuestionResult, SessionState, SessionStatus
from codepilot.tools import ToolDispatcher, ToolExecutionBatch, ToolRegistry, ToolResumeBatch
from codepilot.utils import new_message_id, new_context_id, utc_now_iso, utc_now_millis


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
    question_event: Any
    question_result_holder: dict[str, QuestionResult | None]
    stop_event: Any
    agent_state: AgentState
    llm_state: LLMState
    allow_manual_approval: bool = True
    allow_question_interaction: bool = True


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
        skill_registry: SkillRegistry | None = None,
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
        self._question_coordinator = QuestionCoordinator(message_appender=self._message_appender)
        self._turn_executor = TurnExecutor(
            llm_client=llm_client,
            tool_registry=tool_registry,
            tool_dispatcher=tool_dispatcher,
            hook_manager=hook_manager,
            message_appender=self._message_appender,
            skill_registry=skill_registry,
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
        question_event: Any | None = None,
        question_result_holder: dict[str, QuestionResult | None] | None = None,
        allow_manual_approval: bool = True,
        allow_question_interaction: bool = True,
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
            question_event=question_event,
            question_result_holder=question_result_holder or {"result": None},
            stop_event=stop_event,
            agent_state=self._build_agent_state(session, agent_profile),
            llm_state=self._build_llm_state(session, config),
            allow_manual_approval=allow_manual_approval,
            allow_question_interaction=allow_question_interaction,
        )

        try:
            should_continue = await self._run_session_before(ctx)
            if should_continue:
                await self._run_iterations(ctx)
        finally:
            # SESSION_AFTER 放在 finally 中，保证无论是成功、失败还是取消，都能执行收尾 Hook。
            await self._turn_executor.run_hook(
                HookType.SESSION_AFTER,
                session=session,
                workspace=workspace,
                agent_state=ctx.agent_state,
                llm_state=ctx.llm_state,
                runtime=runtime,
                config=config,
            )
        return await self._finish(session, runtime)

    async def run_subagent(
        self,
        *,
        parent_session: SessionState,
        workspace: Any,
        agent_profile: AgentProfile,
        task: str,
        parent_call_id: str,
        runtime: RuntimeHandles,
        config: Any,
        stop_event: Any,
    ) -> SessionState:
        """执行一次独立 subagent loop，只沉淀消息和工具事件，不发布父会话生命周期。"""
        context_id = new_context_id()
        child_session = SessionState(
            session_id=parent_session.session_id,
            title=parent_session.title,
            workspace_id=parent_session.workspace_id,
            workspace_path=parent_session.workspace_path,
            agent_name=agent_profile.name,
            provider=parent_session.provider,
            model=parent_session.model,
            status=SessionStatus.RUNNING,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            metadata={
                **parent_session.metadata,
                "agent_context_id": context_id,
                "parent_call_id": parent_call_id,
                "agent_kind": agent_profile.kind,
            },
        )
        agent_state = self._build_agent_state(
            child_session,
            agent_profile,
            context_id=context_id,
            parent_call_id=parent_call_id,
        )
        child_session.messages.append(
            self._build_subagent_task_message(parent_session, agent_state, task)
        )
        ctx = _RunContext(
            session=child_session,
            workspace=workspace,
            agent_profile=agent_profile,
            runtime=runtime,
            config=config,
            approval_event=None,
            approval_result_holder={"result": None},
            question_event=None,
            question_result_holder={"result": None},
            stop_event=stop_event,
            agent_state=agent_state,
            llm_state=self._build_llm_state(parent_session, config),
            allow_manual_approval=parent_session.metadata.get("allow_manual_approval") is not False,
            allow_question_interaction=parent_session.metadata.get("allow_question_interaction") is not False,
        )
        task_message = child_session.messages.pop()
        await self._message_appender.append(child_session, task_message, runtime)
        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="user_message_created",
                session_id=child_session.session_id,
                created_at=utc_now_iso(),
                data={**self._agent_event_data(agent_state), "message": task_message.model_dump()},
            )
        )
        await self._run_iterations(ctx)
        return child_session

    def _build_agent_state(
        self,
        session: SessionState,
        agent_profile: AgentProfile,
        *,
        context_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> AgentState:
        resolved_context_id = context_id or str(session.metadata.get("agent_context_id") or "main")
        return AgentState(
            name=agent_profile.name,
            role=agent_profile.kind,
            kind=agent_profile.kind,
            allowed_tools=agent_profile.allowed_tools,
            readonly=agent_profile.readonly,
            context_id=resolved_context_id,
            parent_call_id=parent_call_id or session.metadata.get("parent_call_id"),
            depth=int(session.metadata.get("agent_depth", 0)),
            parent_agent_id=session.metadata.get("parent_agent_id"),
            can_call_subagent=agent_profile.can_call_subagent,
        )

    def _build_subagent_task_message(self, session: SessionState, agent_state: AgentState, task: str) -> Message:
        return Message(
            info=build_user_message_info(
                message_id=new_message_id(),
                session_id=session.session_id,
                created_at_ms=utc_now_millis(),
                agent=agent_state.name,
                agent_kind=agent_state.kind,
                context_id=agent_state.context_id,
                parent_call_id=agent_state.parent_call_id,
                provider_id=session.provider,
                model_id=session.model,
            ),
            parts=[TextPart(text=task, synthetic=True, metadata={"source": "task_tool"})],
        )

    def _build_llm_state(self, session: SessionState, config: Any) -> LLMState:
        activated_provider = config.llm_runtime.activated_providers[session.provider]
        model_settings = activated_provider.model_settings.get(session.model)
        return LLMState(
            provider=session.provider,
            model=session.model,
            max_tokens=config.llm.max_tokens,
            metadata={
                "litellm_model_prefix": activated_provider.litellm_model_prefix,
                "thinking": model_settings.thinking.model_dump() if model_settings and model_settings.thinking else None,
                "thinking_enabled": bool(session.metadata.get("thinking_enabled")),
                "thinking_value": session.metadata.get("thinking_value"),
            },
        )

    async def _run_session_before(self, ctx: _RunContext) -> bool:
        """执行会话前置 Hook，并返回是否应该进入主循环。"""
        # SESSION_BEFORE 是整场会话的入口钩子，适合做全局预处理或提前人工确认。
        session_before = await self._turn_executor.run_hook(
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
            result = await self._resolve_approval(
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
        assistant_message = self._turn_executor.build_finished_assistant_message(
            session=ctx.session,
            agent_state=ctx.agent_state,
            text=f"已超过最大轮推理次数限制（{max_iterations} 轮），停止推理。",
            reason="max_iterations",
        )
        await self._message_appender.append(ctx.session, assistant_message, ctx.runtime)
        await self._turn_executor.publish_assistant_message_completed(ctx.session, assistant_message, ctx.runtime)

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
            stop_event=ctx.stop_event,
        )

    async def _handle_turn_result(self, ctx: _RunContext, turn_result: TurnResult, iteration: int) -> bool:
        """根据单轮结果决定继续、完成或退出主循环。"""
        if turn_result.status in {"completed", "stopped"}:
            ctx.session.status = SessionStatus.COMPLETED
            return False
        if turn_result.status == "failed":
            ctx.session.status = SessionStatus.FAILED
            return False
        if turn_result.status == "needs_question" and turn_result.pending_question is not None:
            if not ctx.allow_question_interaction:
                await self._fail_question_interaction_unavailable(
                    ctx,
                    interaction_id=turn_result.pending_question.request.question_id,
                )
                return False
            if ctx.agent_state.kind == "subagent":
                await self._fail_subagent_question(ctx, turn_result.pending_question)
                return False
            return await self._handle_pending_question(ctx, turn_result.pending_question, iteration, turn_result.resume_batch)
        if turn_result.status != "needs_approval" or turn_result.pending_approval is None:
            return True

        return await self._handle_pending_approval(
            ctx,
            turn_result.pending_approval,
            iteration,
            resume_batch=turn_result.resume_batch,
            stop_after_approval=turn_result.stop_after_approval,
        )

    async def _handle_pending_approval(
        self,
        ctx: _RunContext,
        approval: PendingApproval,
        iteration: int,
        *,
        resume_batch: ToolResumeBatch | None = None,
        stop_after_approval: bool = False,
    ) -> bool:
        """处理单轮执行产生的人工审批，并按原逻辑恢复可能挂起的工具调用。"""
        if ctx.agent_state.kind == "subagent" and ctx.allow_manual_approval:
            await self._fail_subagent_human_approval(ctx, approval)
            return False
        result = await self._resolve_approval(ctx, approval)
        if self._is_rejected(result):
            return False
        if stop_after_approval:
            return False
        if approval.resume_item is None:
            return True

        batch = await self._resume_approved_tool_call(ctx, approval, resume_batch)
        if batch.pending_approval:
            return await self._handle_pending_approval(ctx, batch.pending_approval, iteration, resume_batch=batch.resume_batch)
        if batch.pending_question:
            return await self._handle_pending_question(ctx, batch.pending_question, iteration, batch.resume_batch)
        return await self._run_loop_after_approved_tool(ctx, iteration)

    async def _fail_subagent_human_approval(self, ctx: _RunContext, approval: PendingApproval) -> None:
        """subagent 没有独立审批入口，遇到审批请求必须失败退出，避免前端等待不可恢复。"""
        message = "subagent 不支持人工审批，请由父 Agent 拆分为无需人工确认的探查任务。"
        ctx.session.status = SessionStatus.FAILED
        ctx.session.metadata["subagent_error"] = message
        ctx.session.updated_at = utc_now_iso()
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="error",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={
                    **self._agent_event_data(ctx.agent_state),
                    "message": message,
                    "approval_id": approval.request.approval_id,
                },
            )
        )

    async def _fail_question_interaction_unavailable(self, ctx: _RunContext, *, interaction_id: str) -> None:
        """没有用户回答入口的运行环境不得等待 question，避免后台任务卡死。"""
        message = (
            "定时任务为无人值守运行，不能请求人工输入。"
            if ctx.session.metadata.get("source") == "schedule"
            else "当前会话不支持用户回答。"
        )
        ctx.session.status = SessionStatus.FAILED
        ctx.session.metadata["non_interactive_error"] = message
        ctx.session.updated_at = utc_now_iso()
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="error",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={
                    **self._agent_event_data(ctx.agent_state),
                    "message": message,
                    "kind": "question",
                    "interaction_id": interaction_id,
                },
            )
        )

    async def _fail_subagent_question(self, ctx: _RunContext, question: PendingQuestion) -> None:
        """subagent 没有独立回答面板，禁止等待用户问题。"""
        message = "subagent 不支持向用户提问，请由父 Agent 收集所需信息。"
        ctx.session.status = SessionStatus.FAILED
        ctx.session.metadata["subagent_error"] = message
        ctx.session.updated_at = utc_now_iso()
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="error",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={
                    **self._agent_event_data(ctx.agent_state),
                    "message": message,
                    "kind": "question",
                    "interaction_id": question.request.question_id,
                },
            )
        )

    async def _handle_pending_question(
        self,
        ctx: _RunContext,
        question: PendingQuestion,
        iteration: int,
        resume_batch: ToolResumeBatch | None = None,
    ) -> bool:
        """处理 question 工具等待；回答后回填工具结果，拒答后结束当前 run。"""
        result = await self._wait_for_question(ctx, question)
        if result is None:
            return False
        await self._publish_question_resolved(ctx, question, result)
        if result.declined:
            return False
        await self._publish_question_resume_status(ctx)
        if question.resume_item is None:
            return True
        merge_question_result(ctx.session, question, result)
        await self._turn_executor.publish_assistant_message_completed(ctx.session, ctx.session.messages[-1], ctx.runtime)
        if resume_batch is not None and resume_batch.items:
            batch = await self._resume_tool_batch(ctx, resume_batch)
            if batch.pending_approval:
                return await self._handle_pending_approval(ctx, batch.pending_approval, iteration, resume_batch=batch.resume_batch)
            if batch.pending_question:
                if not ctx.allow_question_interaction:
                    await self._fail_question_interaction_unavailable(
                        ctx,
                        interaction_id=batch.pending_question.request.question_id,
                    )
                    return False
                if ctx.agent_state.kind == "subagent":
                    await self._fail_subagent_question(ctx, batch.pending_question)
                    return False
                return await self._handle_pending_question(ctx, batch.pending_question, iteration, batch.resume_batch)
        return await self._run_loop_after_approved_tool(ctx, iteration)

    async def _publish_question_resolved(
        self,
        ctx: _RunContext,
        question: PendingQuestion,
        result: QuestionResult,
    ) -> None:
        """以追加日志记录 question 答复，避免修改已落盘的 pending 消息。"""
        resume_item = question.resume_item or {}
        status = "declined" if result.declined else "resolved"
        await ctx.runtime.event_bus.publish_domain_event(
            HumanInteractionEvent(
                session_id=ctx.session.session_id,
                interaction_id=result.question_id,
                created_at=result.created_at,
                data={
                    "kind": "question",
                    "status": status,
                    "interaction_id": result.question_id,
                    "message_id": find_tool_message_id(ctx.session, resume_item.get("tool_call_id")),
                    "call_id": resume_item.get("tool_call_id"),
                    "request": question.request.model_dump(),
                    "result": result.model_dump(),
                    "tool_output": build_question_tool_output(question, result),
                },
            )
        )

    async def _publish_question_resume_status(self, ctx: _RunContext) -> None:
        """用户回答后补一条 RUNNING 状态，表达会话从等待状态恢复执行。"""
        await ctx.runtime.event_bus.publish_domain_event(
            SessionLifecycleEvent(
                session_id=ctx.session.session_id,
                status=SessionStatus.RUNNING.value,
                created_at=utc_now_iso(),
                data={
                    **ctx.session.model_dump(exclude={"messages"}),
                    "lifecycle_record_type": "session_status_changed",
                },
            )
        )

    async def _resolve_approval(self, ctx: _RunContext, approval: PendingApproval) -> ApprovalResult | None:
        """按运行策略等待人工审批，或在自动模式下直接放行可审批操作。"""
        if not ctx.allow_manual_approval:
            return ApprovalResult(
                approval_id=approval.request.approval_id,
                approved=True,
                comment="人工审批已关闭，自动通过。",
                created_at=utc_now_iso(),
            )
        return await self._approval_coordinator.wait(
            session=ctx.session,
            approval=approval,
            agent_state=ctx.agent_state,
            runtime=ctx.runtime,
            approval_event=ctx.approval_event,
            approval_result_holder=ctx.approval_result_holder,
        )

    async def _wait_for_question(self, ctx: _RunContext, question: PendingQuestion) -> QuestionResult | None:
        return await self._question_coordinator.wait(
            session=ctx.session,
            question=question,
            agent_state=ctx.agent_state,
            runtime=ctx.runtime,
            question_event=ctx.question_event,
            question_result_holder=ctx.question_result_holder,
        )

    def _is_rejected(self, result: ApprovalResult | None) -> bool:
        return result is not None and not result.approved

    async def _resume_approved_tool_call(
        self,
        ctx: _RunContext,
        approval: PendingApproval,
        resume_batch: ToolResumeBatch | None,
    ) -> ToolExecutionBatch:
        # 审批通过后从暂停点继续执行同一批工具，直到全部完成或再次暂停。
        if resume_batch is not None:
            return await self._resume_tool_batch(ctx, resume_batch)

        approved_tool_part = await self._turn_executor.tool_dispatcher.execute_approved_tool_call(
            session=ctx.session,
            workspace=ctx.workspace,
            agent=ctx.agent_state,
            item=approval.resume_item,
            runtime=ctx.runtime,
            config=ctx.config,
            stop_event=ctx.stop_event,
        )
        merge_approved_tool_result(ctx.session, approved_tool_part)
        await self._message_appender._persist_snapshot(ctx.session, ctx.session.messages[-1], ctx.runtime)
        await self._turn_executor.publish_assistant_message_completed(ctx.session, ctx.session.messages[-1], ctx.runtime)
        return ToolExecutionBatch(tool_parts=[approved_tool_part])

    async def _resume_tool_batch(self, ctx: _RunContext, resume_batch: ToolResumeBatch) -> ToolExecutionBatch:
        batch = await self._turn_executor.tool_dispatcher.resume_tool_batch(
            session=ctx.session,
            workspace=ctx.workspace,
            agent=ctx.agent_state,
            resume_batch=resume_batch,
            runtime=ctx.runtime,
            config=ctx.config,
            stop_event=ctx.stop_event,
        )
        if batch.tool_parts:
            merge_approved_tool_results(ctx.session, batch.tool_parts)
            await self._message_appender._persist_snapshot(ctx.session, ctx.session.messages[-1], ctx.runtime)
            await self._turn_executor.publish_assistant_message_completed(ctx.session, ctx.session.messages[-1], ctx.runtime)
        return batch

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
            result = await self._resolve_approval(
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
            StreamEvent(
                event_type="loop_started",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data=self._agent_event_data(ctx.agent_state),
            )
        )

    async def _publish_iteration_started(self, ctx: _RunContext, iteration: int) -> None:
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="loop_iteration_started",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={**self._agent_event_data(ctx.agent_state), "iteration": iteration},
            )
        )

    async def _publish_iteration_finished(self, ctx: _RunContext, iteration: int) -> None:
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="loop_iteration_finished",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={**self._agent_event_data(ctx.agent_state), "iteration": iteration},
            )
        )

    async def _publish_loop_finished(self, ctx: _RunContext) -> None:
        ctx.session.updated_at = utc_now_iso()
        await ctx.runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="loop_finished",
                session_id=ctx.session.session_id,
                created_at=utc_now_iso(),
                data={**self._agent_event_data(ctx.agent_state), "status": ctx.session.status.value},
            )
        )

    def _agent_event_data(self, agent_state: AgentState) -> dict[str, Any]:
        return {
            "agent": agent_state.name,
            "agent_kind": agent_state.kind,
            "context_id": agent_state.context_id,
            "parent_call_id": agent_state.parent_call_id,
        }

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
