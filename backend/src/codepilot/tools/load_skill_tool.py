from __future__ import annotations

from typing import Any

from codepilot.skills import SkillRegistry
from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import FileToolError, build_tool_failure, build_tool_success, load_tool_description


class LoadSkillTool(BaseTool):
    def __init__(self, registry: SkillRegistry, timeout_seconds: int) -> None:
        self._registry = registry
        self.spec = ToolSpec(
            name="load_skill",
            description=load_tool_description("load_skill"),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "要加载的 skill 名称。"},
                },
                "required": ["name"],
            },
            can_parallel=True,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
            side_effect="read_only",
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            name = str(args.get("name") or "").strip()
            if not name:
                raise FileToolError("name 必须是非空字符串。", error_type="SkillNameInvalid")

            skill = self._registry.get_skill(name)
            if skill is None:
                raise FileToolError(f"未找到 skill：{name}", error_type="SkillNotFound")

            content = "\n".join(
                [
                    f"## Skill: {skill.name}",
                    f"Base directory: {skill.path}",
                    "",
                    skill.load_full_content(),
                ]
            )
            return build_tool_success(
                self.spec.name,
                name=skill.name,
                dir=str(skill.path),
                output=content,
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)
