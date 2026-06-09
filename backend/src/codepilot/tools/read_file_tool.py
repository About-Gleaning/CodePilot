from __future__ import annotations

from typing import Any

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import (
    build_tool_failure,
    build_tool_success,
    load_tool_description,
    read_utf8_text_file,
    resolve_workspace_file_path,
)


EMPTY_FILE_OUTPUT = "文件存在，但内容为空。"
MAX_TEXT_OUTPUT_CHARS = 50_000


class ReadFileTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="read_file",
            description=load_tool_description("read_file"),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "要读取的文件绝对路径。"},
                    "offset": {"type": "integer", "description": "从第几行开始读取，默认 0。", "default": 0},
                    "limit": {"type": "integer", "description": "最多读取多少行；不传表示读取到文件末尾。"},
                },
                "required": ["file_path"],
            },
            can_parallel=True,
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
            text = read_utf8_text_file(target, tool_name=self.spec.name)
            if text == "":
                return build_tool_success(self.spec.name, file_path=str(target), output=EMPTY_FILE_OUTPUT, is_empty=True)

            offset = max(int(args.get("offset") or 0), 0)
            limit_arg = args.get("limit")
            limit = int(limit_arg) if limit_arg is not None else None
            lines = text.splitlines()
            selected = lines[offset:]
            if limit is not None and limit >= 0 and limit < len(selected):
                selected = selected[:limit] + [f"... ({len(lines) - offset - limit} more lines)"]
            output = "\n".join(selected)[:MAX_TEXT_OUTPUT_CHARS]
            return build_tool_success(self.spec.name, file_path=str(target), output=output, is_empty=False)
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)
