from __future__ import annotations

"""定时会话按受控字节游标读取增量事件，不在轮询时重放全部历史。"""

import json
import re
from pathlib import Path


def read_events(directory: Path, session_id: str, cursor: str = "") -> dict:
    if not re.fullmatch(r"sess_[a-zA-Z0-9_-]+", session_id):
        raise ValueError("会话标识无效")
    if cursor:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}):(\d{1,16})", cursor)
        if not match:
            raise ValueError("事件游标无效")
        date, raw_offset = match.groups()
        offset = int(raw_offset)
        path = directory / f"{date}-{session_id}.events.jsonl"
    else:
        paths = sorted(directory.glob(f"*-{session_id}.events.jsonl")) if directory.exists() else []
        if not paths:
            return {"events": [], "cursor": "", "has_more": False}
        path, offset = paths[0], 0
        date = path.name[:10]
    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("事件文件不可访问")
    if not path.exists() or offset > path.stat().st_size:
        raise ValueError("事件文件已变化，请重新加载会话")
    events = []
    read_bytes = 0
    incomplete = False
    with path.open("rb") as file:
        file.seek(offset)
        for _ in range(500):
            line = file.readline(8 * 1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 8 * 1024 * 1024:
                raise ValueError("事件记录超过读取上限")
            if not line.endswith(b"\n"):
                incomplete = True
                break
            event = json.loads(line)
            offset = file.tell()
            read_bytes += len(line)
            if event.get("event_type") not in {"llm_delta", "llm_reasoning_delta"}:
                events.append(event)
            if read_bytes >= 1024 * 1024:
                break
    more = not incomplete and offset < path.stat().st_size
    # 跨午夜才查找下一日文件，普通轮询仅打开一个确定路径。
    from datetime import datetime
    if not more and date < datetime.now().strftime("%Y-%m-%d"):
        following = sorted(p for p in directory.glob(f"*-{session_id}.events.jsonl") if p.name > path.name)
        if following:
            date, offset, more = following[0].name[:10], 0, True
    return {"events": events, "cursor": f"{date}:{offset}", "has_more": more}
