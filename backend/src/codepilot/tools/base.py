from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    can_parallel: bool = False
    requires_approval: bool = False
    timeout_seconds: int
    # 能力目录使用的安全标签；保守默认值保证旧扩展不会被误标为只读。
    side_effect: Literal["read_only", "workspace_mutation", "runtime_mutation", "external_mutation"] = "runtime_mutation"
    assignable_to_custom_agents: bool = True
    allowed_agent_names: list[str] = []
    assignment_reason: str | None = None


@dataclass(slots=True)
class ToolExecutionContext:
    session: Any
    workspace: Any
    agent: Any
    runtime: Any | None = None
    config: Any | None = None
    tool_call_id: str | None = None
    stop_event: Any | None = None
    skip_approval: bool = False


@dataclass(slots=True)
class ToolPreflightResult:
    status: str
    reason: str | None = None
    result: dict[str, Any] | None = None


class BaseTool(ABC):
    spec: ToolSpec

    def get_llm_description(self, *, agent_name: str | None = None, agent_readonly: bool | None = None) -> str:
        return self.spec.description

    async def preflight(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolPreflightResult:
        return ToolPreflightResult(status="allow")

    @abstractmethod
    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError
