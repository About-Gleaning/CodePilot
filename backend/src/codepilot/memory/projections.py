from __future__ import annotations

"""JSONL 会话记录的回放与摘要投影。"""

from typing import Any


SESSION_LIFECYCLE_RECORD_TYPES = {"session_started", "session_status_changed", "session_finished", "session_failed"}


def replay_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """回放指定会话的领域事件，并重建最新会话快照与消息列表。"""
    if not records:
        return {"session": None, "messages": [], "records": []}
    messages: list[dict[str, Any]] = []
    pending_question: dict[str, Any] | None = None
    session_meta = require_session_meta(records)
    session_data: dict[str, Any] = {
        "session_id": session_meta["session_id"],
        "title": session_meta["data"].get("title"),
        "workspace_id": session_meta["data"].get("workspace_id"),
        "workspace_path": session_meta["data"].get("workspace_path"),
        "created_at": session_meta.get("created_at"),
        "updated_at": session_meta.get("updated_at") or session_meta.get("created_at"),
        "metadata": {},
    }
    session_snapshot: dict[str, Any] | None = {
        "record_type": "session_meta",
        "session_id": session_meta["session_id"],
        "created_at": session_meta.get("created_at"),
        "data": session_data,
    }
    for record in records:
        if record["record_type"] == "message":
            messages.append(record["data"])
        if record["record_type"] == "human_interaction":
            apply_human_interaction(messages, record)
            pending_question = apply_pending_question(pending_question, record)
        if record["record_type"] == "session_compacted":
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            if data.get("scope") == "context":
                messages = replace_context_messages(messages, str(data.get("context_id") or "main"), data.get("messages") or [])
            else:
                messages = list(data.get("messages") or [])
        if record["record_type"] in SESSION_LIFECYCLE_RECORD_TYPES:
            session_data = {**session_data, **(record.get("data") or {})}
            session_snapshot = {**record, "data": session_data}
    return {
        "session": session_snapshot,
        "messages": messages,
        "records": records,
        "pending_question": pending_question,
    }


