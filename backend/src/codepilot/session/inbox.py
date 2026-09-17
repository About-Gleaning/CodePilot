from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from codepilot.events import StreamEvent
from codepilot.events.definitions import MessageSubmissionEvent
from codepilot.session.message import Message
from codepilot.session.state import SessionStatus
from codepilot.utils import utc_now_iso


class SessionInbox:
    """仅由主 Agent 消费的输入邮箱；接收与收尾共享短临界区。"""

    def __init__(self, submissions: list[dict[str, Any]] | None = None) -> None:
        self.lock = asyncio.Lock()
        self.pending = {
            item["message_id"]: dict(item)
            for item in submissions or [] if item["status"] == "pending"
        }
        self.batch: list[str] = []
        self.sequence = max((item["sequence"] for item in submissions or []), default=0)
        self.accepting = True

    async def receive(self, session: Any, event_bus: Any, build_message: Any, receipt: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            if not self.accepting or session.status != SessionStatus.RUNNING:
                raise ValueError("当前执行已结束、正在停止或等待人工处理，请刷新后再发送")
            if len(self.pending) >= 20:
                raise ValueError("待纳入消息已达 20 条，请等待处理后再发送")
            message = build_message()
            item = {
                **receipt, "message_id": message.info.id, "message": message.model_dump(),
                "sequence": self.sequence + 1, "status": "pending",
            }
            await event_bus.publish_domain_event(MessageSubmissionEvent(
                session_id=session.session_id, created_at=utc_now_iso(), data={"submissions": [item]},
            ))
            self.sequence += 1
            self.pending[message.info.id] = item
            return dict(item)

    async def prepare(self, session: Any) -> None:
        async with self.lock:
            self.batch = list(self.pending)
            if not self.batch:
                return
            existing = {message.info.id for message in session.messages}
            session.messages.extend(
                Message.model_validate(self.pending[key]["message"])
                for key in self.batch if key not in existing
            )

    async def include(self, session: Any, runtime: Any, iteration: int) -> None:
        async with self.lock:
            if not self.batch:
                return
            selected = set(self.batch)
            anchors: dict[str, str | None] = {}
            following = None
            # 反向扫描一次，恢复时将旧待处理消息放回后续人工输入之前。
            for message in reversed(session.messages):
                if message.info.id in selected:
                    anchors[message.info.id] = following
                else:
                    following = message.info.id
            items = [{
                **self.pending[key], "status": "included",
                "included_run_id": runtime.run_ref.run_id if runtime.run_ref else "",
                "iteration": iteration,
                "before_message_id": anchors.get(key),
            } for key in self.batch]
            await runtime.event_bus.publish_domain_event(MessageSubmissionEvent(
                session_id=session.session_id, created_at=utc_now_iso(), data={"submissions": items},
            ))
            for key in self.batch:
                self.pending.pop(key)
            self.batch = []
        await runtime.event_bus.publish_stream_event(StreamEvent(
            event_type="message_submission", session_id=session.session_id,
            created_at=utc_now_iso(), data={"submissions": [public_submission(item) for item in items]},
        ))

    async def complete(self, session: Any) -> bool:
        async with self.lock:
            if session.status != SessionStatus.RUNNING:
                self.accepting = False
                return False
            if self.pending:
                return True
            self.accepting = False
            session.status = SessionStatus.COMPLETED
            return False

    async def close(self) -> None:
        async with self.lock:
            self.accepting = False

    async def wait_for_human(self, session: Any) -> None:
        async with self.lock:
            session.status = SessionStatus.WAITING_HUMAN


def public_submission(item: dict[str, Any]) -> dict[str, Any]:
    result = {key: item[key] for key in (
        "mode", "message_id", "message", "sequence", "status", "included_run_id", "iteration",
    ) if key in item}
    if "message" in result:
        result["message"] = deepcopy(result["message"])
        for part in result["message"].get("parts", []):
            if part.get("type") == "file":
                # 浏览器通过受控预览 URL 读取附件，不需要运行机器的文件定位。
                part.pop("source", None)
                part.get("metadata", {}).pop("local_path", None)
    return result
