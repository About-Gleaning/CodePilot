from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from codepilot.session.message import Message


class SessionStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    STOPPING = "STOPPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SessionState(BaseModel):
    session_id: str
    title: str | None = None
    workspace_id: str
    workspace_path: str
    agent_name: str
    provider: str
    model: str
    status: SessionStatus
    created_at: str
    updated_at: str
    messages: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentState(BaseModel):
    name: str
    role: str = "main"
    depth: int = 0
    parent_agent_id: str | None = None
    can_call_subagent: bool = False


class LLMState(BaseModel):
    provider: str
    model: str
    max_tokens: int
    temperature: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    approval_id: str
    reason: str
    action: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ApprovalResult(BaseModel):
    approval_id: str
    approved: bool
    comment: str | None = None
    created_at: str


class PendingApproval(BaseModel):
    request: ApprovalRequest
    source: str
    resume_item: dict[str, Any] | None = None


class StopRequest(BaseModel):
    reason: str = "user_requested"
    created_at: str
