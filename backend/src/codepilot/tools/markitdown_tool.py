from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolPreflightResult, ToolSpec
from codepilot.tools.file_tool_common import (
    FileToolError,
    build_tool_failure,
    build_tool_success,
    is_non_interactive_mode,
    resolve_readable_file_path,
    load_tool_description,
)


MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_CHARS = 50_000


class MarkItDownConvertTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="markitdown_convert",
            description=load_tool_description("markitdown_convert"),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "要转换为 Markdown 的文件绝对路径。"},
                    "offset": {"type": "integer", "description": "从转换后 Markdown 的第几行开始返回，默认 0。", "default": 0},
                    "limit": {"type": "integer", "description": "最多返回多少行；不传表示读取到输出末尾。"},
                },
                "required": ["file_path"],
            },
            can_parallel=False,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
        )

    async def preflight(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolPreflightResult:
        try:
            resolve_readable_file_path(str(args.get("file_path", "")), context)
            return ToolPreflightResult(status="allow")
        except FileToolError as exc:
            if exc.error_type != "FilePathForbidden":
                return ToolPreflightResult(status="allow")
            if is_non_interactive_mode(context):
                return ToolPreflightResult(status="allow")
            return ToolPreflightResult(status="requires_approval", reason=exc.message)

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            target = resolve_readable_file_path(str(args.get("file_path", "")), context)
            self._validate_target(target)
            markdown = await asyncio.to_thread(self._convert_local_file, target)
            output, total_lines, returned_lines, truncated = self._slice_markdown(markdown, args)
            return build_tool_success(
                self.spec.name,
                file_path=str(target),
                output=output,
                content_length=len(markdown),
                total_lines=total_lines,
                returned_lines=returned_lines,
                truncated=truncated,
                offset=max(int(args.get("offset") or 0), 0),
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)

    def _validate_target(self, target: Path) -> None:
        if target.is_dir():
            raise FileToolError(f"目标路径是目录：{target}", error_type="FilePathIsDirectory")
        size = target.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise FileToolError(
                f"文件超过 MarkItDown 转换大小限制：{MAX_INPUT_BYTES // 1024 // 1024}MB。",
                error_type="MarkItDownInputTooLarge",
            )

    def _convert_local_file(self, target: Path) -> str:
        markdown = self._build_markitdown().convert_local(str(target)).text_content
        return str(markdown or "").strip()

    def _build_markitdown(self) -> Any:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise FileToolError(
                "缺少 markitdown 依赖，请在后端环境安装 markitdown extra。",
                error_type="MarkItDownDependencyMissing",
            ) from exc
        return MarkItDown(enable_plugins=False)

    def _slice_markdown(self, markdown: str, args: dict[str, Any]) -> tuple[str, int, int, bool]:
        if markdown == "":
            return "文件已转换，但 Markdown 内容为空。", 0, 0, False

        offset = max(int(args.get("offset") or 0), 0)
        limit_arg = args.get("limit")
        limit = int(limit_arg) if limit_arg is not None else None
        lines = markdown.splitlines()
        selected = lines[offset:]
        line_truncated = False
        if limit is not None and limit >= 0 and limit < len(selected):
            selected = selected[:limit] + [f"... ({len(lines) - offset - limit} more lines)"]
            line_truncated = True

        output = "\n".join(selected)
        char_truncated = len(output) > MAX_OUTPUT_CHARS
        if char_truncated:
            output = output[:MAX_OUTPUT_CHARS]
        return output, len(lines), len(selected), line_truncated or char_truncated
