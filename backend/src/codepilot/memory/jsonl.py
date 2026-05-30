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

from codepilot.events import (
    DomainEvent,
    HumanInteractionEvent,
    MessageCreatedEvent,
    SessionCompactedEvent,
    SessionLifecycleEvent,
    SessionMetaEvent,
    StreamEvent,
)
from codepilot.logging import get_logger


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
        records = self._load_session_records(session_id)
        if not records:
            return {"session": None, "messages": [], "records": []}
        messages: list[dict[str, Any]] = []
        session_meta = self._require_session_meta(records)
        session_data: dict[str, Any] = {
            "session_id": session_meta["session_id"],
            "title": session_meta["data"].get("title"),
            "workspace_id": session_meta["data"].get("workspace_id"),
            "workspace_path": session_meta["data"].get("workspace_path"),
            "created_at": session_meta.get("created_at"),
            "updated_at": session_meta.get("updated_at") or session_meta.get("created_at"),
            "metadata": {},
        }
        session_snapshot: dict[str, Any] | None = {
            "record_type": "session_meta",
            "session_id": session_meta["session_id"],
            "created_at": session_meta.get("created_at"),
            "data": session_data,
        }
        for record in records:
            if record["record_type"] == "message":
                messages.append(record["data"])
            if record["record_type"] == "human_interaction":
                self._apply_human_interaction(messages, record)
            if record["record_type"] == "session_compacted":
                messages = list(record["data"].get("messages") or [])
            if record["record_type"] in {"session_started", "session_status_changed", "session_finished", "session_failed"}:
                # 状态节点只保存运行态字段，回放时与首行 session_meta 合成完整 SessionState。
                session_data = {**session_data, **(record.get("data") or {})}
                session_snapshot = {**record, "data": session_data}
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
        if isinstance(event, SessionMetaEvent):
            return {
                "record_type": "session_meta",
                "session_id": event.session_id,
                "created_at": event.created_at,
                "updated_at": event.data.get("updated_at") or event.created_at,
                "data": event.data,
            }
        if isinstance(event, MessageCreatedEvent):
            return {
                "record_type": "message",
                "session_id": event.session_id,
                "message_id": event.message.info.id,
                "created_at": event.created_at,
                "data": event.message.model_dump(),
            }
        if isinstance(event, HumanInteractionEvent):
            return {
                "record_type": "human_interaction",
                "session_id": event.session_id,
                "interaction_id": event.interaction_id,
                "created_at": event.created_at,
                "data": event.data,
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
                if event.data.get("lifecycle_record_type") == "session_status_changed":
                    lifecycle_type = "session_status_changed"
            if event.status in {"COMPLETED", "CANCELLED"}:
                lifecycle_type = "session_finished"
            if event.status == "FAILED":
                lifecycle_type = "session_failed"
            # 将通用生命周期事件收敛为更直观的记录类型，便于回放阶段识别会话阶段。
            return {
                "record_type": lifecycle_type,
                "session_id": event.session_id,
                "created_at": event.created_at,
                "data": self._lifecycle_data(event),
            }
        return {
            "record_type": event.event_type.value,
            "session_id": event.session_id,
            "created_at": event.created_at,
            "data": event.data,
        }

    def _apply_human_interaction(self, messages: list[dict[str, Any]], record: dict[str, Any]) -> None:
        """回放人工交互记录；当前只有 question resolved 会补全工具状态。"""
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if data.get("kind") != "question" or data.get("status") != "resolved":
            return
        message_id = str(data.get("message_id") or "")
        call_id = str(data.get("call_id") or "")
        if not message_id or not call_id:
            return

        for message in messages:
            info = message.get("info") if isinstance(message.get("info"), dict) else {}
            if info.get("id") != message_id:
                continue
            self._complete_question_tool_part(message, data, call_id)
            return

    def _complete_question_tool_part(self, message: dict[str, Any], data: dict[str, Any], call_id: str) -> None:
        """根据 call_id 补齐 question 工具结果，并同步步骤完成原因。"""
        parts = message.get("parts")
        if not isinstance(parts, list):
            return
        matched = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool" and part.get("call_id") == call_id and part.get("tool") == "question":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                part["state"] = {
                    **state,
                    "status": "completed",
                    "output": data.get("tool_output") if isinstance(data.get("tool_output"), dict) else self._fallback_question_output(data),
                }
                matched = True
        if not matched:
            return
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "step-finish" and part.get("reason") == "tool_pending":
                part["reason"] = "tool_completed"
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        if info.get("role") == "assistant":
            info["finish"] = "tool_completed"

    def _fallback_question_output(self, data: dict[str, Any]) -> dict[str, Any]:
        """缺少完整工具输出时，用 interaction 结果生成可读的工具输出。"""
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        return {
            "status": "ok",
            "tool_name": "question",
            "question_id": data.get("interaction_id"),
            "answers": result.get("answers") if isinstance(result.get("answers"), dict) else {},
            "output": data.get("output") or "用户已回答 question 工具提出的问题。",
        }

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

    def _lifecycle_data(self, event: SessionLifecycleEvent) -> dict[str, Any]:
        """只持久化状态事件自身需要的字段，避免重复保存会话全局信息。"""
        data = event.data
        return {
            "status": event.status,
            "agent_name": data.get("agent_name"),
            "provider": data.get("provider"),
            "model": data.get("model"),
            "updated_at": data.get("updated_at") or event.created_at,
        }

    def _require_session_meta(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """新格式强制第一条记录为 session_meta，不再兼容旧 JSONL 布局。"""
        first = records[0]
        if first.get("record_type") != "session_meta":
            raise ValueError("session jsonl 第一条记录必须是 session_meta")
        data = first.get("data")
        if not isinstance(data, dict):
            raise ValueError("session_meta.data 必须是对象")
        return first

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

    def _build_session_summary(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        """从 JSONL 记录流中提取轻量摘要，避免前端加载完整消息体。"""
        if not records:
            return None
        session_data: dict[str, Any] = {}
        session_id = ""
        created_at = ""
        updated_at = ""
        status = ""
        message_count = 0
        preview = ""
        session_meta = self._require_session_meta(records)
        session_data.update(session_meta.get("data") or {})
        created_at = str(session_meta.get("created_at") or "")
        updated_at = str(session_meta.get("updated_at") or created_at)
        for record in records:
            session_id = str(record.get("session_id") or session_id)
            record_created_at = str(record.get("created_at") or "")
            if record_created_at and record.get("record_type") != "session_meta":
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
        if not session_id:
            return None
        return {
            "session_id": session_id,
            "title": session_data.get("title"),
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
