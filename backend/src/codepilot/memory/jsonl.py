"""基于 JSONL 文件的会话记忆与流事件持久化实现。

该模块负责把运行期产生的领域事件和前端流事件追加写入按会话划分的
JSONL 文件，并在需要时按顺序回放，供会话恢复和 SSE 断线续传使用。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles

from codepilot.events import DomainEvent, SessionMetaEvent, StreamEvent
from codepilot.logging import get_logger
from codepilot.memory.projections import build_session_summary, replay_records
from codepilot.memory.records import domain_event_to_record


class JsonlSessionMemory:
    """负责持久化和回放会话级领域事件的 JSONL 存储。"""

    def __init__(self, sessions_dir: Path) -> None:
        """初始化会话存储目录和日志器。"""
        self._sessions_dir = sessions_dir
        self._logger = get_logger("codepilot.memory.session")
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def handle_domain_event(self, event: DomainEvent) -> None:
        """将单个领域事件转换为记录并追加写入当前会话文件。"""
        session_id = event.session_id
        if not session_id:
            return
        async with self._lock_for_session(session_id):
            path = self._session_file(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(event, SessionMetaEvent):
                await self._upsert_session_meta(path, self._to_record(event))
                return
            record = self._to_record(event)
            # JSONL 采用“一行一条记录”的追加写入方式，既便于顺序回放，也避免整文件重写。
            async with aiofiles.open(path, "a", encoding="utf-8") as file:
                await file.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def replay(self, session_id: str | None = None) -> dict[str, Any]:
        """回放指定会话的领域事件，并重建最新会话快照与消息列表。"""
        return replay_records(self._load_session_records(session_id))

    def list_sessions(self) -> list[dict[str, Any]]:
        """扫描本地会话文件，返回可供前端展示的历史会话摘要。"""
        summaries: list[dict[str, Any]] = []
        for session_id in self._session_ids():
            summary = build_session_summary(self._load_session_records(session_id))
            if summary is not None:
                summaries.append(summary)
        return sorted(summaries, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    def _to_record(self, event: DomainEvent) -> dict[str, Any]:
        """把不同类型的领域事件映射成统一可落盘的记录结构。"""
        return domain_event_to_record(event)

    async def _upsert_session_meta(self, path: Path, record: dict[str, Any]) -> None:
        """创建或更新首行 session_meta，避免全局展示信息散落在状态节点中。"""
        if not path.exists():
            async with aiofiles.open(path, "a", encoding="utf-8") as file:
                await file.write(json.dumps(record, ensure_ascii=False) + "\n")
            return

        records = self._load_records_from_path(path)
        if not records:
            records = [record]
        elif records[0].get("record_type") != "session_meta":
            raise ValueError("session jsonl 第一条记录必须是 session_meta")
        else:
            existing_data = records[0].get("data") if isinstance(records[0].get("data"), dict) else {}
            records[0] = {
                **records[0],
                "updated_at": record.get("updated_at") or record.get("created_at") or records[0].get("updated_at"),
                "data": {**existing_data, **(record.get("data") or {})},
            }

        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _lock_for_session(self, session_id: str) -> asyncio.Lock:
        """按 session 维度串行化写入，避免同一 JSONL 文件并发读改写。"""
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

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
            updated_at = str(record.get("updated_at") or data.get("updated_at") or record_created_at or updated_at)
        return updated_at


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
