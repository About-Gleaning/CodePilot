from __future__ import annotations

"""会话消息中与工具结果回填相关的纯辅助逻辑。"""

from typing import Any

from codepilot.session.message import AssistantMessageError, AssistantMessageInfo, Message, StepFinishPart, ToolPart, ToolPartState
from codepilot.session.state import ApprovalResult, PendingApproval, PendingQuestion, QuestionResult, SessionState
from codepilot.utils import utc_now_millis


def merge_approved_tool_result(session: SessionState, approved_tool_part: ToolPart) -> None:
    """把审批后执行得到的工具结果合并回最后一条 assistant 消息。"""
    merge_approved_tool_results(session, [approved_tool_part])


def merge_approved_tool_results(session: SessionState, approved_tool_parts: list[ToolPart]) -> None:
    """把审批恢复后得到的一批工具结果合并回最后一条 assistant 消息。"""
    latest_message = _latest_assistant_message(session)
    if latest_message is None or not approved_tool_parts:
        return

    part_map = {part.call_id: part for part in approved_tool_parts}
    merged_parts: list[object] = []
    replaced_ids: set[str] = set()
    for part in latest_message.parts:
        # 通过 call_id 精确匹配待替换的工具片段，避免误改同一条消息中的其他工具结果。
        if isinstance(part, ToolPart) and part.call_id in part_map:
            merged_parts.append(part_map[part.call_id])
            replaced_ids.add(part.call_id)
            continue
        merged_parts.append(part)
    for tool_part in approved_tool_parts:
        if tool_part.call_id not in replaced_ids:
            merged_parts.append(tool_part)
    latest_message.parts = merged_parts
    _update_approved_tool_completion(latest_message)


def merge_question_result(session: SessionState, question: PendingQuestion, result: QuestionResult) -> None:
    """把用户答案作为原 question 工具调用结果回填，供下一轮 LLM 以 tool 消息读取。"""
    if question.resume_item is None:
        return
    latest_message = _latest_assistant_message(session)
    if latest_message is None:
        return

    call_id = question.resume_item.get("tool_call_id")
    output = build_question_tool_output(question, result)
    for index, part in enumerate(latest_message.parts):
        if isinstance(part, ToolPart) and part.call_id == call_id:
            latest_message.parts[index] = ToolPart(
                call_id=part.call_id,
                tool=part.tool,
                state=ToolPartState(
                    status="completed",
                    input={**part.state.input, "question_id": result.question_id},
                    output=output,
                    time=part.state.time,
                ),
            )
            _mark_assistant_tool_completed(latest_message)
            return


def merge_declined_question_result(session: SessionState, question: PendingQuestion, result: QuestionResult) -> dict[str, Any] | None:
    """把用户拒答作为 question 工具错误回填，避免下一轮上下文留下 pending 工具调用。"""
    if question.resume_item is None:
        return None
    latest_message = _latest_assistant_message(session)
    if latest_message is None:
        return None

    call_id = question.resume_item.get("tool_call_id")
    output = build_question_tool_output(question, result)
    for index, part in enumerate(latest_message.parts):
        if isinstance(part, ToolPart) and part.call_id == call_id:
            latest_message.parts[index] = ToolPart(
                call_id=part.call_id,
                tool=part.tool,
                state=ToolPartState(
                    status="error",
                    input=part.state.input,
                    raw=part.state.raw,
                    output=output,
                    error=AssistantMessageError(
                        code="QuestionDeclined",
                        message=str(output["error_message"]),
                    ),
                    time=part.state.time,
                    attachments=part.state.attachments,
                ),
            )
            _mark_assistant_tool_completed(latest_message)
            return output
    return None


def merge_rejected_tool_result(session: SessionState, approval: PendingApproval, result: ApprovalResult) -> dict[str, Any] | None:
    """把人工拒绝作为工具错误回填，确保后续 LLM 上下文满足工具调用闭环协议。"""
    if approval.resume_item is None:
        return None
    latest_message = _latest_assistant_message(session)
    if latest_message is None:
        return None

    call_id = approval.resume_item.get("tool_call_id")
    output = build_rejected_tool_output(approval, result)
    for index, part in enumerate(latest_message.parts):
        if isinstance(part, ToolPart) and part.call_id == call_id:
            latest_message.parts[index] = ToolPart(
                call_id=part.call_id,
                tool=part.tool,
                state=ToolPartState(
                    status="error",
                    input=part.state.input,
                    raw=part.state.raw,
                    output=output,
                    error=AssistantMessageError(
                        code="ToolApprovalRejected",
                        message=str(output["error_message"]),
                    ),
                    time=part.state.time,
                    attachments=part.state.attachments,
                ),
            )
            _mark_assistant_tool_completed(latest_message)
            return output
    return None


