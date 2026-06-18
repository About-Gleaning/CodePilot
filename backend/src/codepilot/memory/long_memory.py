from __future__ import annotations

from pathlib import Path


LONG_MEMORY_RELATIVE_PATH = Path("instructions") / "memory.instruction.md"
MAX_LONG_MEMORY_CONTENT_CHARS = 2000
DEFAULT_MEMORY_HEADER = """---
type: memory_instruction
version: 1
applyTo:
  - life
---
"""


class LongMemoryError(Exception):
    """表示长期记忆读写中的业务错误。"""

    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type


def long_memory_path(codepilot_home: Path) -> Path:
    """返回长期记忆文件路径，并限制最终路径仍位于 codepilot_home 内。"""
    root = codepilot_home.expanduser().resolve()
    path = (root / LONG_MEMORY_RELATIVE_PATH).resolve()
    if not path.is_relative_to(root):
        raise LongMemoryError("长期记忆文件路径越界。", error_type="LongMemoryPathForbidden")
    return path


def read_long_memory(codepilot_home: Path, *, agent_name: str) -> str | None:
    """读取匹配当前 Agent 的长期记忆正文；文件头不注入模型。"""
    path = long_memory_path(codepilot_home)
    if not path.is_file():
        return None
    try:
        raw_content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    header, content = _split_frontmatter(raw_content)
    if not _matches_apply_to(header, agent_name):
        return None
    return content.strip() or None


def append_long_memory(codepilot_home: Path, content: str) -> tuple[Path, int]:
    """追加一条 Markdown bullet 形式的长期记忆。"""
    normalized = _normalize_memory_content(content)
    path = long_memory_path(codepilot_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"- {normalized}\n"
    needs_header = not path.exists() or not path.read_text(encoding="utf-8").strip()
    with path.open("a", encoding="utf-8") as file:
        if needs_header:
            file.write(DEFAULT_MEMORY_HEADER + "\n")
        file.write(entry)
    bytes_written = len(entry.encode("utf-8"))
    if needs_header:
        bytes_written += len((DEFAULT_MEMORY_HEADER + "\n").encode("utf-8"))
    return path, bytes_written


def _normalize_memory_content(content: str) -> str:
    normalized = "\n  ".join(line.strip() for line in str(content).splitlines() if line.strip()).strip()
    if not normalized:
        raise LongMemoryError("长期记忆内容不能为空。", error_type="LongMemoryContentEmpty")
    if len(normalized) > MAX_LONG_MEMORY_CONTENT_CHARS:
        raise LongMemoryError(
            f"单条长期记忆最多允许 {MAX_LONG_MEMORY_CONTENT_CHARS} 个字符。",
            error_type="LongMemoryContentTooLong",
        )
    return normalized


def _split_frontmatter(content: str) -> tuple[dict[str, object], str]:
    """解析最小 YAML frontmatter；格式缺失时返回空头信息。"""
    if not content.startswith("---\n"):
        return {}, content
    end_index = content.find("\n---", 4)
    if end_index == -1:
        return {}, content
    header_text = content[4:end_index].strip()
    body_start = end_index + len("\n---")
    if content[body_start : body_start + 1] == "\n":
        body_start += 1
    return _parse_frontmatter(header_text), content[body_start:]


def _parse_frontmatter(header_text: str) -> dict[str, object]:
    header: dict[str, object] = {}
    lines = header_text.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.strip()
        index += 1
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if key == "applyTo" and not value:
            values: list[str] = []
            while index < len(lines) and lines[index].startswith("  - "):
                item = _strip_quotes(lines[index][4:].strip())
                if item:
                    values.append(item)
                index += 1
            header[key] = values
            continue
        header[key] = _parse_scalar_or_inline_list(value)
    return header


def _parse_scalar_or_inline_list(value: str) -> object:
    if value.startswith("[") and value.endswith("]"):
        return [_strip_quotes(item.strip()) for item in value[1:-1].split(",") if item.strip()]
    return _strip_quotes(value)


def _strip_quotes(value: str) -> str:
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        return value[1:-1]
    return value


def _matches_apply_to(header: dict[str, object], agent_name: str) -> bool:
    raw_apply_to = header.get("applyTo")
    if isinstance(raw_apply_to, str):
        targets = [raw_apply_to]
    elif isinstance(raw_apply_to, list) and all(isinstance(item, str) for item in raw_apply_to):
        targets = raw_apply_to
    else:
        return False
    return "**" in targets or agent_name in targets
