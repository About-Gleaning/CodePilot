from __future__ import annotations

from typing import Any

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import (
    FileToolError,
    build_tool_failure,
    build_tool_success,
    load_tool_description,
    resolve_workspace_file_path,
)


class WriteFileTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="write_file",
            description=load_tool_description("write_file"),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "要创建的文件绝对路径。"},
                    "content": {"type": "string", "description": "要写入的新文件内容。"},
                },
                "required": ["file_path", "content"],
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
            target = resolve_workspace_file_path(str(args.get("file_path", "")), context, allow_missing=True)
            if target.exists() and target.is_dir():
                raise FileToolError(f"目标路径是目录，无法创建文件：{target}", error_type="FilePathIsDirectory")
            if target.exists():
                raise FileToolError(f"文件已存在：{target}。请改用 edit_file。", error_type="FileAlreadyExists")

            content = str(args.get("content", ""))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            bytes_written = len(content.encode("utf-8"))
            return build_tool_success(
                self.spec.name,
                file_path=str(target),
                bytes_written=bytes_written,
                output=f"创建成功：{target}，共写入 {bytes_written} 字节。",
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)
