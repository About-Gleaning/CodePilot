"""统一导出 Hook 子系统的公共类型与内置实现。"""

from .base import BaseHook, HookErrorPolicy, HookType
from .contracts import HookContext, HookError, HookResult, RuntimeHandles
from .manager import HookManager
from .approval import ApprovalHook
from .plugins import (
    AgentPluginHook,
    CommandPluginHook,
    HttpPluginHook,
    PromptPluginHook,
)

__all__ = [
    "AgentPluginHook",
    "ApprovalHook",
    "BaseHook",
    "CommandPluginHook",
    "HookContext",
    "HookError",
    "HookErrorPolicy",
    "HookManager",
    "HookResult",
    "HookType",
    "HttpPluginHook",
    "PromptPluginHook",
    "RuntimeHandles",
]
