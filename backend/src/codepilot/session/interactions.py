from __future__ import annotations

"""会话人工交互等待与消息追加辅助。"""

from dataclasses import dataclass
from typing import Any

from codepilot.events import HumanInteractionEvent, MessageCreatedEvent, SessionLifecycleEvent, StreamEvent
from codepilot.hooks import HookResult, RuntimeHandles
from codepilot.session.message import Message, TextPart, ToolPart, build_user_message_info
from codepilot.session.message_ops import merge_declined_question_result, merge_rejected_tool_result
from codepilot.session.state import (
    AgentState,
    ApprovalRequest,
    ApprovalResult,
    PendingApproval,
    PendingQuestion,
    QuestionResult,
    SessionState,
    SessionStatus,
)
from codepilot.utils import new_message_id, utc_now_iso, utc_now_millis


def find_tool_message_id(session: SessionState, call_id: str | None) -> str | None:
    """按工具调用 ID 查找承载该工具片段的 assistant 消息。"""
    if not call_id:
        return None
    for message in reversed(session.messages):
        if message.info.role != "assistant":
            continue
        if any(isinstance(part, ToolPart) and part.call_id == call_id for part in message.parts):
            return message.info.id
    return None


@dataclass(slots=True)
class SessionMessageAppender:
    """统一维护消息落入 session 与对应领域事件发布。"""

    async def append(self, session: SessionState, message: Message, runtime: RuntimeHandles) -> None:
        session.messages.append(message)
        session.updated_at = utc_now_iso()
        await runtime.event_bus.publish_domain_event(
            MessageCreatedEvent(
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={"record_type": "message"},
                message=message,
            )
        )

    async def append_batch(self, session: SessionState, messages: list[Message], runtime: RuntimeHandles) -> None:
        for message in messages:
            await self.append(session, message, runtime)

    async def apply_hook_result(self, session: SessionState, result: HookResult, runtime: RuntimeHandles) -> None:
        await self.append_batch(session, result.messages_to_append, runtime)
        for event in result.events_to_emit:
            await runtime.event_bus.publish_stream_event(event)
        session.metadata.update(result.context_patch)
        session.updated_at = utc_now_iso()
        if result.fail_session:
            session.status = SessionStatus.FAILED


@dataclass(slots=True)
class ApprovalCoordinator:
    """集中处理人工审批生命周期，避免审批状态散落在主执行流中。"""

    message_appender: SessionMessageAppender

    async def wait(
        self,
        session: SessionState,
        approval: PendingApproval,
        agent_state: AgentState,
        runtime: RuntimeHandles,
        approval_event: Any,
        approval_result_holder: dict[str, ApprovalResult | None],
    ) -> ApprovalResult | None:
        """统一处理审批等待、结果广播与拒绝后的系统消息沉淀。"""
        approval_event.clear()
        approval_result_holder["result"] = None
        await _publish_waiting_human(
            session=session,
            runtime=runtime,
            kind="approval",
            interaction_id=approval.request.approval_id,
            request=approval.request.model_dump(),
            resume_item=approval.resume_item or {},
            stream_event_type="human_approval_required",
        )

        await approval_event.wait()
        result = approval_result_holder.get("result")
        if result is None:
            return None

        resume_item = approval.resume_item or {}
        tool_output = None
        if not result.approved:
            tool_output = merge_rejected_tool_result(session, approval, result)

        await runtime.event_bus.publish_domain_event(
            HumanInteractionEvent(
                session_id=session.session_id,
                interaction_id=result.approval_id,
                created_at=utc_now_iso(),
                data={
                    "kind": "approval",
                    "status": "approved" if result.approved else "rejected",
                    "interaction_id": result.approval_id,
                    "message_id": find_tool_message_id(session, resume_item.get("tool_call_id")),
                    "call_id": resume_item.get("tool_call_id"),
                    "result": result.model_dump(),
                    "tool_output": tool_output,
                },
            )
        )
        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="human_approval_resolved",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data=result.model_dump(),
            )
        )

        if result.approved:
            _mark_human_wait_finished(session, SessionStatus.RUNNING)
            return result

        await self.message_appender.append(
            session,
            self._build_human_refusal_message(session, agent_state, approval.request, result),
            runtime,
        )
        _mark_human_wait_finished(session, SessionStatus.CANCELLED)
        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="session_status_changed",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={"status": SessionStatus.CANCELLED.value},
            )
        )
        return result

    def _build_human_refusal_message(
        self,
        session: SessionState,
        agent_state: AgentState,
        approval: ApprovalRequest,
        result: ApprovalResult,
    ) -> Message:
        return Message(
            info=build_user_message_info(
                message_id=new_message_id(),
                session_id=session.session_id,
                created_at_ms=utc_now_millis(),
                agent=agent_state.name,
                provider_id=session.provider,
                model_id=session.model,
            ),
            parts=[
                TextPart(
                    text=f"人工拒绝继续执行：{result.comment or '未提供备注'}",
                    metadata={"approval_id": approval.approval_id, "approved": result.approved},
                ),
            ],
        )


