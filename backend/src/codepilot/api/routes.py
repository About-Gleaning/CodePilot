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
    """加载历史会话的请求体。"""

    session_id: str


def build_api_router(app_state: Any) -> APIRouter:
    """构建 API 路由。

    这里集中注册后端对外暴露的 HTTP 接口，职责包括：
    - 接收前端输入并驱动会话运行。
    - 查询当前会话状态和历史回放数据。
    - 返回前端启动所需的运行配置。
    - 提供 SSE 事件流，让前端实时感知会话进度。
    """
    router = APIRouter(prefix="/api")

    @router.post("/session/input")
    async def post_session_input(payload: GatewayInput) -> JSONResponse:
        """接收一次新的会话输入，并交给运行器处理。

        这个接口通常由前端在用户发送消息时调用。
        如果当前会话状态不允许继续输入，底层会抛出异常，这里统一转成 409 响应。
        """
        try:
            session = await app_state.session_runner.handle_input(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "session": session.model_dump(exclude={"messages"}) if session else None})

    @router.get("/session/status")
    async def get_session_status() -> JSONResponse:
        """返回当前会话的状态快照。

        适合前端轮询或页面初始化时快速确认：
        当前是否有会话、会话处于什么状态、是否还在运行。
        """
        return JSONResponse(app_state.session_runner.get_status_snapshot())

    @router.get("/session/replay")
    async def get_session_replay() -> JSONResponse:
        """返回当前会话的回放数据。

        这个接口用于在前端刷新、重连或首次进入页面时，
        重新拿到当前会话已经产生的历史事件和消息内容。
        """
        snapshot = app_state.session_runner.get_status_snapshot()
        replay = await app_state.session_memory.replay(snapshot.get("session_id"))
        return JSONResponse(replay)

    @router.get("/sessions")
    async def get_sessions() -> JSONResponse:
        """返回当前工作区的历史会话摘要列表。"""
        return JSONResponse({"sessions": app_state.session_memory.list_sessions()})

    @router.post("/session/load")
    async def post_session_load(payload: LoadSessionRequest) -> JSONResponse:
        """加载指定历史会话，让后续用户输入继续追加到该 session。"""
        replay = await app_state.session_memory.replay(payload.session_id)
        try:
            session = app_state.session_runner.load_session(payload.session_id, replay)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(
            {
                "ok": True,
                "session": session.model_dump(exclude={"messages"}),
                "messages": replay.get("messages", []),
                "records": replay.get("records", []),
            }
        )

    @router.get("/config")
    async def get_config() -> JSONResponse:
        """返回前端运行所需的基础配置。

        包括工作区信息、当前已激活的模型厂商和模型列表、可用 Agent、
        以及 SSE 相关配置，避免前端硬编码这些内容。
        """
        settings = app_state.settings
        activated_providers = [
            {
                "provider": provider.provider,
                "label": provider.label,
                "models": provider.models,
            }
            for provider in settings.llm_runtime.activated_providers.values()
        ]
        return JSONResponse(
            {
                "workspace_id": app_state.workspace.workspace_id,
                "workspace_path": str(app_state.workspace.workspace_path),
                "codepilot_home": str(app_state.workspace.codepilot_home),
                "activated_providers": activated_providers,
                "agents": [
                    name
                    for name, profile in app_state.agent_profiles.items()
                    if getattr(profile, "kind", "agent") == "agent"
                ],
                "sse": settings.sse.model_dump(),
            }
        )

    @router.get("/session/stream")
    async def get_session_stream(request: Request, after_seq: int = 0) -> StreamingResponse:
        """建立当前会话的 SSE 长连接。

        前端连上这个接口后，可以持续收到会话运行中的实时事件，
        比如消息流式输出、状态变化、人工审批请求等。

        `after_seq` 用于断线重连场景：
        前端可以告诉后端“我已经收到哪一条事件”，
        后端会先补发缺失事件，再继续推送新事件。
        """
        async def event_generator() -> AsyncIterator[str]:
            """持续产出 SSE 数据帧。

            生成器会先按需回放历史事件，再进入实时订阅模式。
            如果一段时间没有业务事件，就发送注释帧做心跳保活。
            """
            snapshot = app_state.session_runner.get_status_snapshot()
            replay_session_id = snapshot.get("session_id")
            queue = app_state.event_bus.create_stream_queue()
            if app_state.settings.sse.replay_on_connect:
                # 支持重连补发，避免前端在网络抖动后丢失关键事件。
                for replay_event in app_state.event_store.replay(session_id=replay_session_id, after_seq=after_seq):
                    yield _to_sse(replay_event)

            heartbeat_seconds = app_state.settings.sse.heartbeat_seconds
            try:
                while True:
                    # 一旦客户端断开连接，立即停止生成数据，避免无意义占用资源。
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                        yield _to_sse(event)
                    except TimeoutError:
                        # 使用 SSE 注释帧保活，避免把保活噪音暴露为业务事件。
                        yield _to_sse_comment(f"heartbeat {utc_now_iso()}")
            finally:
                app_state.event_bus.remove_stream_queue(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return router


def _to_sse(event: StreamEvent) -> str:
    """把内部事件对象格式化成标准 SSE 文本帧。"""
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {json.dumps(event.model_dump(), ensure_ascii=False)}\n\n"


def _to_sse_comment(comment: str) -> str:
    """把注释内容格式化成 SSE 注释帧，常用于心跳保活。"""
    return f": {comment}\n\n"
