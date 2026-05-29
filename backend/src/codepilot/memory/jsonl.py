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
        records = self._load_session_records(session_id)
        if not records:
            return {"session": None, "messages": [], "records": []}
        messages: list[dict[str, Any]] = []
        session_snapshot: dict[str, Any] | None = None
        for record in records:
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

    def list_sessions(self) -> list[dict[str, Any]]:
        """扫描本地会话文件，返回可供前端展示的历史会话摘要。"""
        summaries: list[dict[str, Any]] = []
        for session_id in self._session_ids():
            summary = self._build_session_summary(self._load_session_records(session_id))
            if summary is not None:
                summaries.append(summary)
        return sorted(summaries, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

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
        """生成会话记录文件路径；同一 session 后续写入固定复用既有文件。"""
        existing = self._session_files_for_id(session_id)
        if existing:
            return existing[0]
        return self._sessions_dir / f"{datetime.now().strftime('%Y-%m-%d')}-{session_id}.jsonl"

    def _session_files(self) -> list[Path]:
        """返回所有领域事件会话文件，排除 SSE 事件文件。"""
        return sorted(path for path in self._sessions_dir.glob("*.jsonl") if not path.name.endswith(".events.jsonl"))

    def _session_files_for_id(self, session_id: str) -> list[Path]:
        """按文件名后缀精确匹配 session_id，避免把外部输入解释为 glob 模式。"""
        suffix = f"-{session_id}.jsonl"
        return [path for path in self._session_files() if path.name.endswith(suffix)]

    def _session_ids(self) -> list[str]:
        """从文件名中提取唯一 session_id，避免历史列表按文件重复展示。"""
        session_ids: set[str] = set()
        for path in self._session_files():
            stem = path.name.removesuffix(".jsonl")
            if len(stem) > 11 and stem[4:5] == "-" and stem[7:8] == "-" and stem[10:11] == "-":
                session_ids.add(stem[11:])
        return sorted(session_ids)

    def _load_session_records(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """读取目标会话记录；旧版本跨天产生的多个文件会按顺序合并。"""
        paths = self._session_files_for_id(session_id) if session_id else self._latest_session_group()
        records: list[dict[str, Any]] = []
        for path in paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                records.append(json.loads(line))
        return records

    def _latest_session_group(self) -> list[Path]:
        """定位全局最新会话对应的完整文件组，用于无 session_id 的初始化回放。"""
        latest_file = self._latest_session_file()
        if latest_file is None:
            return []
        records = self._load_records_from_path(latest_file)
        session_id = ""
        for record in records:
            session_id = str(record.get("session_id") or session_id)
        return self._session_files_for_id(session_id) if session_id else [latest_file]

    def _load_records_from_path(self, path: Path) -> list[dict[str, Any]]:
        """读取单个 JSONL 文件，供定位最新会话时复用。"""
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def _latest_session_file(self, session_id: str | None = None) -> Path | None:
        """定位指定会话或全局最新的会话记录文件。"""
        if session_id:
            candidates = self._session_files_for_id(session_id)
            return candidates[-1] if candidates else None
        candidates = self._session_files()
        return max(candidates, key=self._session_file_updated_at) if candidates else None

    def _session_file_updated_at(self, path: Path) -> str:
        """提取文件内最后一次记录时间，避免复用旧文件名后最新会话判断失真。"""
        updated_at = ""
        for record in self._load_records_from_path(path):
            record_created_at = str(record.get("created_at") or "")
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            updated_at = str(data.get("updated_at") or record_created_at or updated_at)
        return updated_at

    def _build_session_summary(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        """从 JSONL 记录流中提取轻量摘要，避免前端加载完整消息体。"""
        session_data: dict[str, Any] = {}
        session_id = ""
        created_at = ""
        updated_at = ""
        status = ""
        message_count = 0
        preview = ""
        for record in records:
            session_id = str(record.get("session_id") or session_id)
            record_created_at = str(record.get("created_at") or "")
            if not created_at and record_created_at:
                created_at = record_created_at
            if record_created_at:
                updated_at = record_created_at
            if record.get("record_type") == "message":
                message_count += 1
                if not preview:
                    preview = self._message_preview(record.get("data"))
            if record.get("record_type") == "session_compacted":
                messages = record.get("data", {}).get("messages") or []
                message_count = len(messages)
                if not preview:
                    preview = self._first_message_preview(messages)
            if record.get("record_type") in {"session_started", "session_status_changed", "session_finished", "session_failed"}:
                session_data = {**session_data, **(record.get("data") or {})}
                status = str(session_data.get("status") or status)
                updated_at = str(session_data.get("updated_at") or updated_at)
                created_at = str(session_data.get("created_at") or created_at)
        if not session_id:
            return None
        return {
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": updated_at or created_at,
            "status": status or session_data.get("status") or "UNKNOWN",
            "agent_name": session_data.get("agent_name") or "",
            "provider": session_data.get("provider"),
            "model": session_data.get("model"),
            "message_count": message_count,
            "preview": self._truncate_preview(preview),
        }

    def _first_message_preview(self, messages: list[Any]) -> str:
        """优先取第一条用户消息文本，作为历史会话列表中的摘要。"""
        for message in messages:
            preview = self._message_preview(message)
            if preview:
                return preview
        return ""

    def _message_preview(self, message: Any) -> str:
        """从持久化消息字典中提取可读文本摘要。"""
        if not isinstance(message, dict):
            return ""
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        if info.get("role") != "user":
            return ""
        texts = [
            str(part.get("text") or "")
            for part in message.get("parts") or []
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        return " ".join(text.strip() for text in texts if text.strip())

    def _truncate_preview(self, value: str, limit: int = 80) -> str:
        """限制摘要长度，避免长输入撑开侧边栏。"""
        normalized = " ".join(value.split())
        return normalized if len(normalized) <= limit else f"{normalized[:limit]}..."


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
