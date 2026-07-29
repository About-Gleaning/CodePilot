"""Hook 运行契约。

这个模块定义 Hook 执行时共享的上下文、运行时句柄与返回结果，
让 Hook 本身只关心业务扩展，不直接耦合会话主循环的内部实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from codepilot.events import StreamEvent
from codepilot.session import AgentState, ApprovalRequest, LLMState, Message, SessionState


@dataclass(slots=True)
class RuntimeHandles:
    """封装 Hook 执行期需要访问的运行时基础设施。"""

    event_bus: Any
    run_ref: Any | None = None
    # 仅保存安全摘要，禁止记录参数、返回正文或凭证。
    active_tools: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class HookContext:
    """聚合一次 Hook 执行可读取的上下文快照。"""

    hook_type: str
    session: SessionState
    workspace: Any
    agent: AgentState
    messages: list[Message]
    current_message: Message | None = None
    llm: LLMState | None = None
    tool_call: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None
    approval: ApprovalRequest | None = None
    config: Any = None
    runtime: RuntimeHandles | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class HookError(BaseModel):
    """描述 Hook 执行失败时对外暴露的结构化错误信息。"""

    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class HookResult(BaseModel):
    """描述 Hook 对消息、事件、上下文和控制流产生的影响。"""

    status: str = "ok"
    messages_to_append: list[Message] = Field(default_factory=list)
    events_to_emit: list[StreamEvent] = Field(default_factory=list)
    context_patch: dict[str, Any] = Field(default_factory=dict)
    stop_loop: bool = False
    fail_session: bool = False
    requires_human_input: bool = False
    human_request: ApprovalRequest | None = None
    error: HookError | None = None
