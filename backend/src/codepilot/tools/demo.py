from __future__ import annotations

from typing import Any

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec


class EchoTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="echo_tool",
            description="无副作用示例工具，返回输入文本。",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "需要回显的文本"}},
                "required": ["text"],
            },
            can_parallel=False,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        return {"status": "ok", "tool_name": self.spec.name, "output": f"echo: {args.get('text', '')}"}
