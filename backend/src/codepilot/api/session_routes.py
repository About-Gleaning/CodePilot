from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from codepilot.events import StreamEvent
from codepilot.gateway import GatewayInput, UploadedAttachmentInput
from codepilot.gateway import GatewayInputType
from codepilot.gateway.gateway_input import MAX_USER_CONTENT_CHARS
from codepilot.session.state import RunRef
from codepilot.session.agent_runtime import RuntimeConflict
from codepilot.session.inbox import public_submission
from codepilot.utils import utc_now_iso


class LoadSessionRequest(BaseModel):
    session_id: str


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    expected_run_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    content: str = Field(min_length=1, max_length=MAX_USER_CONTENT_CHARS)
    provider: str | None = None
    model: str | None = None
    thinking_value: str | None = Field(default=None, max_length=64)
    user_metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[UploadedAttachmentInput] = Field(default_factory=list, max_length=4)
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("user_metadata")
    @classmethod
    def validate_user_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {
            "user_id", "agent_id", "agent_name", "run_id", "revision_id",
            "source", "schedule", "schedule_task_id", "schedule_run_id", "schedule_task_name",
        }
        if reserved.intersection(value):
            raise ValueError("user_metadata 包含服务端保留字段")
        if len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)) > MAX_USER_CONTENT_CHARS:
            raise ValueError("user_metadata 内容过大")
        return value


class InteractionReplyRequest(BaseModel):
    type: GatewayInputType
    approved: bool | None = None
    answers: dict[str, Any] | None = None
    comment: str | None = Field(default=None, max_length=2_000)


