from __future__ import annotations

from enum import Enum
from typing import Any, Literal

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


class AgentLifecycleState(str, Enum):
    """Agent 进程内生命周期；与单次会话运行状态分离。"""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class RunStatus(str, Enum):
    """一次用户消息触发的执行状态。"""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    CANCELLING = "CANCELLING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunRef(BaseModel):
    """所有运行时控制操作必须携带的完整资源归属。"""

    agent_id: str
    session_id: str
    run_id: str
    revision_id: str = ""


class InteractionStatus(str, Enum):
    PENDING = "PENDING"
    RESOLVING = "RESOLVING"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class HumanInteractionRef(BaseModel):
    agent_id: str
    session_id: str
    run_id: str
    interaction_id: str


class HumanInteractionState(BaseModel):
    ref: HumanInteractionRef
    kind: Literal["approval", "question"]
    status: InteractionStatus = InteractionStatus.PENDING
    result_fingerprint: str | None = None
    created_at: str
    ended_at: str | None = None


class RunState(BaseModel):
    ref: RunRef
    client_request_id: str
    status: RunStatus = RunStatus.STARTING
    run_seq: int = 0
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    error_code: str | None = None
    request_fingerprint: str = ""
    provider: str | None = None
    model: str | None = None
    thinking_value: str | None = None


class AgentRuntimeState(BaseModel):
    agent_id: str
    desired_state: AgentLifecycleState = AgentLifecycleState.STOPPED
    lifecycle_state: AgentLifecycleState = AgentLifecycleState.STOPPED
    recent_session_id: str | None = None
    active_run_count: int = 0
    waiting_human_count: int = 0
    error_code: str | None = None


class SessionState(BaseModel):
    session_id: str
    agent_id: str = ""
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
    role: str = "agent"
    kind: Literal["agent", "subagent"] = "agent"
    allowed_tools: list[str] = Field(default_factory=list)
    readonly: bool = False
    context_id: str | None = "main"
    parent_call_id: str | None = None
    depth: int = 0
    parent_agent_id: str | None = None
    can_call_subagent: bool = False


class LLMState(BaseModel):
    provider: str
    model: str
    max_tokens: int
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


class QuestionRequest(BaseModel):
    question_id: str
    questions: list[dict[str, Any]]
    created_at: str


class QuestionResult(BaseModel):
    question_id: str
    answers: dict[str, Any] = Field(default_factory=dict)
    declined: bool = False
    comment: str | None = None
    created_at: str


class PendingQuestion(BaseModel):
    request: QuestionRequest
    source: str
    resume_item: dict[str, Any] | None = None


class StopRequest(BaseModel):
    reason: str = "user_requested"
    created_at: str
