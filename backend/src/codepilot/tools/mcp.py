from __future__ import annotations

from typing import Any

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec


class McpToolAdapter(BaseTool):
    def __init__(self, name: str) -> None:
        self.spec = ToolSpec(
            name=name,
            description="MCP 工具适配扩展位，当前未启用。",
            input_schema={"type": "object", "properties": {}},
            can_parallel=False,
            requires_approval=False,
            timeout_seconds=30,
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "disabled",
            "tool_name": self.spec.name,
            "error_type": "McpNotImplemented",
            "error_message": "一期未接入真实 MCP。后续必须通过官方 MCP Python SDK 接入。",
            "recoverable": True,
        }
