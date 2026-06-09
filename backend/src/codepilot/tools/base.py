from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    can_parallel: bool = False
    requires_approval: bool = False
    timeout_seconds: int


@dataclass(slots=True)
class ToolExecutionContext:
    session: Any
    workspace: Any
    agent: Any
    runtime: Any | None = None
    config: Any | None = None
    tool_call_id: str | None = None
    stop_event: Any | None = None


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
