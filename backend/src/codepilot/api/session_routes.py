from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

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
    @router.get("/agent-runtimes")
    async def get_agent_runtimes() -> JSONResponse:
        return JSONResponse({"runtimes": [item.model_dump() for item in app_state.agent_runtime.list_agent_states()]})

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
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.post("/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str) -> JSONResponse:
        try:
            return JSONResponse((await app_state.agent_runtime.stop_agent(agent_id)).model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc

    @router.get("/agents/{agent_id}/sessions")
    async def get_agent_sessions(agent_id: str) -> JSONResponse:
        try:
            profile = app_state.agent_runtime._require_profile(agent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_not_found"}) from exc
        sessions = [item for item in app_state.session_memory.list_sessions() if item.get("agent_name") == profile.name]
        return JSONResponse({"sessions": sessions})

    @router.post("/agents/{agent_id}/runs")
    async def start_run(agent_id: str, payload: StartRunRequest) -> JSONResponse:
        request = GatewayInput(type=GatewayInputType.USER_MESSAGE, session_id=payload.session_id, content=payload.content, agent_name="runtime", provider=payload.provider, model=payload.model, metadata=payload.metadata, attachments=payload.attachments)
        try:
            run = await app_state.agent_runtime.start_run(agent_id, request, payload.session_id, payload.client_request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "agent_or_session_not_found"}) from exc
        except RuntimeConflict as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
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
        try:
            await app_state.agent_runtime.load_session(agent_id, session_id)
        except (KeyError, RuntimeConflict, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": str(exc)}) from exc
        return _stream_response(request, app_state, session_id, after_seq)

    @router.post("/session/input")
    async def post_session_input(payload: GatewayInput) -> JSONResponse:
        try:
            session = await app_state.session_runner.handle_input(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "session": session.model_dump(exclude={"messages"}) if session else None})

    @router.get("/session/status")
    async def get_session_status() -> JSONResponse:
        return JSONResponse(app_state.session_runner.get_status_snapshot())

    @router.get("/session/replay")
    async def get_session_replay() -> JSONResponse:
        snapshot = app_state.session_runner.get_status_snapshot()
        session_id = snapshot.get("session_id")
        if not session_id:
            return JSONResponse({"session": None, "messages": [], "records": []})
        return JSONResponse(await app_state.session_memory.replay(session_id))

    @router.get("/sessions")
    async def get_sessions() -> JSONResponse:
        return JSONResponse({"sessions": app_state.session_memory.list_sessions()})

    @router.post("/session/load")
    async def post_session_load(payload: LoadSessionRequest) -> JSONResponse:
        replay = await app_state.session_memory.replay(payload.session_id)
        try:
            session = app_state.session_runner.load_session(payload.session_id, replay)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "session": session.model_dump(exclude={"messages"}), "messages": replay.get("messages", []), "records": replay.get("records", [])})

    @router.get("/session/stream")
    async def get_session_stream(request: Request, after_seq: int = 0) -> StreamingResponse:
        async def event_generator() -> AsyncIterator[str]:
            snapshot = app_state.session_runner.get_status_snapshot()
            active_session_id = snapshot.get("session_id")
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
                            if event.event_type == "session_started" and event.session_id == app_state.session_runner.current_session_id():
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


def _stream_response(request: Request, app_state: Any, session_id: str, after_seq: int) -> StreamingResponse:
    """按明确 Session 建立 SSE；队列被总线移除时客户端自行重连回放。"""
    async def event_generator() -> AsyncIterator[str]:
        queue = app_state.event_bus.create_stream_queue()
        if app_state.settings.sse.replay_on_connect:
            for event in app_state.event_store.replay(session_id=session_id, after_seq=after_seq):
                yield _to_sse(event)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=app_state.settings.sse.heartbeat_seconds)
                    if event.session_id == session_id:
                        yield _to_sse(event)
                except TimeoutError:
                    yield _to_sse_comment(f"heartbeat {utc_now_iso()}")
        finally:
            app_state.event_bus.remove_stream_queue(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _to_sse_comment(comment: str) -> str:
    return f": {comment}\n\n"
