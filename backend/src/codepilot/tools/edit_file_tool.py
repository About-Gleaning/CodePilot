from __future__ import annotations

from typing import Any

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import (
    FileToolError,
    build_tool_failure,
    build_tool_success,
    build_unified_diff,
    load_tool_description,
    read_utf8_text_file,
    resolve_workspace_file_path,
)


class EditFileTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="edit_file",
            description=load_tool_description("edit_file"),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "要编辑的文件绝对路径。"},
                    "old_string": {"type": "string", "description": "要替换或删除的原始文本。"},
                    "new_string": {"type": "string", "description": "替换后的文本；为空表示删除。"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配；默认 false。",
                        "default": False,
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
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
            target = resolve_workspace_file_path(str(args.get("file_path", "")), context, allow_missing=False)
            before = read_utf8_text_file(target, tool_name=self.spec.name)
            old_string = str(args.get("old_string", ""))
            new_string = str(args.get("new_string", ""))
            replace_all = bool(args.get("replace_all", False))
            if old_string == new_string:
                raise FileToolError("new_string 必须与 old_string 不同。", error_type="EditContentUnchanged")

            if old_string == "":
                after = before + new_string
                replaced_count = 1
                operation = "append"
            else:
                matches = before.count(old_string)
                if matches == 0:
                    raise FileToolError("未找到 old_string，请重新读取文件后补充上下文再试。", error_type="EditTextNotFound")
                if matches > 1 and not replace_all:
                    raise FileToolError(
                        "old_string 匹配到多处内容，请补充上下文或设置 replace_all=true。",
                        error_type="EditMatchNotUnique",
                    )
                after = before.replace(old_string, new_string, -1 if replace_all else 1)
                replaced_count = matches if replace_all else 1
                operation = "delete" if new_string == "" else "replace"

            target.write_text(after, encoding="utf-8")
            diff = build_unified_diff(target, before, after)
            return build_tool_success(
                self.spec.name,
                file_path=str(target),
                operation=operation,
                replaced_count=replaced_count,
                diff=diff,
                output=f"{operation} 成功：{target}，共处理 {replaced_count} 处。",
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)
