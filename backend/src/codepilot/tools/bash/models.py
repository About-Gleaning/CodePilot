from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel


ApprovalStatus = Literal["allow", "requires_approval", "blocked"]


class BashRequest(BaseModel):
    command: str
    cwd: str = "."
    timeout_seconds: int | None = None
    description: str | None = None


@dataclass(slots=True)
class ApprovalDecision:
    status: ApprovalStatus
    reason: str


@dataclass(slots=True)
class BashResult:
    status: str
    tool_name: str
    command: str
    cwd: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_ms: int = 0
    error_type: str | None = None
    error_message: str | None = None

    def to_tool_result(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "tool_name": self.tool_name,
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "duration_ms": self.duration_ms,
        }
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.error_message:
            payload["error_message"] = self.error_message
            payload["recoverable"] = True
        return payload
