from __future__ import annotations

"""领域事件到 JSONL 记录的转换逻辑。"""

from typing import Any

from codepilot.events import (
    DomainEvent,
    HumanInteractionEvent,
    MessageCreatedEvent,
    SessionCompactedEvent,
    SessionLifecycleEvent,
    SessionMetaEvent,
)


def domain_event_to_record(event: DomainEvent) -> dict[str, Any]:
    """把不同类型的领域事件映射成统一可落盘的记录结构。"""
    ownership = {
        "agent_id": event.agent_id,
        "run_id": event.run_id,
        "revision_id": event.revision_id,
    }
    if isinstance(event, SessionMetaEvent):
        return {
            "record_type": "session_meta",
            "session_id": event.session_id,
            "created_at": event.created_at,
            "updated_at": event.data.get("updated_at") or event.created_at,
            "data": event.data,
            **ownership,
        }
    if isinstance(event, MessageCreatedEvent):
        return {
            "record_type": "message",
            "session_id": event.session_id,
            "message_id": event.message.info.id,
            "created_at": event.created_at,
            "data": event.message.model_dump(),
            **ownership,
        }
    if isinstance(event, HumanInteractionEvent):
        return {
            "record_type": "human_interaction",
            "session_id": event.session_id,
            "interaction_id": event.interaction_id,
            "created_at": event.created_at,
            "data": event.data,
            **ownership,
        }
    if isinstance(event, SessionCompactedEvent):
        return {
            "record_type": "session_compacted",
            "session_id": event.session_id,
            "created_at": event.created_at,
            "data": event.data,
            **ownership,
        }
    if isinstance(event, SessionLifecycleEvent):
        lifecycle_type = _lifecycle_record_type(event)
        return {
            "record_type": lifecycle_type,
            "session_id": event.session_id,
            "created_at": event.created_at,
            "data": lifecycle_data(event),
            **ownership,
        }
    return {
        "record_type": event.event_type.value,
        "session_id": event.session_id,
        "created_at": event.created_at,
        "data": event.data,
        **ownership,
    }


def lifecycle_data(event: SessionLifecycleEvent) -> dict[str, Any]:
    """只持久化状态事件自身需要的字段，避免重复保存会话全局信息。"""
    data = event.data
    result = {
        "status": event.status,
        "agent_name": data.get("agent_name"),
        "provider": data.get("provider"),
        "model": data.get("model"),
        "updated_at": data.get("updated_at") or event.created_at,
    }
    optional = {
        "agent_id": event.agent_id or data.get("agent_id"),
        "run_id": event.run_id or data.get("run_id"),
        "revision_id": event.revision_id or data.get("revision_id"),
    }
    result.update({key: value for key, value in optional.items() if value})
    return result


def _lifecycle_record_type(event: SessionLifecycleEvent) -> str:
    lifecycle_type = "session_status_changed"
    if event.status == "RUNNING":
        lifecycle_type = "session_started"
        if event.data.get("lifecycle_record_type") == "session_status_changed":
            lifecycle_type = "session_status_changed"
    if event.status in {"COMPLETED", "CANCELLED"}:
        lifecycle_type = "session_finished"
    if event.status == "FAILED":
        lifecycle_type = "session_failed"
    return lifecycle_type
