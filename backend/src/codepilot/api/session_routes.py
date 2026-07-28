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
from codepilot.utils import utc_now_iso


class LoadSessionRequest(BaseModel):
    session_id: str


def register_session_routes(router: APIRouter, app_state: Any) -> None:
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


def _to_sse_comment(comment: str) -> str:
    return f": {comment}\n\n"
