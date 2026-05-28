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


class BaseTool(ABC):
    spec: ToolSpec

    @abstractmethod
    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError
