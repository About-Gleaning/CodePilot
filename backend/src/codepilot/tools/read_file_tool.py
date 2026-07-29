from __future__ import annotations

from typing import Any

from codepilot.session.attachments import (
    MAX_IMAGE_ATTACHMENT_BYTES,
    SUPPORTED_IMAGE_MIMES,
    detect_image_mime,
)
from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolPreflightResult, ToolSpec
from codepilot.tools.file_tool_common import (
    build_tool_failure,
    build_tool_success,
    is_non_interactive_mode,
    FileToolError,
    load_tool_description,
    read_utf8_text_file,
    resolve_readable_file_path,
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
            side_effect="read_only",
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
            image_result = self._read_image_file(target)
            if image_result is not None:
                return build_tool_success(self.spec.name, **image_result)
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

    def _read_image_file(self, target: Any) -> dict[str, Any] | None:
        if target.is_dir():
            return None
        with target.open("rb") as file:
            header = file.read(16)
        mime = detect_image_mime(header)
        if mime not in SUPPORTED_IMAGE_MIMES:
            return None
        size = target.stat().st_size
        if size > MAX_IMAGE_ATTACHMENT_BYTES:
            return {
                "file_path": str(target),
                "mime": mime,
                "bytes": size,
                "output": f"图片文件超过 {MAX_IMAGE_ATTACHMENT_BYTES // 1024 // 1024}MB，未发送给 LLM：{target.name}。",
                "attachments": [],
            }
        return {
            "file_path": str(target),
            "mime": mime,
            "bytes": size,
            "output": f"已读取图片文件：{target.name}（{mime}，{size} bytes）。",
            "attachments": [
                {
                    "type": "image",
                    "mime": mime,
                    "filename": target.name,
                    "source_path": str(target),
                    "bytes": size,
                }
            ],
        }