@dataclass(slots=True)
class QuestionCoordinator:
    """集中处理 question 工具等待生命周期，与审批语义保持隔离。"""

    message_appender: SessionMessageAppender

    async def wait(
        self,
        session: SessionState,
        question: PendingQuestion,
        agent_state: AgentState,
        runtime: RuntimeHandles,
        question_event: Any,
        question_result_holder: dict[str, QuestionResult | None],
    ) -> QuestionResult | None:
        question_event.clear()
        question_result_holder["result"] = None
        await _publish_waiting_human(
            session=session,
            runtime=runtime,
            kind="question",
            interaction_id=question.request.question_id,
            request=question.request.model_dump(),
            resume_item=question.resume_item or {},
            stream_event_type="human_question_required",
        )

        await question_event.wait()
        result = question_result_holder.get("result")
        if result is None:
            return None

        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="human_question_resolved",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={**result.model_dump(), "continue_loop": not result.declined},
            )
        )
        if result.declined:
            merge_declined_question_result(session, question, result)
            await self.message_appender.append(
                session,
                self._build_question_decline_message(session, agent_state, question, result),
                runtime,
            )
            _mark_human_wait_finished(session, SessionStatus.COMPLETED)
            return result

        _mark_human_wait_finished(session, SessionStatus.RUNNING)
        return result

    def _build_question_decline_message(
        self,
        session: SessionState,
        agent_state: AgentState,
        question: PendingQuestion,
        result: QuestionResult,
    ) -> Message:
        question_summary = "；".join(str(item.get("question", "")) for item in question.request.questions if isinstance(item, dict))
        suffix = f"。问题：{question_summary}" if question_summary else ""
        return Message(
            info=build_user_message_info(
                message_id=new_message_id(),
                session_id=session.session_id,
                created_at_ms=utc_now_millis(),
                agent=agent_state.name,
                provider_id=session.provider,
                model_id=session.model,
            ),
            parts=[
                TextPart(
                    text=f"用户拒绝回答 question 工具提出的问题{suffix}",
                    metadata={"question_id": result.question_id, "declined": True},
                ),
            ],
        )


async def _publish_waiting_human(
    *,
    session: SessionState,
    runtime: RuntimeHandles,
    kind: str,
    interaction_id: str,
    request: dict[str, Any],
    resume_item: dict[str, Any],
    stream_event_type: str,
) -> None:
    """进入等待人工交互状态，并发布统一的领域事件与前端流事件。"""
    session.status = SessionStatus.WAITING_HUMAN
    session.metadata["pending_human_type"] = kind
    if kind == "question":
        # 回复必须绑定当前等待请求，拒绝陈旧页面或手工构造的错误交互 ID。
        session.metadata["pending_question_id"] = interaction_id
    session.updated_at = utc_now_iso()
    await runtime.event_bus.publish_domain_event(
        HumanInteractionEvent(
            session_id=session.session_id,
            interaction_id=interaction_id,
            created_at=utc_now_iso(),
            data={
                "kind": kind,
                "status": "pending",
                "interaction_id": interaction_id,
                "message_id": find_tool_message_id(session, resume_item.get("tool_call_id")),
                "call_id": resume_item.get("tool_call_id"),
                "request": request,
            },
        )
    )
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
            event_type=stream_event_type,
            session_id=session.session_id,
            created_at=utc_now_iso(),
            data=request,
        )
    )


def _mark_human_wait_finished(session: SessionState, status: SessionStatus) -> None:
    """清理人工等待标记，并切换到调用方指定的后续状态。"""
    session.metadata.pop("pending_human_type", None)
    session.metadata.pop("pending_question_id", None)
    session.status = status
    session.updated_at = utc_now_iso()
