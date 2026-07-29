from __future__ import annotations

"""多 Agent 控制面的低频状态存储。

这些文件只保存资源标识、状态和摘要，不保存 Prompt、附件或 Tool 结果。
"""

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.events import StreamEvent
from codepilot.session.state import RunState


class RuntimeStoreCorrupt(ValueError):
    """运行日志无法安全恢复，必须阻止新的副作用执行。"""


class RuntimeStateStore:
    def __init__(self, workspace_dir: Path) -> None:
        self.path = Path(workspace_dir) / "agent-runtimes.json"

    async def read(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._read_sync)

    async def write(self, payload: dict[str, Any]) -> None:
        await asyncio.to_thread(_atomic_json_write, self.path, payload)

    def _read_sync(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeStoreCorrupt("Agent 运行状态文件损坏") from exc
        if not isinstance(payload, dict):
            raise RuntimeStoreCorrupt("Agent 运行状态文件格式无效")
        return payload


class JsonlRunStore:
    def __init__(self, workspace_dir: Path) -> None:
        self.path = Path(workspace_dir) / "agent-runs.jsonl"
        self._lock = asyncio.Lock()

    async def append(self, state: RunState) -> None:
        record = {
            "record_type": "run_state",
            "run": state.model_dump(mode="json"),
        }
        async with self._lock:
            await asyncio.to_thread(_append_json_line, self.path, record)

    async def recover(self) -> tuple[dict[tuple[str, str, str], RunState], dict[str, tuple[str, tuple[str, str, str]]]]:
        async with self._lock:
            records = await asyncio.to_thread(_read_jsonl_with_tail_repair, self.path)
        runs: dict[tuple[str, str, str], RunState] = {}
        idempotency: dict[str, tuple[str, tuple[str, str, str]]] = {}
        for record in records:
            if record.get("record_type") != "run_state":
                continue
            try:
                run = RunState.model_validate(record["run"])
            except Exception as exc:  # noqa: BLE001
                raise RuntimeStoreCorrupt("Run 日志包含无效记录") from exc
            key = (run.ref.agent_id, run.ref.session_id, run.ref.run_id)
            previous = runs.get(key)
            if previous and previous.client_request_id != run.client_request_id:
                raise RuntimeStoreCorrupt("Run 日志资源标识冲突")
            runs[key] = run
            existing = idempotency.get(run.client_request_id)
            entry = (run.request_fingerprint, key)
            if existing and existing != entry:
                raise RuntimeStoreCorrupt("Run 日志幂等键冲突")
            idempotency[run.client_request_id] = entry
        return runs, idempotency


class RuntimeControlEventStore:
    def __init__(self, workspace_dir: Path) -> None:
        self.path = Path(workspace_dir) / "agent-runtime-events.jsonl"
        self._lock = asyncio.Lock()
        self._seq = 0

    async def recover(self) -> None:
        async with self._lock:
            records = await asyncio.to_thread(_read_jsonl_with_tail_repair, self.path)
            self._seq = max((int(item.get("control_seq") or 0) for item in records), default=0)

    @property
    def current_seq(self) -> int:
        return self._seq

    async def append(self, event: StreamEvent) -> None:
        async with self._lock:
            self._seq = max(self._seq + 1, event.seq)
            record = {
                "record_type": "runtime_control_event",
                "control_seq": self._seq,
                "event": event.model_dump(mode="json"),
            }
            await asyncio.to_thread(_append_json_line, self.path, record)

    async def replay(self, cursor: str | None) -> list[tuple[int, str, StreamEvent]]:
        after_seq = decode_runtime_cursor(cursor) if cursor else 0
        async with self._lock:
            records = await asyncio.to_thread(_read_jsonl_with_tail_repair, self.path)
            current = self._seq
        if after_seq > current:
            raise ValueError("cursor 指向未来事件")
        events: list[tuple[int, str, StreamEvent]] = []
        for record in records:
            seq = int(record.get("control_seq") or 0)
            if seq <= after_seq or record.get("record_type") != "runtime_control_event":
                continue
            events.append((seq, encode_runtime_cursor(seq), StreamEvent.model_validate(record["event"])))
        return events


def encode_runtime_cursor(seq: int) -> str:
    raw = json.dumps({"v": 1, "seq": seq}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_runtime_cursor(value: str) -> int:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("v") != 1 or not isinstance(payload.get("seq"), int) or payload["seq"] < 0:
            raise ValueError
        return int(payload["seq"])
    except Exception as exc:  # noqa: BLE001
        raise ValueError("运行时 cursor 无效") from exc


def _append_json_line(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(path, 0o600)


def _read_jsonl_with_tail_repair(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    valid_bytes = bytearray()
    for index, line in enumerate(lines):
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            is_last = index == len(lines) - 1 and not line.endswith(b"\n")
            if not is_last:
                raise RuntimeStoreCorrupt("运行日志中间记录损坏") from exc
            _repair_truncated_tail(path, bytes(valid_bytes))
            break
        if not isinstance(record, dict):
            raise RuntimeStoreCorrupt("运行日志记录必须是对象")
        records.append(record)
        valid_bytes.extend(line)
    return records


def _repair_truncated_tail(path: Path, valid_prefix: bytes) -> None:
    corrupt_dir = path.parent / ".corrupt"
    corrupt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    archived = corrupt_dir / f"{path.name}.{uuid4().hex}.corrupt"
    os.replace(path, archived)
    os.chmod(archived, 0o600)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(valid_prefix)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
