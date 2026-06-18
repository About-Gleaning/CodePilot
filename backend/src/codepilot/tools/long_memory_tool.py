from __future__ import annotations

from typing import Any

from codepilot.memory import append_long_memory
from codepilot.memory.long_memory import LongMemoryError
from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import FileToolError, build_tool_failure, build_tool_success, load_tool_description


class LongMemoryWriteTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="long_memory_write",
            description=load_tool_description("long_memory_write"),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "需要长期记住的精炼内容。"},
                },
                "required": ["content"],
                "additionalProperties": False,
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
        try:
            if context is None:
                raise FileToolError("long_memory_write 缺少运行上下文。", error_type="ToolContextMissing")
            if context.agent.name != "life":
                raise FileToolError("long_memory_write 只能由 life agent 调用。", error_type="LongMemoryAgentForbidden")

            memory_path, bytes_written = append_long_memory(
                context.workspace.codepilot_home,
                str(args.get("content", "")),
            )
            return build_tool_success(
                self.spec.name,
                memory_path=str(memory_path),
                bytes_written=bytes_written,
                output=f"长期记忆已保存，共写入 {bytes_written} 字节。",
            )
        except LongMemoryError as exc:
            return build_tool_failure(
                self.spec.name,
                FileToolError(exc.message, error_type=exc.error_type),
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)
