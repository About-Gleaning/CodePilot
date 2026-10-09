from __future__ import annotations

"""从已有工具记录提取成果，不解析自由文本路径或扫描工作目录。"""

from pathlib import Path
from urllib.parse import quote


def artifact_target(part: dict, root: Path) -> Path | None:
    keys = {"write_file": "file_path", "edit_file": "file_path", "write_plan": "plan_path"}
    key = keys.get(part.get("tool"))
    state = part.get("state") or {}
    output = state.get("output") or {}
    if not key or state.get("status") != "completed" or output.get("status") != "ok":
        return None
    value = output.get(key)
    if not isinstance(value, str):
        return None
    path = Path(value)
    root = root.resolve()
    if not path.is_absolute() or not path.is_relative_to(root):
        return None
    try:
        # 每个路径分量均禁止符号链接，读取时重新核实而非相信展示时的检查。
        if any(candidate.is_symlink() for candidate in (path, *path.parents) if candidate.is_relative_to(root)):
            return None
        if not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            return None
    except OSError:
        return None
    return path


def decorate_message(message: dict, *, run_id: str | None, agent_id: str, session_id: str, root: Path) -> dict:
    parts = []
    message_id = (message.get("info") or {}).get("id", "")
    for original in message.get("parts", []):
        part = dict(original)
        target = artifact_target(part, root)
        if target and message_id and part.get("call_id"):
            prefix = f"/api/agents/{quote(agent_id, safe='')}/sessions/{quote(session_id, safe='')}"
            part["artifact"] = {"name": target.name, "path": str(target.relative_to(root.resolve())),
                "url": f"{prefix}/artifacts/{quote(message_id, safe='')}/{quote(part['call_id'], safe='')}"}
        parts.append(part)
    return {**message, "info": {**message.get("info", {}), "run_id": run_id}, "parts": parts}