def apply_pending_question(
    pending_question: dict[str, Any] | None,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """从追加式人工交互事件恢复仍待回答的问题，避免依赖可变的消息快照。"""
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    if data.get("kind") != "question":
        return pending_question
    interaction_id = str(data.get("interaction_id") or record.get("interaction_id") or "")
    if data.get("status") == "pending":
        request = data.get("request") if isinstance(data.get("request"), dict) else {}
        question_id = str(request.get("question_id") or interaction_id)
        questions = request.get("questions")
        if question_id and isinstance(questions, list):
            return {
                "question_id": question_id,
                "questions": questions,
                "created_at": request.get("created_at"),
            }
        return pending_question
    if data.get("status") in {"resolved", "declined"} and pending_question:
        if interaction_id == str(pending_question.get("question_id") or ""):
            return None
    return pending_question


def build_session_summary(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从 JSONL 记录流中提取轻量摘要，避免前端加载完整消息体。"""
    if not records:
        return None
    session_data: dict[str, Any] = {}
    session_id = ""
    created_at = ""
    updated_at = ""
    status = ""
    summary_messages: list[dict[str, Any]] = []
    preview = ""
    session_meta = require_session_meta(records)
    session_data.update(session_meta.get("data") or {})
    created_at = str(session_meta.get("created_at") or "")
    updated_at = str(session_meta.get("updated_at") or created_at)
    for record in records:
        session_id = str(record.get("session_id") or session_id)
        record_created_at = str(record.get("created_at") or "")
        if record_created_at and record.get("record_type") != "session_meta":
            updated_at = record_created_at
        if record.get("record_type") == "message":
            data = record.get("data")
            if isinstance(data, dict):
                summary_messages.append(data)
            if not preview:
                preview = message_preview(data)
        if record.get("record_type") == "session_compacted":
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            messages = data.get("messages") or []
            if data.get("scope") == "context":
                summary_messages = replace_context_messages(summary_messages, str(data.get("context_id") or "main"), messages)
            else:
                summary_messages = list(messages)
            if not preview:
                preview = first_message_preview(messages)
        if record.get("record_type") in SESSION_LIFECYCLE_RECORD_TYPES:
            session_data = {**session_data, **(record.get("data") or {})}
            status = str(session_data.get("status") or status)
            updated_at = str(session_data.get("updated_at") or updated_at)
    if not session_id:
        return None
    return {
        "session_id": session_id,
        "title": session_data.get("title"),
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "status": status or session_data.get("status") or "UNKNOWN",
        "agent_name": session_data.get("agent_name") or "",
        "provider": session_data.get("provider"),
        "model": session_data.get("model"),
        "message_count": len(summary_messages),
        "preview": truncate_preview(preview),
        "source": session_data.get("source"),
        "schedule_task_id": session_data.get("schedule_task_id"),
        "schedule_run_id": session_data.get("schedule_run_id"),
        "schedule_task_name": session_data.get("schedule_task_name"),
    }


def apply_human_interaction(messages: list[dict[str, Any]], record: dict[str, Any]) -> None:
    """回放人工交互记录，补齐需要返回给 LLM 的工具结果。"""
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    kind = data.get("kind")
    status = data.get("status")
    if (kind, status) not in {("question", "resolved"), ("question", "declined"), ("approval", "rejected")}:
        return
    message_id = str(data.get("message_id") or "")
    call_id = str(data.get("call_id") or "")
    if not message_id or not call_id:
        return

    for message in messages:
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        if info.get("id") != message_id:
            continue
        if kind == "question" and status == "resolved":
            complete_question_tool_part(message, data, call_id)
        elif kind == "question":
            decline_question_tool_part(message, data, call_id)
        else:
            reject_approval_tool_part(message, data, call_id)
        return


def complete_question_tool_part(message: dict[str, Any], data: dict[str, Any], call_id: str) -> None:
    """根据 call_id 补齐 question 工具结果，并同步步骤完成原因。"""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return
    matched = False
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool" and part.get("call_id") == call_id and part.get("tool") == "question":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            part["state"] = {
                **state,
                "status": "completed",
                "output": data.get("tool_output") if isinstance(data.get("tool_output"), dict) else fallback_question_output(data),
            }
            matched = True
    if not matched:
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "step-finish" and part.get("reason") == "tool_pending":
            part["reason"] = "tool_completed"
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    if info.get("role") == "assistant":
        info["finish"] = "tool_completed"


def reject_approval_tool_part(message: dict[str, Any], data: dict[str, Any], call_id: str) -> None:
    """根据审批拒绝事件把 pending 工具调用补成 error，避免历史上下文无法继续发送。"""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return
    matched = False
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool" and part.get("call_id") == call_id:
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            output = data.get("tool_output") if isinstance(data.get("tool_output"), dict) else fallback_rejected_tool_output(data, part)
            part["state"] = {
                **state,
                "status": "error",
                "output": output,
                "error": {
                    "code": "ToolApprovalRejected",
                    "message": str(output.get("error_message") or "用户拒绝执行该工具调用。"),
                    "detail": {},
                },
            }
            matched = True
    if not matched:
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "step-finish" and part.get("reason") == "tool_pending":
            part["reason"] = "tool_completed"
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    if info.get("role") == "assistant":
        info["finish"] = "tool_completed"


def decline_question_tool_part(message: dict[str, Any], data: dict[str, Any], call_id: str) -> None:
    """根据 question 拒答事件把 pending question 工具调用补成 error。"""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return
    matched = False
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool" and part.get("call_id") == call_id and part.get("tool") == "question":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            output = data.get("tool_output") if isinstance(data.get("tool_output"), dict) else fallback_declined_question_output(data)
            part["state"] = {
                **state,
                "status": "error",
                "output": output,
                "error": {
                    "code": "QuestionDeclined",
                    "message": str(output.get("error_message") or "用户拒绝回答 question 工具提出的问题。"),
                    "detail": {},
                },
            }
            matched = True
    if not matched:
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "step-finish" and part.get("reason") == "tool_pending":
            part["reason"] = "tool_completed"
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    if info.get("role") == "assistant":
        info["finish"] = "tool_completed"


def fallback_rejected_tool_output(data: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    """兼容缺少 tool_output 的旧审批事件。"""
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    comment = str(result.get("comment") or "未提供备注").strip()
    return {
        "status": "error",
        "tool_name": str(part.get("tool") or "unknown"),
        "error_type": "ToolApprovalRejected",
        "error_message": f"用户拒绝执行该工具调用：{comment}",
        "recoverable": True,
        "approval_id": data.get("interaction_id"),
        "approved": False,
        "comment": result.get("comment"),
    }


def fallback_declined_question_output(data: dict[str, Any]) -> dict[str, Any]:
    """兼容缺少 tool_output 的旧 question 拒答事件。"""
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    comment = str(result.get("comment") or "未提供备注").strip()
    return {
        "status": "error",
        "tool_name": "question",
        "error_type": "QuestionDeclined",
        "error_message": f"用户拒绝回答 question 工具提出的问题：{comment}",
        "recoverable": True,
        "question_id": data.get("interaction_id"),
        "declined": True,
        "comment": result.get("comment"),
    }


def fallback_question_output(data: dict[str, Any]) -> dict[str, Any]:
    """缺少完整工具输出时，用 interaction 结果生成可读的工具输出。"""
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    return {
        "status": "ok",
        "tool_name": "question",
        "question_id": data.get("interaction_id"),
        "answers": result.get("answers") if isinstance(result.get("answers"), dict) else {},
        "output": data.get("output") or "用户已回答 question 工具提出的问题。",
    }


def replace_context_messages(
    messages: list[dict[str, Any]],
    context_id: str,
    replacement: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    replaced = False
    result: list[dict[str, Any]] = []
    for message in messages:
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        message_context_id = str(info.get("context_id") or "main")
        if message_context_id != context_id:
            result.append(message)
            continue
        if not replaced:
            result.extend(item for item in replacement if isinstance(item, dict))
            replaced = True
    if not replaced:
        result.extend(item for item in replacement if isinstance(item, dict))
    return result


def require_session_meta(records: list[dict[str, Any]]) -> dict[str, Any]:
    """新格式强制第一条记录为 session_meta，不再兼容旧 JSONL 布局。"""
    first = records[0]
    if first.get("record_type") != "session_meta":
        raise ValueError("session jsonl 第一条记录必须是 session_meta")
    data = first.get("data")
    if not isinstance(data, dict):
        raise ValueError("session_meta.data 必须是对象")
    return first


def first_message_preview(messages: list[Any]) -> str:
    """优先取第一条用户消息文本，作为历史会话列表中的摘要。"""
    for message in messages:
        preview = message_preview(message)
        if preview:
            return preview
    return ""


def message_preview(message: Any) -> str:
    """从持久化消息字典中提取可读文本摘要。"""
    if not isinstance(message, dict):
        return ""
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    if info.get("role") != "user":
        return ""
    texts = [
        str(part.get("text") or "")
        for part in message.get("parts") or []
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ]
    return " ".join(text.strip() for text in texts if text.strip())


def truncate_preview(value: str, limit: int = 80) -> str:
    """限制摘要长度，避免长输入撑开侧边栏。"""
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else f"{normalized[:limit]}..."
