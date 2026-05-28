from __future__ import annotations

from pathlib import Path
from typing import Any

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import FileToolError, build_tool_failure, build_tool_success, load_tool_description


class WritePlanTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="write_plan",
            description=load_tool_description("write_plan"),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "完整执行计划 Markdown 内容。"},
                },
                "required": ["content"],
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
                raise FileToolError("write_plan 缺少运行上下文。", error_type="ToolContextMissing")
            if context.agent.name != "plan":
                raise FileToolError("write_plan 只能由 plan agent 调用。", error_type="PlanToolAgentForbidden")

            plans_dir = Path(context.workspace.workspace_dir).resolve() / "plans"
            plans_dir.mkdir(parents=True, exist_ok=True)
            plan_path = (plans_dir / f"{context.session.session_id}.md").resolve()
            if not plan_path.is_relative_to(plans_dir.resolve()):
                raise FileToolError("计划文件路径越界。", error_type="PlanPathForbidden")

            content = str(args.get("content", ""))
            plan_path.write_text(content, encoding="utf-8")
            bytes_written = len(content.encode("utf-8"))
            return build_tool_success(
                self.spec.name,
                plan_path=str(plan_path),
                bytes_written=bytes_written,
                output=f"计划写入成功：{plan_path}，共写入 {bytes_written} 字节。",
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)
