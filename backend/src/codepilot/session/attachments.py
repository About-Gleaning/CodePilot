from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import Any


SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGE_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_IMAGE_ATTACHMENTS_PER_MESSAGE = 4
# base64 最坏约为原始字节的 4/3，并为 data URL 前缀保留少量空间。
MAX_IMAGE_ATTACHMENT_BASE64_CHARS = ((MAX_IMAGE_ATTACHMENT_BYTES + 2) // 3) * 4 + 128


class AttachmentError(ValueError):
    """附件输入不符合当前安全约束。"""


def sanitize_attachment_filename(filename: str) -> str:
    """清理用户上传文件名，避免路径穿越和控制字符进入落盘路径。"""

    name = Path(filename or "attachment").name.strip()
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120].strip("._") or "attachment"


def decode_image_attachment(data_base64: str, declared_mime: str) -> tuple[bytes, str]:
    """解码并校验图片附件，返回图片字节和按文件头确认的 MIME。"""

    if declared_mime not in SUPPORTED_IMAGE_MIMES:
        raise AttachmentError(f"当前仅支持图片附件：{', '.join(sorted(SUPPORTED_IMAGE_MIMES))}")
    try:
        data = base64.b64decode(_strip_data_url_prefix(data_base64), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("附件内容不是合法 base64。") from exc
    if not data:
        raise AttachmentError("附件内容为空。")
    if len(data) > MAX_IMAGE_ATTACHMENT_BYTES:
        raise AttachmentError(f"单个图片附件不能超过 {MAX_IMAGE_ATTACHMENT_BYTES // 1024 // 1024}MB。")
    detected_mime = detect_image_mime(data)
    if detected_mime is None:
        raise AttachmentError("附件内容不是受支持的图片格式。")
    if detected_mime != declared_mime:
        raise AttachmentError(f"附件 MIME 与文件内容不一致：声明 {declared_mime}，实际 {detected_mime}。")
    return data, detected_mime


def detect_image_mime(data: bytes) -> str | None:
    """基于文件头识别首期支持的图片类型。"""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_file_to_data_url(path: Path, mime: str | None = None) -> str:
    """按需把图片文件编码成 data URL，避免持久化层保存大块 base64。"""

    data = path.read_bytes()
    detected = detect_image_mime(data)
    resolved_mime = mime or detected
    if resolved_mime not in SUPPORTED_IMAGE_MIMES or detected is None:
        raise AttachmentError(f"文件不是受支持的图片：{path}")
    if len(data) > MAX_IMAGE_ATTACHMENT_BYTES:
        raise AttachmentError(f"图片超过 {MAX_IMAGE_ATTACHMENT_BYTES // 1024 // 1024}MB：{path}")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{resolved_mime};base64,{encoded}"


def resolve_stored_attachment_path(workspace: Any, raw_path: str) -> Path:
    """解析已保存附件路径，并限制在 CodePilot 工作区附件目录内。"""

    root = attachment_root(workspace).resolve()
    path = Path(raw_path).expanduser().resolve(strict=False)
    if not path.is_relative_to(root):
        raise AttachmentError(f"附件路径超出允许范围：{raw_path}")
    if not path.exists() or not path.is_file():
        raise AttachmentError(f"附件文件不存在：{path}")
    return path


def attachment_root(workspace: Any) -> Path:
    return Path(workspace.workspace_dir).resolve() / "attachments"


def attachment_message_dir(workspace: Any, session_id: str, message_id: str) -> Path:
    return attachment_root(workspace) / _safe_segment(session_id) / _safe_segment(message_id)


def _safe_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_-]+", "_", value or "")
    if not segment:
        raise AttachmentError("附件路径缺少必要标识。")
    return segment


def _strip_data_url_prefix(value: str) -> str:
    marker = ";base64,"
    if marker in value[:128]:
        return value.split(marker, 1)[1]
    return value
