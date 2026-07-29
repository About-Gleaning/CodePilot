"""会话运行流编排。

本文件负责把一次 assistant 回合拆成可控制、可观测的执行节点：
Hook 前后置处理、LLM 流式调用、工具调用合并、人工审批等待、
消息持久化到 SessionState，以及对应领域事件/流事件发布。
这里不承载具体业务工具逻辑，只维护会话控制流和消息状态的一致性。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from codepilot.context import ContextCompressionError, ContextCompressor
from codepilot.events import SessionCompactedEvent, StreamEvent
from codepilot.hooks import HookContext, HookManager, HookResult, HookType, RuntimeHandles
from codepilot.llm import LiteLLMClient
from codepilot.skills import SkillRegistry
from codepilot.session.agents import AgentProfile
from codepilot.session.interactions import SessionMessageAppender
from codepilot.session.message import (
    AssistantMessageError,
    AssistantMessageInfo,
    Message,
    ReasoningPart,
    StepFinishPart,
    StepStartPart,
    TextPart,
    ToolPart,
    AssistantMessageTokens,
    build_assistant_message_info,
)
from codepilot.session.state import (
    AgentState,
    LLMState,
    PendingApproval,
    PendingQuestion,
    SessionState,
    SessionStatus,
)
from codepilot.session.system_prompt import build_runtime_context_prompt, build_system_prompt
from codepilot.tools.dispatcher import ToolDispatcher, ToolResumeBatch
from codepilot.tools.registry import ToolRegistry
from codepilot.utils import new_message_id, utc_now_iso, utc_now_millis


@dataclass(slots=True)
class TurnResult:
    """单轮执行结果，用于驱动外层会话循环继续、结束或等待人工审批。"""

    status: Literal["continue", "completed", "stopped", "needs_approval", "needs_question", "failed"]
    pending_approval: PendingApproval | None = None
    pending_question: PendingQuestion | None = None
    resume_batch: ToolResumeBatch | None = None
    stop_after_approval: bool = False


@dataclass(slots=True)
class TurnExecutor:
    """执行 assistant 的一个思考/行动回合，是 Runtime 主循环的核心编排器。"""

    llm_client: LiteLLMClient
    tool_registry: ToolRegistry
    tool_dispatcher: ToolDispatcher
    hook_manager: HookManager
    message_appender: SessionMessageAppender
    skill_registry: SkillRegistry | None = None
    context_compressor: ContextCompressor = field(default_factory=ContextCompressor)

    async def execute(
        self,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        agent_profile: AgentProfile,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
        iteration: int,
        stop_event: Any | None = None,
    ) -> TurnResult:
        """按固定顺序执行 Hook、LLM、工具与后置 Hook，并返回下一步控制信号。"""

        loop_before_result = await self._run_loop_before_stage(
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
        )
        if loop_before_result:
            return loop_before_result

        compression_result = await self._compress_context_stage(
            session=session,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
            iteration=iteration,
        )
        if compression_result:
            return compression_result

        llm_before_result = await self._run_llm_before_stage(
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
        )
        if llm_before_result:
            return llm_before_result

        stream_payload = await self._stream_assistant_message(
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            agent_profile=agent_profile,
            llm_state=llm_state,
            runtime=runtime,
            iteration=iteration,
        )
        if isinstance(stream_payload, TurnResult):
            return stream_payload
        stream_result, assistant_message = stream_payload

        llm_after_result = await self._run_llm_after_stage(
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
            assistant_message=assistant_message,
        )
        if llm_after_result:
            return llm_after_result

        return await self._handle_tool_stage(
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
            iteration=iteration,
            tool_calls=stream_result.tool_calls,
            assistant_message=assistant_message,
            stop_event=stop_event,
        )

    async def _run_loop_before_stage(
        self,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
    ) -> TurnResult | None:
        # LOOP_BEFORE 是进入本轮前的最后检查点，常用于注入上下文或拦截高风险操作。
        loop_before = await self._run_hook(
            HookType.LOOP_BEFORE,
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
        )
        return self._turn_result_from_hook(session, loop_before)

    async def _compress_context_stage(
        self,
        session: SessionState,
        agent_state: AgentState,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
        iteration: int,
    ) -> TurnResult | None:
        try:
            # 压缩必须在 LLM_BEFORE 前完成，确保后续 Hook 看到的就是实际将发送给模型的上下文。
            compression_result = await self.context_compressor.compress(
                session=session,
                config=config,
                llm_state=llm_state,
                llm_client=self.llm_client,
                context_id=agent_state.context_id,
            )
        except ContextCompressionError as exc:
            await runtime.event_bus.publish_stream_event(
                StreamEvent(
                    event_type="assistant_message_started",
                    session_id=session.session_id,
                    created_at=utc_now_iso(),
                    data={**self._agent_event_data(agent_state), "iteration": iteration, "stage": "context_compression"},
                )
            )
            assistant_message = self._build_context_compression_error_message(session, agent_state, exc)
            await self.message_appender.append(session, assistant_message, runtime)
            await self.publish_assistant_message_completed(session, assistant_message, runtime)
            return TurnResult(status="failed")

        if compression_result.changed:
            await runtime.event_bus.publish_domain_event(
                SessionCompactedEvent(
                    session_id=session.session_id,
                    created_at=utc_now_iso(),
                    data={
                        **compression_result.to_event_data(),
                        "metadata": session.metadata,
                        "messages": [
                            message.model_dump()
                            for message in session.messages
                            if (message.info.context_id or "main") == compression_result.context_id
                        ],
                    },
                )
            )
            await runtime.event_bus.publish_stream_event(
                StreamEvent(
                    event_type="context_compacted",
                    session_id=session.session_id,
                    created_at=utc_now_iso(),
                    data={**self._agent_event_data(agent_state), **compression_result.to_event_data()},
                )
            )
        return None

    async def _run_llm_before_stage(
        self,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
    ) -> TurnResult | None:
        # LLM_BEFORE 发生在请求模型前，适合修改消息、校验配置或发起人工确认。
        llm_before = await self._run_hook(
            HookType.LLM_BEFORE,
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
        )
        return self._turn_result_from_hook(session, llm_before)

    async def _stream_assistant_message(
        self,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        agent_profile: AgentProfile,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        iteration: int,
    ) -> tuple[Any, Message] | TurnResult:
        # Provider 消息只在发送前从统一 Message 模型转换，避免运行时内部依赖厂商格式。
        system_prompt = build_system_prompt(
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            agent_profile=agent_profile,
            llm_state=llm_state,
            skill_registry=self.skill_registry,
        )
        context_messages = self._messages_for_context(session, agent_state.context_id)
        provider_messages = self.llm_client.build_provider_messages(
            context_messages,
            system_prompt=system_prompt,
            runtime_context=build_runtime_context_prompt(
                session=session,
                workspace=workspace,
                agent_state=agent_state,
                llm_state=llm_state,
                now=_latest_user_message_datetime(context_messages),
            ),
        )
        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="assistant_message_started",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={**self._agent_event_data(agent_state), "iteration": iteration},
            )
        )
        try:
            tool_schemas = self.tool_registry.get_llm_tool_schemas(agent_profile.allowed_tools, agent_profile=agent_profile)
            stream_result = await self.llm_client.stream_chat(
                session=session,
                llm_state=llm_state,
                provider_messages=provider_messages,
                tools=tool_schemas,
                event_bus=_AgentEventBus(runtime.event_bus, self._agent_event_data(agent_state)),
            )
        except Exception as exc:  # noqa: BLE001
            error = self._convert_llm_exception(exc)
            if not self._is_fatal_llm_error(error):
                assistant_message = self._build_recoverable_llm_error_message(
                    session=session,
                    agent_state=agent_state,
                    error=error,
                    provider_messages=provider_messages,
                    tools=tool_schemas,
                    llm_state=llm_state,
                )
                await self.message_appender.append(session, assistant_message, runtime)
                await self.publish_assistant_message_completed(session, assistant_message, runtime)
                return TurnResult(status="continue")
            assistant_message = self._build_llm_error_message(session, agent_state, error)
            await self.message_appender.append(session, assistant_message, runtime)
            await self.publish_assistant_message_completed(session, assistant_message, runtime)
            return TurnResult(status="failed")
        assistant_message = self._build_assistant_message(
            session=session,
            agent_state=agent_state,
            text=stream_result.text,
            reasoning=stream_result.reasoning,
            tool_calls=stream_result.tool_calls,
            tokens=getattr(stream_result, "tokens", None),
        )
        return stream_result, assistant_message

    async def _run_llm_after_stage(
        self,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
        assistant_message: Message,
    ) -> TurnResult | None:
        # LLM_AFTER 能看到本次 assistant 消息草稿，可用于审查输出或追加审计信息。
        llm_after = await self._run_hook(
            HookType.LLM_AFTER,
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
            current_message=assistant_message,
        )
        llm_after_result = self._turn_result_from_hook(session, llm_after, skip_stop=True)
        if llm_after_result:
            return llm_after_result
        if llm_after.stop_loop:
            # LLM_AFTER 拦截时只沉淀模型已生成的草稿；即使包含工具调用，也不能继续执行工具。
            self._append_step_finish(assistant_message, reason="stopped")
            await self.message_appender.append(session, assistant_message, runtime)
            await self.publish_assistant_message_completed(session, assistant_message, runtime)
            return TurnResult(status="stopped")
        return None

    async def _handle_tool_stage(
        self,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
        iteration: int,
        tool_calls: list[dict[str, Any]],
        assistant_message: Message,
        stop_event: Any | None = None,
    ) -> TurnResult:
        # 没有工具调用时，本轮到此闭环：补完成标记、落消息、通知前端完成。
        if not tool_calls:
            self._append_step_finish(assistant_message, reason="completed")
            await self.message_appender.append(session, assistant_message, runtime)
            await self.publish_assistant_message_completed(session, assistant_message, runtime)
            return TurnResult(status="completed")

        tool_batch = await self.tool_dispatcher.execute_tool_calls(
            session=session,
            workspace=workspace,
            agent=agent_state,
            tool_calls=tool_calls,
            runtime=runtime,
            config=config,
            stop_event=stop_event,
        )
        # 工具执行结果会替换原先 pending 的 ToolPart，保证一条 assistant 消息包含完整行动轨迹。
        # 先合并再落库，避免历史记录中出现只有 pending、缺少最终结果的 assistant 行动消息。
        self._merge_tool_parts(assistant_message, tool_batch.tool_parts)
        self._append_step_finish(
            assistant_message,
            reason="tool_pending" if tool_batch.pending_approval or tool_batch.pending_question else "tool_completed",
        )
        await self.message_appender.append(session, assistant_message, runtime)
        await self.publish_assistant_message_completed(session, assistant_message, runtime)
        if any(
            part.state.output.get("error_type") == "McpOutcomeUncertain"
            for part in tool_batch.tool_parts
        ):
            # 已发出的外部变更结果不确定时必须 fail closed，禁止当前 Run 继续调用工具。
            session.metadata["external_effect_uncertain"] = True
            session.status = SessionStatus.FAILED
            return TurnResult(status="failed")
        if tool_batch.pending_approval:
            return TurnResult(
                status="needs_approval",
                pending_approval=tool_batch.pending_approval,
                resume_batch=tool_batch.resume_batch,
            )
        if tool_batch.pending_question:
            return TurnResult(
                status="needs_question",
                pending_question=tool_batch.pending_question,
                resume_batch=tool_batch.resume_batch,
            )

        # 工具完成后才运行 LOOP_AFTER，让 Hook 可以基于工具结果决定继续、失败或请求审批。
        # 如果工具还在等待审批，后续控制权必须交给审批流程，不能提前触发后置 Hook。
        loop_after = await self.run_loop_after(
            HookType.LOOP_AFTER,
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
            metadata={"iteration": iteration},
        )
        loop_after_result = self._turn_result_from_hook(session, loop_after)
        if loop_after_result:
            return loop_after_result
        return TurnResult(status="continue")

    def _turn_result_from_hook(
        self,
        session: SessionState,
        hook_result: HookResult,
        *,
        skip_stop: bool = False,
    ) -> TurnResult | None:
        """把 Hook 的控制信号转换成单轮结果，判断顺序必须与原主流程一致。"""

        if hook_result.requires_human_input and hook_result.human_request:
            return TurnResult(
                status="needs_approval",
                pending_approval=PendingApproval(request=hook_result.human_request, source="hook"),
                stop_after_approval=hook_result.stop_loop,
            )
        if session.status == SessionStatus.FAILED:
            return TurnResult(status="failed")
        if hook_result.stop_loop and not skip_stop:
            return TurnResult(status="stopped")
        return None

    async def run_loop_after(
        self,
        hook_type: HookType,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
        metadata: dict[str, Any] | None = None,
    ) -> HookResult:
        return await self.run_hook(
            hook_type,
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
            metadata=metadata,
        )

    async def run_hook(
        self,
        hook_type: HookType,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
        current_message: Message | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HookResult:
        """构造 Hook 上下文并统一应用 Hook 对会话产生的副作用。"""

        ctx = HookContext(
            hook_type=hook_type.value,
            session=session,
            workspace=workspace,
            agent=agent_state,
            messages=session.messages,
            current_message=current_message or (session.messages[-1] if session.messages else None),
            llm=llm_state,
            config=config,
            runtime=runtime,
            metadata=metadata or {},
        )
        result = await self.hook_manager.run(hook_type, ctx)
        await self.message_appender.apply_hook_result(session, result, runtime)
        return result

    async def _run_hook(
        self,
        hook_type: HookType,
        session: SessionState,
        workspace: Any,
        agent_state: AgentState,
        llm_state: LLMState,
        runtime: RuntimeHandles,
        config: Any,
        current_message: Message | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HookResult:
        return await self.run_hook(
            hook_type,
            session=session,
            workspace=workspace,
            agent_state=agent_state,
            llm_state=llm_state,
            runtime=runtime,
            config=config,
            current_message=current_message,
            metadata=metadata,
        )

    def _build_assistant_message(
        self,
        session: SessionState,
        agent_state: AgentState,
        text: str,
        reasoning: str,
        tool_calls: list[dict[str, Any]],
        tokens: AssistantMessageTokens | None = None,
    ) -> Message:
        """把模型流式结果收敛为统一的 assistant Message。"""

        message_id = new_message_id()
        parts: list[Any] = [StepStartPart(snapshot=f"iter_{utc_now_iso()}")]
        if text:
            parts.append(TextPart(text=text))
        if reasoning:
            parts.append(ReasoningPart(text=reasoning))
        for index, tool_call in enumerate(tool_calls):
            call_id = tool_call.get("tool_call_id") or f"call_{message_id}_{index}"
            tool_call["tool_call_id"] = call_id
            parts.append(
                ToolPart(
                    call_id=call_id,
                    tool=tool_call["tool_name"],
                    state={
                        "status": "pending",
                        "input": tool_call["arguments"],
                        "raw": json.dumps(tool_call["arguments"], ensure_ascii=False),
                        "time": {"created": utc_now_iso()},
                    },
                )
            )
        message = Message(
            info=build_assistant_message_info(
                message_id=message_id,
                session_id=session.session_id,
                created_at_ms=utc_now_millis(),
                parent_id=self._find_latest_user_message_id(session),
                agent=agent_state.name,
                agent_kind=agent_state.kind,
                context_id=agent_state.context_id,
                parent_call_id=agent_state.parent_call_id,
                provider_id=session.provider,
                model_id=session.model,
                cwd=session.workspace_path,
                root=session.workspace_path,
            ),
            parts=parts,
        )
        assert isinstance(message.info, AssistantMessageInfo)
        message.info.tokens = tokens
        return message

    def _build_llm_error_message(self, session: SessionState, agent_state: AgentState, error: AssistantMessageError | Exception) -> Message:
        """把模型调用异常转换成可展示、可追踪的 assistant 错误消息。"""

        if not isinstance(error, AssistantMessageError):
            error = self._convert_llm_exception(error)
        message = self._build_assistant_message(
            session=session,
            agent_state=agent_state,
            text=f"LLM 调用失败，AgentLoop 已停止：{error.message}",
            reasoning="",
            tool_calls=[],
            tokens=None,
        )
        assert isinstance(message.info, AssistantMessageInfo)
        message.info.error = error
        self._append_step_finish(message, reason="llm_error")
        return message

    def _build_recoverable_llm_error_message(
        self,
        *,
        session: SessionState,
        agent_state: AgentState,
        error: AssistantMessageError,
        provider_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        llm_state: LLMState,
    ) -> Message:
        """把非致命 LLM 错误转为下一轮可见的纯文本诊断消息。"""

        failed_request = {
            "provider": llm_state.provider,
            "model": llm_state.model,
            "messages": provider_messages,
            "tools": tools,
        }
        request_text = self._dump_failed_llm_request(failed_request)
        message = self._build_assistant_message(
            session=session,
            agent_state=agent_state,
            text=(
                "LLM 调用发生非致命错误，AgentLoop 将继续下一轮。\n"
                "这是一条运行时诊断消息，不是用户输入。下一轮需要根据错误信息向用户说明失败原因，"
                "不要声称已经看到了未成功发送给模型的内容。\n\n"
                f"错误 code：{error.code or 'llm_api_error'}\n"
                f"异常类型：{error.detail.get('exception_type') or 'UnknownError'}\n"
                f"HTTP 状态码：{error.detail.get('status_code')}\n"
                f"错误信息：{error.message}\n\n"
                f"失败报文（已脱敏）：\n```json\n{request_text}\n```"
            ),
            reasoning="",
            tool_calls=[],
            tokens=None,
        )
        assert isinstance(message.info, AssistantMessageInfo)
        message.info.error = error
        self._append_step_finish(message, reason="llm_error_recoverable")
        return message

    def _build_context_compression_error_message(self, session: SessionState, agent_state: AgentState, exc: Exception) -> Message:
        """把上下文压缩异常沉淀为 assistant 消息，避免继续发送超长上下文。"""

        error = AssistantMessageError(
            code="context_compression_error",
            message=self._sanitize_error_message(str(exc) or exc.__class__.__name__),
            detail={"exception_type": exc.__class__.__name__},
        )
        message = self._build_assistant_message(
            session=session,
            agent_state=agent_state,
            text=f"上下文压缩失败，AgentLoop 已停止：{error.message}",
            reasoning="",
            tool_calls=[],
            tokens=None,
        )
        assert isinstance(message.info, AssistantMessageInfo)
        message.info.error = error
        self._append_step_finish(message, reason="context_compression_error")
        return message

    def _convert_llm_exception(self, exc: Exception) -> AssistantMessageError:
        """统一转换 LLM API 异常，避免把敏感凭证透传到消息或事件中。"""

        raw_message = self._sanitize_error_message(str(exc) or exc.__class__.__name__)
        lower_message = raw_message.lower()
        status_code = self._extract_status_code(exc)
        code = "llm_api_error"
        if any(keyword in lower_message for keyword in ("quota", "balance", "billing", "insufficient_quota")) or any(
            keyword in raw_message for keyword in ("欠费", "余额不足", "额度不足")
        ):
            code = "llm_insufficient_quota"
        elif status_code in {401, 403}:
            code = "llm_auth_error"
        elif status_code == 429:
            code = "llm_rate_limited"
        elif self._is_service_unavailable_error(lower_message, status_code, exc):
            code = "llm_service_unavailable"
        elif status_code == 400:
            code = "llm_bad_request"

        return AssistantMessageError(
            code=code,
            message=raw_message,
            detail={
                "exception_type": exc.__class__.__name__,
                "status_code": status_code,
            },
        )

    def _is_fatal_llm_error(self, error: AssistantMessageError) -> bool:
        return error.code in {
            "llm_auth_error",
            "llm_insufficient_quota",
            "llm_rate_limited",
            "llm_service_unavailable",
        }

    def _is_service_unavailable_error(self, lower_message: str, status_code: int | None, exc: Exception) -> bool:
        if status_code is not None and status_code >= 500:
            return True
        exception_name = exc.__class__.__name__.lower()
        fatal_keywords = (
            "service unavailable",
            "temporarily unavailable",
            "upstream unavailable",
            "timeout",
            "timed out",
            "connection",
            "connect error",
        )
        return any(keyword in lower_message or keyword in exception_name for keyword in fatal_keywords)

    def _dump_failed_llm_request(self, request: dict[str, Any]) -> str:
        redacted = self._redact_failed_llm_request(request)
        text = json.dumps(redacted, ensure_ascii=False, indent=2, default=str)
        max_chars = 20_000
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n... 已截断失败报文，避免诊断消息过长。"

    def _redact_failed_llm_request(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if key.lower() in {"api_key", "token", "password", "authorization", "cookie", "secret"}:
                    redacted[key] = "***REDACTED***"
                    continue
                redacted[key] = self._redact_failed_llm_request(item)
            return redacted
        if isinstance(value, list):
            return [self._redact_failed_llm_request(item) for item in value]
        if isinstance(value, str) and value.startswith("data:image/"):
            return "[image data url redacted]"
        return value

    def _extract_status_code(self, exc: Exception) -> int | None:
        status_code = getattr(exc, "status_code", None)
        if status_code is None and getattr(exc, "response", None) is not None:
            status_code = getattr(exc.response, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        return None

    def _sanitize_error_message(self, message: str) -> str:
        # 上游异常可能包含认证头或 key，落消息前做最小脱敏，避免敏感信息进入历史记录。
        sanitized = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s,;]+", r"\1***", message)
        sanitized = re.sub(r"(?i)(api[_-]?key[\"'\s:=]+)[^\"'\s,;]+", r"\1***", sanitized)
        sanitized = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", sanitized)
        return sanitized

    def _merge_tool_parts(self, assistant_message: Message, tool_parts: list[ToolPart]) -> None:
        if not tool_parts:
            return
        # 优先按 call_id 原位替换，避免工具结果改变模型原始工具调用顺序。
        part_map = {part.call_id: part for part in tool_parts}
        merged_parts: list[Any] = []
        for part in assistant_message.parts:
            if isinstance(part, ToolPart) and part.call_id in part_map:
                merged_parts.append(part_map.pop(part.call_id))
                continue
            merged_parts.append(part)
        merged_parts.extend(part_map.values())
        assistant_message.parts = merged_parts

    def _append_step_finish(self, assistant_message: Message, reason: str) -> None:
        assert isinstance(assistant_message.info, AssistantMessageInfo)
        assistant_message.info.time.completed = utc_now_millis()
        assistant_message.info.finish = reason
        assistant_message.parts.append(StepFinishPart(reason=reason, tokens=assistant_message.info.tokens))

    def _find_latest_user_message_id(self, session: SessionState) -> str:
        # assistant 回复必须显式挂到最近一条用户消息下，避免控制消息把父子关系串乱。
        for message in reversed(session.messages):
            if message.info.role == "user":
                return message.info.id
        raise ValueError("assistant 消息缺少可关联的用户父消息")

    def _agent_event_data(self, agent_state: AgentState) -> dict[str, Any]:
        return {
            "agent": agent_state.name,
            "agent_kind": agent_state.kind,
            "context_id": agent_state.context_id,
            "parent_call_id": agent_state.parent_call_id,
        }

    def _messages_for_context(self, session: SessionState, context_id: str | None) -> list[Message]:
        target_context_id = context_id or "main"
        return [message for message in session.messages if (message.info.context_id or "main") == target_context_id]

    def build_finished_assistant_message(
        self,
        session: SessionState,
        agent_state: AgentState,
        *,
        text: str,
        reason: str,
    ) -> Message:
        message = self._build_assistant_message(
            session=session,
            agent_state=agent_state,
            text=text,
            reasoning="",
            tool_calls=[],
        )
        self._append_step_finish(message, reason=reason)
        return message

    async def publish_assistant_message_completed(
        self,
        session: SessionState,
        assistant_message: Message,
        runtime: RuntimeHandles,
    ) -> None:
        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="assistant_message_completed",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={**self._agent_event_data(agent_state_from_message(assistant_message)), "message": assistant_message.model_dump()},
            )
        )


def _latest_user_message_datetime(messages: list[Message]) -> datetime | None:
    """同一用户输入触发的多轮请求复用用户提交时刻，避免秒级 now 打断缓存前缀。"""
    for message in reversed(messages):
        if message.info.role == "user":
            return datetime.fromtimestamp(message.info.time.created / 1000).astimezone()
    return None


def agent_state_from_message(message: Message) -> AgentState:
    return AgentState(
        name=getattr(message.info, "agent", ""),
        kind=getattr(message.info, "agent_kind", "agent"),
        role=getattr(message.info, "agent_kind", "agent"),
        context_id=getattr(message.info, "context_id", None),
        parent_call_id=getattr(message.info, "parent_call_id", None),
    )


class _AgentEventBus:
    def __init__(self, event_bus: Any, agent_data: dict[str, Any]) -> None:
        self._event_bus = event_bus
        self._agent_data = agent_data

    async def publish_stream_event(self, event: StreamEvent) -> StreamEvent:
        event.data = {**self._agent_data, **(event.data or {})}
        return await self._event_bus.publish_stream_event(event)
