"""事件模型定义。

该模块统一描述流式事件与领域事件的数据结构，保证事件总线、
会话运行时与消费端之间使用一致的事件契约。
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from codepilot.session.message import Message


class StreamEvent(BaseModel):
    """面向流式传输场景的事件模型。"""

    # seq 由事件总线在发布时统一分配，初始值仅用于对象创建阶段。
    seq: int = 0
    event_id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    event_type: str
    session_id: str | None = None
    created_at: str
    data: dict[str, Any] = Field(default_factory=dict)


class DomainEventType(str, Enum):
    """系统内部可发布的领域事件类型。"""

    MESSAGE_CREATED = "message_created"
    SESSION_LIFECYCLE = "session_lifecycle"
    APPROVAL = "approval"
    SESSION_COMPACTED = "session_compacted"


class DomainEvent(BaseModel):
    """领域事件基类，承载不同事件共有的元数据。"""

    event_type: DomainEventType
    session_id: str | None = None
    created_at: str
    data: dict[str, Any] = Field(default_factory=dict)


class MessageCreatedEvent(DomainEvent):
    """消息创建完成后发布的领域事件。"""

    event_type: DomainEventType = DomainEventType.MESSAGE_CREATED
    message: Message


class SessionLifecycleEvent(DomainEvent):
    """会话生命周期状态变化时发布的领域事件。"""

    event_type: DomainEventType = DomainEventType.SESSION_LIFECYCLE
    status: str


class ApprovalEvent(DomainEvent):
    """审批流程状态变化时发布的领域事件。"""

    event_type: DomainEventType = DomainEventType.APPROVAL
    approval_id: str
    status: str


class SessionCompactedEvent(DomainEvent):
    """会话上下文压缩完成后发布的领域事件。"""

    event_type: DomainEventType = DomainEventType.SESSION_COMPACTED
