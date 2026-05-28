from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .message import (
    AssistantMessageError,
    AssistantMessageInfo,
    AssistantMessagePath,
    AssistantMessageTokens,
    BaseMessageInfo,
    Message,
    MessageInfo,
    MessageModelRef,
    MessagePart,
    TextPart,
    ToolPart,
    UserMessageInfo,
    build_assistant_message_info,
    build_user_message_info,
)
from .state import AgentState, ApprovalRequest, ApprovalResult, LLMState, PendingApproval, SessionState, SessionStatus, StopRequest

if TYPE_CHECKING:
    from .agents import AgentProfile
    from .session import AgentLoop
    from .session_runner import SessionRunner

__all__ = [
    "AgentLoop",
    "AgentProfile",
    "AgentState",
    "ApprovalRequest",
    "ApprovalResult",
    "LLMState",
    "Message",
    "AssistantMessageError",
    "AssistantMessageInfo",
    "AssistantMessagePath",
    "AssistantMessageTokens",
    "BaseMessageInfo",
    "MessageInfo",
    "MessageModelRef",
    "MessagePart",
    "PendingApproval",
    "SessionRunner",
    "SessionState",
    "SessionStatus",
    "StopRequest",
    "TextPart",
    "ToolPart",
    "UserMessageInfo",
    "build_assistant_message_info",
    "build_user_message_info",
    "build_agent_profiles",
]


def __getattr__(name: str) -> Any:
    # 延迟导入重模块，避免 hooks.contracts -> session.state 时触发 session 包级循环导入。
    if name in {"AgentProfile", "build_agent_profiles"}:
        module = import_module("codepilot.session.agents")
        return getattr(module, name)
    if name == "AgentLoop":
        module = import_module("codepilot.session.session")
        return module.AgentLoop
    if name == "SessionRunner":
        module = import_module("codepilot.session.session_runner")
        return module.SessionRunner
    raise AttributeError(f"module 'codepilot.session' has no attribute {name!r}")
