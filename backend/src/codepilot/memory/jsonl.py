"""基于 JSONL 文件的会话记忆与流事件持久化实现。

该模块负责把运行期产生的领域事件和前端流事件追加写入按会话划分的
JSONL 文件，并在需要时按顺序回放，供会话恢复和 SSE 断线续传使用。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles

from codepilot.events import ApprovalEvent, DomainEvent, MessageCreatedEvent, SessionCompactedEvent, SessionLifecycleEvent, StreamEvent
from codepilot.logging import get_logger


class JsonlSessionMemory:
    """负责持久化和回放会话级领域事件的 JSONL 存储。"""

    def __init__(self, sessions_dir: Path) -> None:
        """初始化会话存储目录和日志器。"""
        self._sessions_dir = sessions_dir
        self._logger = get_logger("codepilot.memory.session")

    async def handle_domain_event(self, event: DomainEvent) -> None:
        """将单个领域事件转换为记录并追加写入当前会话文件。"""
        session_id = event.session_id
        if not session_id:
            return
        path = self._session_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = self._to_record(event)
        # JSONL 采用“一行一条记录”的追加写入方式，既便于顺序回放，也避免整文件重写。
        async with aiofiles.open(path, "a", encoding="utf-8") as file:
            await file.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def replay(self, session_id: str | None = None) -> dict[str, Any]:
        """回放指定会话的领域事件，并重建最新会话快照与消息列表。"""
        target = self._latest_session_file(session_id)
        if target is None or not target.exists():
            return {"session": None, "messages": [], "records": []}
        messages: list[dict[str, Any]] = []
        session_snapshot: dict[str, Any] | None = None
        records: list[dict[str, Any]] = []
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            records.append(record)
            if record["record_type"] == "message":
                messages.append(record["data"])
            if record["record_type"] == "session_compacted":
                messages = list(record["data"].get("messages") or [])
                session_snapshot = {
                    **(session_snapshot or {}),
                    "session_id": record["session_id"],
                    "created_at": record["created_at"],
                    "data": {
                        **((session_snapshot or {}).get("data") or {}),
                        "metadata": record["data"].get("metadata") or {},
                    },
                }
            if record["record_type"] in {"session_started", "session_status_changed", "session_finished", "session_failed"}:
                # 会话状态采用逐条覆盖的方式累积，最终得到最近一次生命周期快照。
                session_snapshot = {**(session_snapshot or {}), **record}
        return {"session": session_snapshot, "messages": messages, "records": records}

    def _to_record(self, event: DomainEvent) -> dict[str, Any]:
        """把不同类型的领域事件映射成统一可落盘的记录结构。"""
        if isinstance(event, MessageCreatedEvent):
            return {
                "record_type": "message",
                "session_id": event.session_id,
                "message_id": event.message.info.id,
                "created_at": event.created_at,
                "data": event.message.model_dump(),
            }
        if isinstance(event, ApprovalEvent):
            return {
                "record_type": "human_approval",
                "session_id": event.session_id,
                "approval_id": event.approval_id,
                "created_at": event.created_at,
                "data": {"status": event.status, **event.data},
            }
        if isinstance(event, SessionCompactedEvent):
            return {
                "record_type": "session_compacted",
                "session_id": event.session_id,
                "created_at": event.created_at,
                "data": event.data,
            }
        if isinstance(event, SessionLifecycleEvent):
            lifecycle_type = "session_status_changed"
            if event.status == "RUNNING":
                lifecycle_type = "session_started"
            if event.status in {"COMPLETED", "CANCELLED"}:
                lifecycle_type = "session_finished"
            if event.status == "FAILED":
                lifecycle_type = "session_failed"
            # 将通用生命周期事件收敛为更直观的记录类型，便于回放阶段识别会话阶段。
            return {
                "record_type": lifecycle_type,
                "session_id": event.session_id,
                "workspace_id": event.data.get("workspace_id"),
                "created_at": event.created_at,
                "data": event.data,
            }
        return {
            "record_type": event.event_type.value,
            "session_id": event.session_id,
            "created_at": event.created_at,
            "data": event.data,
        }

    def _session_file(self, session_id: str) -> Path:
        """生成当天会话记录文件的路径。"""
        return self._sessions_dir / f"{datetime.now().strftime('%Y-%m-%d')}-{session_id}.jsonl"

    def _latest_session_file(self, session_id: str | None = None) -> Path | None:
        """定位指定会话或全局最新的会话记录文件。"""
        if session_id:
            candidates = sorted(self._sessions_dir.glob(f"*-{session_id}.jsonl"))
            return candidates[-1] if candidates else None
        candidates = sorted(path for path in self._sessions_dir.glob("*.jsonl") if not path.name.endswith(".events.jsonl"))
        return candidates[-1] if candidates else None


class JsonlEventStore:
    """负责持久化和回放 SSE 流事件的 JSONL 存储。"""

    def __init__(self, sessions_dir: Path) -> None:
        """初始化事件存储目录和日志器。"""
        self._sessions_dir = sessions_dir
        self._logger = get_logger("codepilot.memory.events")

    async def append(self, event: StreamEvent) -> None:
        """将单个流事件追加写入所属会话的事件文件。"""
        if not event.session_id:
            return
        path = self._event_file(event.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 流事件按产生顺序直接追加，便于客户端按 seq 增量恢复。
        async with aiofiles.open(path, "a", encoding="utf-8") as file:
            await file.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")

    def replay(self, session_id: str | None = None, after_seq: int = 0) -> list[StreamEvent]:
        """回放指定序号之后的流事件，用于断线重连后的增量补发。"""
        target = self._latest_event_file(session_id)
        if target is None or not target.exists():
            return []
        events: list[StreamEvent] = []
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("seq", 0) > after_seq:
                events.append(StreamEvent.model_validate(payload))
        return events

    def latest_seq(self, session_id: str | None = None) -> int:
        """返回指定会话当前已持久化的最新事件序号。"""
        events = self.replay(session_id=session_id, after_seq=0)
        return events[-1].seq if events else 0

    def _event_file(self, session_id: str) -> Path:
        """生成当天流事件文件的路径。"""
        return self._sessions_dir / f"{datetime.now().strftime('%Y-%m-%d')}-{session_id}.events.jsonl"

    def _latest_event_file(self, session_id: str | None = None) -> Path | None:
        """定位指定会话或全局最新的流事件文件。"""
        if session_id:
            candidates = sorted(self._sessions_dir.glob(f"*-{session_id}.events.jsonl"))
            return candidates[-1] if candidates else None
        candidates = sorted(self._sessions_dir.glob("*.events.jsonl"))
        return candidates[-1] if candidates else None
