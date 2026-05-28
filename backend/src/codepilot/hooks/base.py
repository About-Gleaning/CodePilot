"""Hook 基础定义。

这个模块集中描述 Hook 的类型、异常处理策略与最小执行契约，
供 Hook 管理器和具体插件 Hook 复用，避免各实现自行约定行为。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel

from codepilot.hooks.contracts import HookContext, HookResult


class HookType(str, Enum):
    """定义运行时允许触发 Hook 的标准生命周期节点。"""

    SESSION_BEFORE = "session.before"
    SESSION_AFTER = "session.after"
    LOOP_BEFORE = "loop.before"
    LOOP_AFTER = "loop.after"
    LLM_BEFORE = "llm.before"
    LLM_AFTER = "llm.after"
    TOOL_BEFORE = "tool.before"
    TOOL_AFTER = "tool.after"


class HookErrorPolicy(str, Enum):
    """定义 Hook 执行失败后的统一处理策略。"""

    CONTINUE = "continue"
    BREAK_LOOP = "break_loop"
    FAIL_SESSION = "fail_session"
    REQUIRE_HUMAN = "require_human"


class BaseHook(BaseModel, ABC):
    """声明所有 Hook 实现必须具备的公共配置与执行入口。"""

    hook_id: str
    hook_type: HookType
    name: str
    description: str | None = None
    enabled: bool = True
    order: int = 100
    on_error: HookErrorPolicy = HookErrorPolicy.CONTINUE
    timeout_seconds: float | None = None
    blocking: bool = True
    allow_modify_context: bool = True
    allow_emit_message: bool = True
    allow_emit_event: bool = True
    applies_to_agents: list[str] | None = None
    applies_to_modes: list[str] | None = None
    applies_to_tools: list[str] | None = None

    @abstractmethod
    async def execute(self, ctx: HookContext) -> HookResult:
        """执行 Hook 逻辑，并返回对后续运行流程的影响结果。"""
        raise NotImplementedError
