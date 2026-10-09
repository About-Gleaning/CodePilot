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
                    "resource_path": {"type": "string", "description": "Skill 当前内容中的相对文件路径，默认 SKILL.md。"},
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

            scoped = getattr(getattr(context, "runtime", None), "skill_registry", None)
            skill = (scoped if scoped is not None else self._registry).get_skill(name)
            if skill is None:
                raise FileToolError(f"未找到 skill：{name}", error_type="SkillNotFound")

            resource_path = str(args.get("resource_path") or "SKILL.md")
            from codepilot.skills.store import SkillStore
            identity = str(skill.metadata.get("skill_id", ""))
            directory = skill.path
            # 名称只在已授权清单内匹配；按稳定身份读取当前文件，不复用缓存正文。
            if identity.startswith("public:"):
                from codepilot.session.publications import split_identity
                publication, resource = split_identity(identity)
                store = SkillStore(context.workspace.codepilot_home, publication_id=publication)
                raw, directory = store.read_resource(context.workspace.user_id, resource, resource_path, active=True)
            elif identity and not identity.startswith("shared:"):
                store = SkillStore(context.workspace.codepilot_home)
                raw, directory = store.read_resource(context.workspace.user_id, identity, resource_path, active=True)
            else:
                raw = SkillStore.read_shared_path(skill.path, resource_path)
            try:
                resource_content = raw.decode("utf-8")
            except UnicodeError as exc:
                raise FileToolError("二进制资源请通过 Skill 文件下载入口获取", error_type="SkillBinaryResource") from exc
            content = "\n".join(
                [
                    f"## Skill: {skill.name}",
                    f"Base directory: {directory}",
                    "",
                    resource_content[:65536],
                ]
            )
            return build_tool_success(
                self.spec.name,
                name=skill.name,
                dir=str(directory),
                output=content,
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)
