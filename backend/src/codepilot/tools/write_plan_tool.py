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
            side_effect="workspace_mutation",
            assignable_to_custom_agents=True,
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            if context is None:
                raise FileToolError("write_plan 缺少运行上下文。", error_type="ToolContextMissing")
            # 个人副本按能力快照授权；无能力字段的旧上下文保留 plan 限制。
            allowed = getattr(context.agent, "allowed_tools", None)
            if (allowed is not None and "write_plan" not in allowed) or (allowed is None and context.agent.name != "plan"):
                raise FileToolError("当前 Agent 未获得计划写入能力。", error_type="PlanToolAgentForbidden")

            workspace_root = Path(context.workspace.workspace_path).resolve()
            plans_dir = workspace_root / ".codepilot" / "plans"
            if not plans_dir.resolve().is_relative_to(workspace_root):
                raise FileToolError("计划目录越界。", error_type="PlanPathForbidden")
            plans_dir.mkdir(parents=True, exist_ok=True)
            plan_path = (plans_dir / f"{context.session.session_id}.md").resolve()
            if not plan_path.is_relative_to(plans_dir.resolve()):
                raise FileToolError("计划文件路径越界。", error_type="PlanPathForbidden")

            content = str(args.get("content", ""))
            if len(content.encode("utf-8")) > 1024 * 1024:
                raise FileToolError("计划正文最多 1 MiB。", error_type="PlanContentTooLarge")
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
