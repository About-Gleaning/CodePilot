from __future__ import annotations

from typing import Any

from codepilot.memory import append_long_memory, replace_long_memory
from codepilot.memory.long_memory import LongMemoryError
from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import (
    FileToolError,
    build_tool_failure,
    build_tool_success,
    build_unified_diff,
    load_tool_description,
)


class LongMemoryWriteTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="long_memory_write",
            description=load_tool_description("long_memory_write"),
            input_schema={
                "type": "object",
                "properties": {
                    "old_string": {"type": "string", "description": "要替换的原始长期记忆文本；为空表示追加。"},
                    "new_string": {"type": "string", "description": "要追加或替换成的新长期记忆文本。"},
                },
                "required": ["old_string", "new_string"],
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

            old_string = str(args.get("old_string", ""))
            new_string = str(args.get("new_string", ""))
            if old_string == "":
                memory_path, bytes_written = append_long_memory(context.workspace.codepilot_home, new_string)
                return build_tool_success(
                    self.spec.name,
                    memory_path=str(memory_path),
                    operation="append",
                    bytes_written=bytes_written,
                    output=f"长期记忆已保存，共写入 {bytes_written} 字节。",
                )

            memory_path, before, after = replace_long_memory(context.workspace.codepilot_home, old_string, new_string)
            diff = build_unified_diff(memory_path, before, after)
            return build_tool_success(
                self.spec.name,
                memory_path=str(memory_path),
                operation="replace",
                replaced_count=1,
                diff=diff,
                output=f"长期记忆已更新：{memory_path}，共处理 1 处。",
            )
        except LongMemoryError as exc:
            return build_tool_failure(
                self.spec.name,
                FileToolError(exc.message, error_type=exc.error_type),
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)
