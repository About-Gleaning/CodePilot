from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from codepilot.events import StreamEvent
from codepilot.gateway import GatewayInput
from codepilot.gateway import GatewayInputType
from codepilot.session.state import RunRef
from codepilot.session.agent_runtime import RuntimeConflict
from codepilot.utils import utc_now_iso


class LoadSessionRequest(BaseModel):
    session_id: str


class StartRunRequest(BaseModel):
    session_id: str | None = None
    content: str
    provider: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = {}
    attachments: list[Any] = []
    client_request_id: str


class InteractionReplyRequest(BaseModel):
    type: GatewayInputType
    approved: bool | None = None
    answers: dict[str, Any] | None = None
    comment: str | None = None


def register_session_routes(router: APIRouter, app_state: Any) -> None:
    legacy = LegacySessionAdapter(app_state)
    @router.get("/agent-runtimes")
    async def get_agent_runtimes() -> JSONResponse:
        return JSONResponse({"runtimes": [item.model_dump() for item in app_state.agent_runtime.list_agent_states()]})

    @router.get("/agent-runtimes/stream")
    async def get_agent_runtime_stream(request: Request, cursor: str | None = None) -> StreamingResponse:
        subscription = app_state.agent_runtime.create_runtime_subscription()
        try:
            replay = await app_state.agent_runtime.replay_runtime_events(cursor)
        except RuntimeConflict as exc:
            app_state.agent_runtime.remove_runtime_subscription(subscription)
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
                app_state.agent_runtime.remove_runtime_subscription(subscription)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.get("/agents/{agent_id}/runtime")
    async def get_agent_runtime(agent_id: str) -> JSONResponse:
        try:
            return JSONResponse(app_state.agent_runtime.get_agent_state(agent_id).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc

    @router.post("/agents/{agent_id}/start")
    async def start_agent(agent_id: str) -> JSONResponse:
        try:
            return JSONResponse((await app_state.agent_runtime.start_agent(agent_id)).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.post("/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str) -> JSONResponse:
        try:
            return JSONResponse((await app_state.agent_runtime.stop_agent(agent_id)).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc

    @router.get("/agents/{agent_id}/sessions")
    async def get_agent_sessions(agent_id: str) -> JSONResponse:
        try:
            sessions = app_state.agent_runtime.list_sessions(agent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc
        return JSONResponse({"sessions": sessions})

    @router.post("/agents/{agent_id}/runs")
    async def start_run(agent_id: str, payload: StartRunRequest) -> JSONResponse:
        request = GatewayInput(type=GatewayInputType.USER_MESSAGE, session_id=payload.session_id, content=payload.content, agent_name="runtime", provider=payload.provider, model=payload.model, metadata=payload.metadata, attachments=payload.attachments)
        try:
            run = await app_state.agent_runtime.start_run(agent_id, request, payload.session_id, payload.client_request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_or_session_not_found"}) from exc
        except RuntimeConflict as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            raise HTTPException(
                status_code=exc.status,
                detail={"code": exc.code, "message": str(exc)},
                headers=headers,
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_run_request", "message": str(exc)}) from exc
        return JSONResponse(run.model_dump())

    @router.get("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}")
    async def get_run(agent_id: str, session_id: str, run_id: str) -> JSONResponse:
        try:
            return JSONResponse(app_state.agent_runtime.get_run_state(RunRef(agent_id=agent_id, session_id=session_id, run_id=run_id)).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "run_not_found"}) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc

    @router.post("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel")
    async def cancel_run(agent_id: str, session_id: str, run_id: str) -> JSONResponse:
        try:
            run = await app_state.agent_runtime.cancel_run(RunRef(agent_id=agent_id, session_id=session_id, run_id=run_id))
            return JSONResponse(run.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "run_not_found"}) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc

    @router.post("/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/interactions/{interaction_id}")
    async def reply_interaction(agent_id: str, session_id: str, run_id: str, interaction_id: str, payload: InteractionReplyRequest) -> JSONResponse:
        if payload.type not in {GatewayInputType.HUMAN_REPLY, GatewayInputType.QUESTION_REPLY, GatewayInputType.QUESTION_DECLINE}:
            raise HTTPException(status_code=422, detail={"code": "invalid_interaction_type"})
        request = GatewayInput(type=payload.type, approval_id=interaction_id if payload.type == GatewayInputType.HUMAN_REPLY else None, question_id=interaction_id if payload.type != GatewayInputType.HUMAN_REPLY else None, approved=payload.approved, answers=payload.answers, comment=payload.comment)
        try:
            run = await app_state.agent_runtime.reply_interaction(RunRef(agent_id=agent_id, session_id=session_id, run_id=run_id), interaction_id, request)
            return JSONResponse(run.model_dump())
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=409, detail={"code": getattr(exc, "code", "interaction_conflict"), "message": str(exc)}) from exc

    @router.get("/agents/{agent_id}/sessions/{session_id}/replay")
    async def get_agent_session_replay(agent_id: str, session_id: str) -> JSONResponse:
        try:
            await app_state.agent_runtime.load_session(agent_id, session_id)
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": str(exc)}) from exc
        return JSONResponse(await app_state.session_memory.replay(session_id))

    @router.get("/agents/{agent_id}/sessions/{session_id}/stream")
    async def get_agent_session_stream(agent_id: str, session_id: str, request: Request, after_seq: int = 0) -> StreamingResponse:
        if after_seq < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_after_seq"})
        try:
            await app_state.agent_runtime.load_session(agent_id, session_id)
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": str(exc)}) from exc
        return _stream_response(request, app_state, session_id, after_seq)

    @router.post("/session/input")
    async def post_session_input(payload: GatewayInput) -> JSONResponse:
        try:
            return JSONResponse(await legacy.handle_input(payload))
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(
                status_code=getattr(exc, "status", 409),
                detail={"code": getattr(exc, "code", "legacy_session_conflict"), "message": str(exc)},
            ) from exc

    @router.get("/session/status")
    async def get_session_status() -> JSONResponse:
        return JSONResponse(legacy.status())

    @router.get("/session/replay")
    async def get_session_replay() -> JSONResponse:
        session_id = legacy.session_id
        if not session_id:
            return JSONResponse({"session": None, "messages": [], "records": []})
        return JSONResponse(await app_state.session_memory.replay(session_id))

    @router.get("/sessions")
    async def get_sessions() -> JSONResponse:
        return JSONResponse({"sessions": app_state.session_memory.list_sessions()})

    @router.post("/session/load")
    async def post_session_load(payload: LoadSessionRequest) -> JSONResponse:
        try:
            result = await legacy.load(payload.session_id)
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=409, detail={"code": "legacy_session_conflict", "message": str(exc)}) from exc
        return JSONResponse(result)

    @router.get("/session/stream")
    async def get_session_stream(request: Request, after_seq: int = 0) -> StreamingResponse:
        if after_seq < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_after_seq"})
        async def event_generator() -> AsyncIterator[str]:
            active_session_id = legacy.session_id
            queue = app_state.event_bus.create_stream_queue()
            if active_session_id and app_state.settings.sse.replay_on_connect:
                for event in app_state.event_store.replay(session_id=active_session_id, after_seq=after_seq):
                    yield _to_sse(event)
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=app_state.settings.sse.heartbeat_seconds)
                        if active_session_id and event.session_id != active_session_id:
                            if event.event_type == "session_started" and event.session_id == legacy.session_id:
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
                app_state.event_bus.remove_stream_queue(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")


def _to_sse(event: StreamEvent) -> str:
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {json.dumps(event.model_dump(), ensure_ascii=False)}\n\n"


def _to_control_sse(event: StreamEvent, cursor: str) -> str:
    return f"id: {cursor}\nevent: {event.event_type}\ndata: {json.dumps(event.model_dump(), ensure_ascii=False)}\n\n"


def _stream_response(request: Request, app_state: Any, session_id: str, after_seq: int) -> StreamingResponse:
    """按明确 Session 建立 SSE；队列被总线移除时客户端自行重连回放。"""
    async def event_generator() -> AsyncIterator[str]:
        subscription = app_state.event_bus.create_stream_subscription()
        if app_state.settings.sse.replay_on_connect:
            for event in app_state.event_store.replay(session_id=session_id, after_seq=after_seq):
                yield _to_sse(event)
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
                        yield _to_sse(event)
                except TimeoutError:
                    yield _to_sse_comment(f"heartbeat {utc_now_iso()}")
        finally:
            app_state.event_bus.remove_stream_subscription(subscription)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _to_sse_comment(comment: str) -> str:
    return f": {comment}\n\n"


class LegacySessionAdapter:
    """把旧单会话指针限制在兼容层，所有执行仍委托资源化 Manager。"""

    def __init__(self, app_state: Any) -> None:
        self._app_state = app_state
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
            agent_id = self._app_state.agent_runtime.find_active_agent_id(payload.agent_name or "")
            await self._app_state.agent_runtime.start_agent(agent_id)
            request_id = payload.metadata.get("client_request_id")
            legacy_best_effort = not isinstance(request_id, str) or not request_id
            if legacy_best_effort:
                request_id = f"legacy_{uuid4().hex}"
            run = await self._app_state.agent_runtime.start_run(
                agent_id,
                payload,
                payload.session_id,
                request_id,
            )
            self.agent_id, self.session_id, self.run_ref = agent_id, run.ref.session_id, run.ref
            return {
                "ok": True,
                "legacy_best_effort": legacy_best_effort,
                "session": self._app_state.agent_runtime.get_session_status(agent_id, run.ref.session_id),
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
        replay = await self._app_state.session_memory.replay(session_id)
        data = (replay.get("session") or {}).get("data") or {}
        agent_id = data.get("agent_id")
        if not agent_id:
            agent_id = self._app_state.agent_runtime.find_active_agent_id(str(data.get("agent_name") or ""))
        await self._app_state.agent_runtime.load_session(agent_id, session_id)
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
            return self._app_state.agent_runtime.get_session_status(self.agent_id, self.session_id)
        except KeyError:
            return {"session_id": self.session_id, "agent_id": self.agent_id, "status": "IDLE"}