def register_session_routes(router: APIRouter, app_state: Any) -> None:
    legacy_by_user: dict[str, LegacySessionAdapter] = {}

    def legacy(request: Request) -> "LegacySessionAdapter":
        user_id = _user_id(request)
        return legacy_by_user.setdefault(user_id, LegacySessionAdapter(app_state, user_id))
    @router.get("/agent-runtimes")
    async def get_agent_runtimes(request: Request) -> JSONResponse:
        return JSONResponse(await app_state.agent_runtime.get_runtime_overview(_user_id(request)))

    @router.get("/agent-sessions/recent")
    async def get_recent_agent_sessions(request: Request) -> JSONResponse:
        sessions = app_state.agent_runtime.list_recent_sessions(_user_id(request))
        return JSONResponse({"sessions": sessions}, headers={"Cache-Control": "no-store"})

    @router.get("/agent-runtimes/stream")
    async def get_agent_runtime_stream(request: Request, cursor: str | None = None) -> StreamingResponse:
        user_id = _user_id(request)
        subscription = app_state.agent_runtime.create_runtime_subscription(user_id)
        try:
            replay = await app_state.agent_runtime.replay_runtime_events(user_id, cursor)
        except RuntimeConflict as exc:
            app_state.agent_runtime.remove_runtime_subscription(user_id, subscription)
            raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

        async def event_generator() -> AsyncIterator[str]:
            try:
                for _, event_cursor, event in replay:
                    yield _to_control_sse(event, event_cursor)
                while not await request.is_disconnected():
                    if subscription.resync_required.is_set():
                        yield "event: stream_reset_required\ndata: {\"resync_required\":true}\n\n"
                        break
                    try:
                        event = await asyncio.wait_for(
                            subscription.queue.get(),
                            timeout=app_state.settings.sse.heartbeat_seconds,
                        )
                    except TimeoutError:
                        yield _to_sse_comment(f"heartbeat {utc_now_iso()}")
                        continue
                    event_cursor = app_state.agent_runtime.runtime_cursor_for_seq(event.seq)
                    yield _to_control_sse(event, event_cursor)
            finally:
                app_state.agent_runtime.remove_runtime_subscription(user_id, subscription)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.get("/agents/{agent_id}/runtime")
    async def get_agent_runtime(agent_id: str, request: Request) -> JSONResponse:
        try:
            return JSONResponse(app_state.agent_runtime.get_agent_state(_user_id(request), agent_id).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc

    @router.post("/agents/{agent_id}/start")
    async def start_agent(agent_id: str, request: Request) -> JSONResponse:
        try:
            return JSONResponse((await app_state.agent_runtime.start_agent(_user_id(request), agent_id)).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc), "issues": exc.issues}) from exc

    @router.post("/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str, request: Request) -> JSONResponse:
        try:
            return JSONResponse((await app_state.agent_runtime.stop_agent(_user_id(request), agent_id)).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc

    @router.get("/agents/{agent_id}/sessions")
    async def get_agent_sessions(agent_id: str, request: Request) -> JSONResponse:
        try:
            sessions = app_state.agent_runtime.list_sessions(_user_id(request), agent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc
        return JSONResponse({"sessions": sessions})

    @router.post("/agents/{agent_id}/runs")
    async def start_run(agent_id: str, payload: StartRunRequest, http_request: Request) -> JSONResponse:
        metadata = {"user_metadata": payload.user_metadata}
        if payload.thinking_value:
            metadata["thinking_value"] = payload.thinking_value
        request = GatewayInput(type=GatewayInputType.USER_MESSAGE, session_id=payload.session_id, content=payload.content, agent_name="runtime", provider=payload.provider, model=payload.model, metadata=metadata, attachments=payload.attachments)
        try:
            run = await app_state.agent_runtime.submit_message(
                _user_id(http_request), agent_id, request, payload.session_id,
                payload.client_request_id, payload.expected_run_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_or_session_not_found"}) from exc
        except RuntimeConflict as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            raise HTTPException(
                status_code=exc.status,
                detail={"code": exc.code, "message": str(exc), "issues": exc.issues},
                headers=headers,
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_run_request", "message": str(exc)}) from exc
        return JSONResponse(run)

    @router.get("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}")
    async def get_run(agent_id: str, session_id: str, run_id: str, request: Request) -> JSONResponse:
        try:
            return JSONResponse(app_state.agent_runtime.get_run_state(RunRef(user_id=_user_id(request), agent_id=agent_id, session_id=session_id, run_id=run_id)).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "run_not_found"}) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc

    @router.post("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel")
    async def cancel_run(agent_id: str, session_id: str, run_id: str, request: Request) -> JSONResponse:
        try:
            run = await app_state.agent_runtime.cancel_run(RunRef(user_id=_user_id(request), agent_id=agent_id, session_id=session_id, run_id=run_id))
            return JSONResponse(run.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "run_not_found"}) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc

    @router.post("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/interactions/{interaction_id}")
    async def reply_interaction(agent_id: str, session_id: str, run_id: str, interaction_id: str, payload: InteractionReplyRequest, request: Request) -> JSONResponse:
        if payload.type not in {GatewayInputType.HUMAN_REPLY, GatewayInputType.QUESTION_REPLY, GatewayInputType.QUESTION_DECLINE}:
            raise HTTPException(status_code=422, detail={"code": "invalid_interaction_type"})
        gateway_request = GatewayInput(type=payload.type, approval_id=interaction_id if payload.type == GatewayInputType.HUMAN_REPLY else None, question_id=interaction_id if payload.type != GatewayInputType.HUMAN_REPLY else None, approved=payload.approved, answers=payload.answers, comment=payload.comment)
        try:
            run = await app_state.agent_runtime.reply_interaction(RunRef(user_id=_user_id(request), agent_id=agent_id, session_id=session_id, run_id=run_id), interaction_id, gateway_request)
            return JSONResponse(run.model_dump())
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=409, detail={"code": getattr(exc, "code", "interaction_conflict"), "message": str(exc)}) from exc

    @router.get("/agents/{agent_id}/sessions/{session_id}/replay")
    async def get_agent_session_replay(agent_id: str, session_id: str, request: Request) -> JSONResponse:
        try:
            user_id = _user_id(request)
            # 先固定已持久化事件边界；其后的事件由 SSE 重放，重复部分由 event_id 去重。
            events = _event_replay(app_state.event_store, user_id, session_id, 0)
            latest_event_seq = events[-1].seq if events else 0
            replay = await app_state.agent_runtime.validate_session_owner(user_id, agent_id, session_id)
            replay["latest_event_seq"] = latest_event_seq
            replay["runtime"] = await app_state.agent_runtime.get_session_runtime_snapshot(
                user_id,
                agent_id,
                session_id,
                replay,
            )
            from codepilot.session.workbench import decorate_message
            ownership = {record.get("message_id"): record.get("run_id") for record in replay.get("records", []) if record.get("record_type") == "message"}
            data = ((replay.get("session") or {}).get("data") or {})
            root = Path(data.get("workspace_path") or app_state.workspace.workspace_path)
            replay["messages"] = [decorate_message(message, run_id=ownership.get(message.get("info", {}).get("id")),
                agent_id=agent_id, session_id=session_id, root=root) for message in replay.get("messages", [])[-1000:]]
            replay["workbench_events"] = [event.model_dump() for event in events if event.event_type in {
                "loop_started", "loop_iteration_started", "loop_finished", "tool_call_started", "tool_call_finished", "tool_call_failed",
            }][-240:]
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": str(exc)}) from exc
        return JSONResponse(_safe_replay(replay))

    @router.get("/agents/{agent_id}/sessions/{session_id}/artifacts/{message_id}/{call_id}")
    async def get_session_artifact(agent_id: str, session_id: str, message_id: str, call_id: str, request: Request):
        from codepilot.session.workbench import artifact_target
        from fastapi.responses import FileResponse
        try:
            replay = await app_state.agent_runtime.validate_session_owner(_user_id(request), agent_id, session_id)
            root = Path(replay["session"]["data"]["workspace_path"])
            message = next(item for item in replay["messages"] if item["info"]["id"] == message_id)
            part = next(item for item in message["parts"] if item.get("call_id") == call_id)
            target = artifact_target(part, root)
            if target is None:
                raise ValueError("不可读取")
            return FileResponse(target, filename=target.name, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
        except (KeyError, StopIteration, ValueError, RuntimeConflict, OSError):
            raise HTTPException(404, detail={"code": "artifact_unavailable", "message": "成果文件不存在或不允许访问。"})

    @router.get("/agents/{agent_id}/sessions/{session_id}/stream")
    async def get_agent_session_stream(agent_id: str, session_id: str, request: Request, after_seq: int = 0) -> StreamingResponse:
        if after_seq < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_after_seq"})
        try:
            user_id = _user_id(request)
            replay = await app_state.agent_runtime.validate_session_owner(user_id, agent_id, session_id)
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": str(exc)}) from exc
        root = Path(((replay.get("session") or {}).get("data") or {}).get("workspace_path") or app_state.workspace.workspace_path)
        return _stream_response(request, app_state, user_id, session_id, after_seq, root=root, agent_id=agent_id)

    @router.post("/session/input")
    async def post_session_input(payload: GatewayInput, request: Request) -> JSONResponse:
        reserved = {
            "user_id", "agent_id", "agent_name", "run_id", "revision_id",
            "source", "schedule", "schedule_task_id", "schedule_run_id", "schedule_task_name",
        }
        if payload.type == GatewayInputType.USER_MESSAGE and reserved.intersection(payload.metadata):
            raise HTTPException(status_code=422, detail={"code": "reserved_metadata_forbidden"})
        if "_message_id" in payload.metadata:
            raise HTTPException(status_code=422, detail={"code": "reserved_metadata_forbidden"})
        try:
            return JSONResponse(await legacy(request).handle_input(payload))
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(
                status_code=getattr(exc, "status", 409),
                detail={"code": getattr(exc, "code", "legacy_session_conflict"), "message": str(exc)},
            ) from exc

    @router.get("/session/status")
    async def get_session_status(request: Request) -> JSONResponse:
        return JSONResponse(legacy(request).status())

    @router.get("/session/replay")
    async def get_session_replay(request: Request) -> JSONResponse:
        user_id = _user_id(request)
        session_id = legacy(request).session_id
        if not session_id:
            return JSONResponse({"session": None, "messages": [], "records": []})
        return JSONResponse(await _memory_replay(app_state.session_memory, user_id, session_id))

    @router.get("/sessions")
    async def get_sessions(request: Request) -> JSONResponse:
        return JSONResponse({"sessions": _memory_list(app_state.session_memory, _user_id(request))})

    @router.post("/session/load")
    async def post_session_load(payload: LoadSessionRequest, request: Request) -> JSONResponse:
        try:
            result = await legacy(request).load(payload.session_id)
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=409, detail={"code": "legacy_session_conflict", "message": str(exc)}) from exc
        return JSONResponse(result)

    @router.get("/session/stream")
    async def get_session_stream(request: Request, after_seq: int = 0) -> StreamingResponse:
        if after_seq < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_after_seq"})
        async def event_generator() -> AsyncIterator[str]:
            user_id = _user_id(request)
            adapter = legacy(request)
            active_session_id = adapter.session_id
            subscription = _create_subscription(app_state.event_bus, user_id=user_id)
            if active_session_id and app_state.settings.sse.replay_on_connect:
                for event in _event_replay(app_state.event_store, user_id, active_session_id, after_seq):
                    yield _to_sse(event)
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(subscription.queue.get(), timeout=app_state.settings.sse.heartbeat_seconds)
                        if active_session_id and event.session_id != active_session_id:
                            if event.event_type == "session_started" and event.session_id == adapter.session_id:
                                active_session_id = event.session_id
                            else:
                                continue
                        if not active_session_id:
                            if event.event_type != "session_started" or not event.session_id:
                                continue
                            active_session_id = event.session_id
                        yield _to_sse(event)
                    except TimeoutError:
                        yield _to_sse_comment(f"heartbeat {utc_now_iso()}")
            finally:
                _remove_subscription(app_state.event_bus, subscription)

        return StreamingResponse(event_generator(), media_type="text/event-stream")


def _to_sse(event: StreamEvent) -> str:
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {json.dumps(event.model_dump(), ensure_ascii=False)}\n\n"


def _safe_replay(replay: dict[str, Any]) -> dict[str, Any]:
    """只返回 Agent Studio 使用的会话投影，隐藏内部记录和本机路径。"""
    session = replay.get("session")
    safe_session: dict[str, Any] | None = None
    if isinstance(session, dict):
        data = session.get("data") if isinstance(session.get("data"), dict) else {}
        safe_data = {
            key: data.get(key)
            for key in (
                "session_id",
                "agent_id",
                "agent_name",
                "title",
                "provider",
                "model",
                "status",
                "stop_reason",
                "created_at",
                "updated_at",
                "source",
                "schedule_task_id",
                "schedule_run_id",
                "schedule_task_name",
            )
            if data.get(key) is not None
        }
        safe_session = {
            "record_type": session.get("record_type"),
            "session_id": session.get("session_id"),
            "created_at": session.get("created_at"),
            "data": safe_data,
        }
    return {
        "session": safe_session,
        "messages": [
            _redact_local_paths(message)
            for message in replay.get("messages", [])
            if isinstance(message, dict)
        ],
        "latest_event_seq": int(replay.get("latest_event_seq") or 0),
        "submissions": [
            _redact_local_paths(public_submission(item)) for item in replay.get("submissions", [])[-240:]
        ],
        "runtime": _redact_local_paths(replay.get("runtime")) if isinstance(replay.get("runtime"), dict) else {},
        "workbench_events": _redact_local_paths(replay.get("workbench_events", [])),
    }


def _redact_local_paths(value: Any, *, key: str = "") -> Any:
    """保留页面所需消息结构，但隐藏运行目录字段和用户主目录。"""
    if isinstance(value, dict):
        return {item_key: _redact_local_paths(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_local_paths(item) for item in value]
    if not isinstance(value, str):
        return value
    if key in {"workspace_path", "codepilot_home", "local_path", "cwd", "root"}:
        return "<LOCAL_PATH>"
    home = str(Path.home())
    return value.replace(home, "<USER_HOME>") if home else value


def _to_control_sse(event: StreamEvent, cursor: str) -> str:
    return f"id: {cursor}\nevent: {event.event_type}\ndata: {json.dumps(event.model_dump(), ensure_ascii=False)}\n\n"


def _stream_response(request: Request, app_state: Any, user_id: str, session_id: str, after_seq: int, *, root: Path | None = None, agent_id: str = "") -> StreamingResponse:
    """按明确 Session 建立 SSE；队列被总线移除时客户端自行重连回放。"""
    def encode(event):
        if root is not None and isinstance(event.data.get("message"), dict):
            from codepilot.session.workbench import decorate_message
            data = {**event.data, "message": decorate_message(event.data["message"], run_id=event.run_id,
                    agent_id=agent_id, session_id=session_id, root=root)}
            event = event.model_copy(update={"data": data})
        return _to_sse(event)

    async def event_generator() -> AsyncIterator[str]:
        subscription = _create_subscription(app_state.event_bus, user_id=user_id, session_id=session_id)
        if app_state.settings.sse.replay_on_connect:
            for event in _event_replay(app_state.event_store, user_id, session_id, after_seq):
                yield encode(event)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    if subscription.resync_required.is_set():
                        yield "event: stream_reset_required\ndata: {\"resync_required\":true}\n\n"
                        break
                    event = await asyncio.wait_for(subscription.queue.get(), timeout=app_state.settings.sse.heartbeat_seconds)
                    if event.session_id == session_id:
                        yield encode(event)
                except TimeoutError:
                    yield _to_sse_comment(f"heartbeat {utc_now_iso()}")
        finally:
            _remove_subscription(app_state.event_bus, subscription)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _to_sse_comment(comment: str) -> str:
    return f": {comment}\n\n"


class LegacySessionAdapter:
    """把旧单会话指针限制在兼容层，所有执行仍委托资源化 Manager。"""

    def __init__(self, app_state: Any, user_id: str = "") -> None:
        self._app_state = app_state
        self.user_id = user_id
        self.agent_id: str | None = None
        snapshot = app_state.session_runner.get_status_snapshot()
        self.session_id: str | None = snapshot.get("session_id")
        self.run_ref: RunRef | None = None

    async def handle_input(self, payload: GatewayInput) -> dict[str, Any]:
        # 仅用于旧路由的轻量单元测试桩；正式 AppContext 始终提供 Manager。
        if not hasattr(self._app_state, "agent_runtime"):
            session = await self._app_state.session_runner.handle_input(payload)
            if session is not None:
                self.session_id = session.session_id
            return {
                "ok": True,
                "session": session.model_dump(exclude={"messages"}) if session else None,
            }
        if payload.type == GatewayInputType.USER_MESSAGE:
            agent_id = self._app_state.agent_runtime.find_active_agent_id(self.user_id, payload.agent_name or "")
            await self._app_state.agent_runtime.start_agent(self.user_id, agent_id)
            request_id = payload.metadata.get("client_request_id")
            legacy_best_effort = not isinstance(request_id, str) or not request_id
            if legacy_best_effort:
                request_id = f"legacy_{uuid4().hex}"
            run = await self._app_state.agent_runtime.start_run(
                self.user_id,
                agent_id,
                payload,
                payload.session_id,
                request_id,
            )
            self.agent_id, self.session_id, self.run_ref = agent_id, run.ref.session_id, run.ref
            return {
                "ok": True,
                "legacy_best_effort": legacy_best_effort,
                "session": self._app_state.agent_runtime.get_session_status(self.user_id, agent_id, run.ref.session_id),
            }
        if self.run_ref is None:
            raise RuntimeConflict("legacy_run_not_selected", "旧接口没有明确的活动 Run")
        if payload.type == GatewayInputType.STOP:
            run = await self._app_state.agent_runtime.cancel_run(self.run_ref)
        else:
            interaction_id = payload.approval_id or payload.question_id
            if not interaction_id:
                raise ValueError("缺少 interaction ID")
            run = await self._app_state.agent_runtime.reply_interaction(self.run_ref, interaction_id, payload)
        return {"ok": True, "session": self.status(), "run": run.model_dump()}

    async def load(self, session_id: str) -> dict[str, Any]:
        replay = await _memory_replay(self._app_state.session_memory, self.user_id, session_id)
        data = (replay.get("session") or {}).get("data") or {}
        agent_id = data.get("agent_id")
        if not agent_id:
            agent_id = self._app_state.agent_runtime.find_active_agent_id(self.user_id, str(data.get("agent_name") or ""))
        await self._app_state.agent_runtime.load_session(self.user_id, agent_id, session_id)
        self.agent_id, self.session_id, self.run_ref = agent_id, session_id, None
        return {
            "ok": True,
            "session": self.status(),
            "messages": replay.get("messages", []),
            "records": replay.get("records", []),
        }

    def status(self) -> dict[str, Any]:
        if not hasattr(self._app_state, "agent_runtime"):
            return self._app_state.session_runner.get_status_snapshot()
        if not self.agent_id or not self.session_id:
            return {"session_id": None, "status": "IDLE"}
        try:
            return self._app_state.agent_runtime.get_session_status(self.user_id, self.agent_id, self.session_id)
        except KeyError:
            return {"session_id": self.session_id, "agent_id": self.agent_id, "status": "IDLE"}


def _user_id(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.user_id if principal is not None else "legacy"


async def _memory_replay(memory: Any, user_id: str, session_id: str) -> dict[str, Any]:
    try:
        return await memory.replay(user_id, session_id)
    except TypeError:
        result = memory.replay(session_id)
        return await result if hasattr(result, "__await__") else result


def _memory_list(memory: Any, user_id: str) -> list[dict[str, Any]]:
    try:
        return memory.list_sessions(user_id)
    except TypeError:
        return memory.list_sessions()


def _event_replay(store: Any, user_id: str, session_id: str, after_seq: int) -> list[StreamEvent]:
    try:
        return store.replay(user_id, session_id=session_id, after_seq=after_seq)
    except TypeError:
        return store.replay(session_id=session_id, after_seq=after_seq)


def _create_subscription(event_bus: Any, *, user_id: str, session_id: str | None = None) -> Any:
    if hasattr(event_bus, "create_stream_subscription"):
        return event_bus.create_stream_subscription(user_id=user_id, session_id=session_id)
    queue = event_bus.create_stream_queue()
    return type("LegacySubscription", (), {"queue": queue, "resync_required": asyncio.Event()})()


def _remove_subscription(event_bus: Any, subscription: Any) -> None:
    if hasattr(event_bus, "remove_stream_subscription"):
        event_bus.remove_stream_subscription(subscription)
    else:
        event_bus.remove_stream_queue(subscription.queue)
