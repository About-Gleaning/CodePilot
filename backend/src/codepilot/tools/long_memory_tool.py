from __future__ import annotations

from typing import Any

from codepilot.memory import append_long_memory, append_user_long_memory, replace_long_memory, replace_user_long_memory
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
            side_effect="runtime_mutation",
            assignable_to_custom_agents=True,
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            if context is None:
                raise FileToolError("long_memory_write 缺少运行上下文。", error_type="ToolContextMissing")
            if not getattr(context.agent, "memory_enabled", True):
                raise FileToolError("当前 Agent 已关闭长期记忆。", error_type="LongMemoryDisabled")
            user_home = getattr(context.workspace, "user_home_dir", None)
            has_capability = self.spec.name in getattr(context.agent, "allowed_tools", [])
            if not has_capability and not (user_home is None and context.agent.name == "life"):
                raise FileToolError("当前 Agent 未获得长期记忆写入能力。", error_type="LongMemoryAgentForbidden")

            agent_id = str(getattr(context.agent, "agent_id", "") or "")

            old_string = str(args.get("old_string", ""))
            new_string = str(args.get("new_string", ""))
            if old_string == "":
                if user_home is not None and agent_id:
                    memory_path, bytes_written = append_user_long_memory(user_home, agent_id, new_string)
                else:
                    memory_path, bytes_written = append_long_memory(context.workspace.codepilot_home, new_string)
                return build_tool_success(
                    self.spec.name,
                    memory_path=str(memory_path),
                    operation="append",
                    bytes_written=bytes_written,
                    output=f"长期记忆已保存，共写入 {bytes_written} 字节。",
                )

            if user_home is not None and agent_id:
                memory_path, before, after = replace_user_long_memory(user_home, agent_id, old_string, new_string)
            else:
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
