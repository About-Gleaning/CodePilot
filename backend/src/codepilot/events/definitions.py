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

    SESSION_META = "session_meta"
    MESSAGE_CREATED = "message_created"
    HUMAN_INTERACTION = "human_interaction"
    SESSION_LIFECYCLE = "session_lifecycle"
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


class HumanInteractionEvent(DomainEvent):
    """人工交互生命周期事件，统一覆盖 question 与 approval。"""

    event_type: DomainEventType = DomainEventType.HUMAN_INTERACTION
    interaction_id: str


class SessionMetaEvent(DomainEvent):
    """会话全局索引信息创建或更新时发布的领域事件。"""

    event_type: DomainEventType = DomainEventType.SESSION_META


class SessionLifecycleEvent(DomainEvent):
    """会话生命周期状态变化时发布的领域事件。"""

    event_type: DomainEventType = DomainEventType.SESSION_LIFECYCLE
    status: str


class SessionCompactedEvent(DomainEvent):
    """会话上下文压缩完成后发布的领域事件。"""

    event_type: DomainEventType = DomainEventType.SESSION_COMPACTED