def build_rejected_tool_output(approval: PendingApproval, result: ApprovalResult) -> dict[str, Any]:
    """生成可发给模型的审批拒绝工具结果，保留用户备注用于下一轮决策。"""
    resume_item = approval.resume_item or {}
    tool_name = str(resume_item.get("tool_name") or approval.request.action.get("tool_name") or "unknown")
    comment = (result.comment or "未提供备注").strip()
    return {
        "status": "error",
        "tool_name": tool_name,
        "error_type": "ToolApprovalRejected",
        "error_message": f"用户拒绝执行该工具调用：{comment}",
        "recoverable": True,
        "approval_id": result.approval_id,
        "approved": False,
        "comment": result.comment,
    }


def build_question_tool_output(question: PendingQuestion, result: QuestionResult) -> dict[str, Any]:
    """统一生成 question 工具结果，供内存合并和 JSONL 回放共用。"""
    if result.declined:
        comment = (result.comment or "未提供备注").strip()
        return {
            "status": "error",
            "tool_name": "question",
            "error_type": "QuestionDeclined",
            "error_message": f"用户拒绝回答 question 工具提出的问题：{comment}",
            "recoverable": True,
            "question_id": result.question_id,
            "declined": True,
            "comment": result.comment,
        }
    return {
        "status": "ok",
        "tool_name": "question",
        "question_id": result.question_id,
        "answers": result.answers,
        "output": summarize_question_answers(question.request.questions, result.answers),
    }


def summarize_question_answers(questions: list[dict[str, Any]], answers: dict[str, Any]) -> str:
    """把结构化答案转成自然语言，避免 LLM 直接读取前端内部 JSON。"""
    if not answers:
        return "用户未提供具体答案。"

    lines = ["用户已回答 question 工具提出的问题："]
    for index, question in enumerate(questions, start=1):
        question_id = str(question.get("id") or "")
        question_text = str(question.get("question") or "").strip() or question_id or f"问题 {index}"
        answer = answers.get(question_id)
        answer_record = answer if isinstance(answer, dict) else {}
        values = answer_record.get("values")
        selected_values = [str(value) for value in values] if isinstance(values, list) else []
        option_labels = _resolve_question_option_labels(question, selected_values)
        note = str(answer_record.get("note") or "").strip()

        lines.append("")
        lines.append(f"{index}. {question_text}")
        if option_labels:
            response = f"回答：{'、'.join(option_labels)}。"
        else:
            response = "回答：未选择。"
        if note:
            response += f"备注：{_ensure_sentence_end(note)}"
        lines.append(response)
    return "\n".join(lines)


def _latest_assistant_message(session: SessionState) -> Message | None:
    if not session.messages:
        return None
    latest_message = session.messages[-1]
    return latest_message if latest_message.info.role == "assistant" else None


def _mark_assistant_tool_completed(message: Message) -> None:
    assert isinstance(message.info, AssistantMessageInfo)
    message.info.time.completed = utc_now_millis()
    message.info.finish = "tool_completed"


def _update_approved_tool_completion(message: Message) -> None:
    """仅在同一消息的所有工具片段终态后关闭整批工具调用。"""
    tool_parts = [part for part in message.parts if isinstance(part, ToolPart)]
    if not tool_parts or any(part.state.status not in {"completed", "error"} for part in tool_parts):
        message.info.finish = "tool_pending"
        return

    _mark_assistant_tool_completed(message)
    for part in message.parts:
        if isinstance(part, StepFinishPart) and part.reason == "tool_pending":
            part.reason = "tool_completed"


def _resolve_question_option_labels(question: dict[str, Any], selected_values: list[str]) -> list[str]:
    option_map: dict[str, str] = {}
    raw_options = question.get("options")
    if isinstance(raw_options, list):
        for raw_option in raw_options:
            if not isinstance(raw_option, dict):
                continue
            value = str(raw_option.get("value") or "")
            label = str(raw_option.get("label") or "").strip()
            if value and label:
                option_map[value] = label
    return [option_map.get(value, value) for value in selected_values]


def _ensure_sentence_end(text: str) -> str:
    return text if text.endswith(("。", "！", "？", ".", "!", "?")) else f"{text}。"
