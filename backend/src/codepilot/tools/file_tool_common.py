from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from codepilot.session.attachments import AttachmentError, resolve_stored_attachment_path
from codepilot.tools.base import ToolExecutionContext


DESCRIPTION_DIR = Path(__file__).resolve().parent / "descriptions"


class FileToolError(Exception):
    """表示模型可通过调整参数修复的文件工具业务错误。"""

    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type


def load_tool_description(name: str) -> str:
    return (DESCRIPTION_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


def build_tool_success(tool_name: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "tool_name": tool_name, **payload}


def build_tool_failure(tool_name: str, exc: Exception) -> dict[str, Any]:
    error_type = exc.error_type if isinstance(exc, FileToolError) else exc.__class__.__name__
    error_message = exc.message if isinstance(exc, FileToolError) else str(exc)
    return {
        "status": "error",
        "tool_name": tool_name,
        "error_type": error_type,
        "error_message": error_message,
        "recoverable": True,
    }


def resolve_workspace_file_path(
    file_path: str,
    context: ToolExecutionContext | None,
    *,
    allow_missing: bool,
) -> Path:
    if context is None:
        raise FileToolError("文件工具缺少运行上下文。", error_type="ToolContextMissing")

    workspace_root = Path(context.workspace.workspace_path).resolve()
    raw_path = Path(file_path).expanduser()
    if not raw_path.is_absolute():
        raise FileToolError("file_path 必须是绝对路径。", error_type="FilePathNotAbsolute")

    resolved = raw_path.resolve(strict=False)
    if resolved.is_relative_to(workspace_root):
        target = resolved
    else:
        if raw_path.exists():
            raise FileToolError(f"路径超出工作区范围：{file_path}", error_type="FilePathForbidden")
        # 支持 /backend/foo.py 这类“仓库根目录绝对路径”，最终仍限制在 workspace 内。
        root_relative = Path(*raw_path.parts[1:])
        target = (workspace_root / root_relative).resolve(strict=False)

    if not target.is_relative_to(workspace_root):
        raise FileToolError(f"路径超出工作区范围：{file_path}", error_type="FilePathForbidden")
    if not allow_missing and not target.exists():
        raise FileToolError(f"文件不存在：{target}", error_type="FileNotFound")
    return target


def resolve_readable_file_path(file_path: str, context: ToolExecutionContext | None) -> Path:
    """解析可读文件：工作区优先，其次附件，工作区外路径必须已获批准。"""

    try:
        return resolve_workspace_file_path(file_path, context, allow_missing=False)
    except FileToolError as exc:
        if exc.error_type != "FilePathForbidden" or context is None:
            raise
        try:
            return resolve_stored_attachment_path(context.workspace, file_path)
        except AttachmentError:
            if not (context.skip_approval or is_non_interactive_mode(context)):
                raise exc
            raw_path = Path(file_path).expanduser()
            if not raw_path.is_absolute():
                raise exc
            target = raw_path.resolve(strict=False)
            if not target.exists():
                raise FileToolError(f"文件不存在：{target}", error_type="FileNotFound")
            return target


def is_non_interactive_mode(context: ToolExecutionContext) -> bool:
    config = getattr(context, "config", None)
    hitl = getattr(config, "human_in_the_loop", None)
    return hitl is not None and not bool(getattr(hitl, "enabled", True))


def read_utf8_text_file(target: Path, *, tool_name: str) -> str:
    if target.is_dir():
        raise FileToolError(f"目标路径是目录：{target}", error_type="FilePathIsDirectory")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FileToolError(
            f"{tool_name} 仅支持 UTF-8 文本文件：{target}，无法解码字节位置 {exc.start}。",
            error_type="FileEncodingUnsupported",
        ) from exc


def build_unified_diff(file_path: Path, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=str(file_path),
            tofile=str(file_path),
            lineterm="",
        )
    )
