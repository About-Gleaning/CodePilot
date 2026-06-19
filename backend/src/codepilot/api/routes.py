from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from codepilot.events import StreamEvent
from codepilot.gateway import GatewayInput
from codepilot.scheduler.models import ScheduleRunStatus, ScheduleTrigger, compute_next_run_at
from codepilot.scheduler.service import ScheduleValidationError, validate_schedule_task_payload
from codepilot.session.attachments import AttachmentError, SUPPORTED_IMAGE_MIMES, attachment_message_dir, detect_image_mime, sanitize_attachment_filename
from codepilot.utils import utc_now_iso


class LoadSessionRequest(BaseModel):
    """加载历史会话的请求体。"""

    session_id: str


class ScheduleTaskRequest(BaseModel):
    name: str
    prompt: str
    agent_name: str
    provider: str
    model: str
    trigger: ScheduleTrigger
    working_dir: str
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    isolation_mode: str = "subprocess"


class ScheduleTaskPatchRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    agent_name: str | None = None
    provider: str | None = None
    model: str | None = None
    trigger: ScheduleTrigger | None = None
    working_dir: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None
    isolation_mode: str | None = None


class ScheduleRunReportRequest(BaseModel):
    run_id: str
    status: ScheduleRunStatus
    session_id: str | None = None
    summary: str | None = None
    error: str | None = None


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
        session_id = snapshot.get("session_id")
        if not session_id:
            return JSONResponse({"session": None, "messages": [], "records": []})
        replay = await app_state.session_memory.replay(session_id)
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
                "model_capabilities": {
                    model_id: {"thinking": model_settings.thinking.model_dump() if model_settings.thinking else None}
                    for model_id, model_settings in provider.model_settings.items()
                },
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

    @router.get("/attachments/{session_id}/{message_id}/{filename}")
    async def get_attachment(session_id: str, message_id: str, filename: str) -> FileResponse:
        """返回用户上传图片预览，读取范围限制在当前 workspace 的附件目录。"""
        try:
            safe_filename = sanitize_attachment_filename(filename)
            if safe_filename != filename:
                raise AttachmentError("附件文件名非法。")
            target_dir = attachment_message_dir(app_state.workspace, session_id, message_id).resolve()
            target = (target_dir / safe_filename).resolve(strict=False)
            if not target.is_relative_to(target_dir) or not target.exists() or not target.is_file():
                raise AttachmentError("附件不存在。")
        except AttachmentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with target.open("rb") as file:
            media_type = detect_image_mime(file.read(16))
        if media_type not in SUPPORTED_IMAGE_MIMES:
            raise HTTPException(status_code=415, detail="附件类型不支持预览")
        return FileResponse(target, media_type=media_type, filename=target.name)

    @router.get("/schedules")
    async def get_schedules() -> JSONResponse:
        """返回当前工作区全部定时任务。"""
        return JSONResponse({"schedules": [task.model_dump() for task in app_state.schedule_store.list_tasks()]})

    @router.post("/schedules")
    async def post_schedule(payload: ScheduleTaskRequest) -> JSONResponse:
        """创建一个定时任务。"""
        try:
            validated = validate_schedule_task_payload(
                settings=app_state.settings,
                agent_profiles=app_state.agent_profiles,
                payload=payload.model_dump(),
            )
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        task = app_state.schedule_runner.create_task(**validated)
        return JSONResponse({"ok": True, "schedule": task.model_dump()})

    @router.patch("/schedules/{task_id}")
    async def patch_schedule(task_id: str, payload: ScheduleTaskPatchRequest) -> JSONResponse:
        """更新定时任务。"""
        current = app_state.schedule_store.get_task(task_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"schedule `{task_id}` 不存在")
        raw_updates = payload.model_dump(exclude_unset=True)
        merged = current.model_dump()
        merged.update(raw_updates)
        try:
            validated = validate_schedule_task_payload(
                settings=app_state.settings,
                agent_profiles=app_state.agent_profiles,
                payload=merged,
            )
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        updates = {key: validated[key] for key in raw_updates if key in validated}
        if "trigger" in raw_updates:
            updates["trigger"] = validated["trigger"]
            updates["next_run_at"] = compute_next_run_at(validated["trigger"]) if merged.get("enabled", current.enabled) else None
        task = app_state.schedule_runner.update_task(task_id, updates)
        if task is None:
            raise HTTPException(status_code=404, detail=f"schedule `{task_id}` 不存在")
        return JSONResponse({"ok": True, "schedule": task.model_dump()})

    @router.delete("/schedules/{task_id}")
    async def delete_schedule(task_id: str) -> JSONResponse:
        """删除定时任务；已运行的 worker 不会被强杀。"""
        deleted = app_state.schedule_runner.delete_task(task_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"schedule `{task_id}` 不存在")
        return JSONResponse({"ok": True})

    @router.get("/schedule-runs")
    async def get_schedule_runs() -> JSONResponse:
        """返回 active runs 和最近运行记录，供前端轻量轮询。"""
        active = app_state.schedule_store.active_runs()
        recent = app_state.schedule_store.recent_runs(limit=20)
        return JSONResponse(
            {
                "active": [run.model_dump() for run in active],
                "recent": [run.model_dump() for run in recent],
            }
        )

    @router.post("/schedule-runs/{run_id}/report")
    async def post_schedule_run_report(
        run_id: str,
        payload: ScheduleRunReportRequest,
        request: Request,
        x_codepilot_schedule_token: str | None = Header(default=None),
    ) -> JSONResponse:
        """接收本机 worker 上报的执行结果。"""
        if payload.run_id != run_id:
            raise HTTPException(status_code=400, detail="report run_id 与路径不一致")
        if payload.status not in {ScheduleRunStatus.COMPLETED, ScheduleRunStatus.FAILED, ScheduleRunStatus.TIMEOUT}:
            raise HTTPException(status_code=400, detail="report status 只能是 completed/failed/timeout")
        client_host = request.client.host if request.client else ""
        if client_host and client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail="schedule report 只接受本机请求")
        if not x_codepilot_schedule_token or x_codepilot_schedule_token != app_state.schedule_store.token():
            raise HTTPException(status_code=403, detail="schedule report token 无效")
        try:
            run = await app_state.schedule_runner.report(
                run_id,
                status=payload.status,
                session_id=payload.session_id,
                summary=payload.summary,
                error=payload.error,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "run": run.model_dump()})

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
            active_session_id = replay_session_id
            queue = app_state.event_bus.create_stream_queue()
            if active_session_id and app_state.settings.sse.replay_on_connect:
                # 支持重连补发，避免前端在网络抖动后丢失关键事件。
                for replay_event in app_state.event_store.replay(session_id=active_session_id, after_seq=after_seq):
                    yield _to_sse(replay_event)

            heartbeat_seconds = app_state.settings.sse.heartbeat_seconds
            try:
                while True:
                    # 一旦客户端断开连接，立即停止生成数据，避免无意义占用资源。
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                        event_session_id = event.session_id
                        if active_session_id and event_session_id != active_session_id:
                            current_session_id = app_state.session_runner.current_session_id()
                            if event.event_type == "session_started" and event_session_id == current_session_id:
                                # 前端从“新会话”状态提交首条消息时，连接可能仍绑定旧会话；
                                # 以新 session_started 为准切换绑定，避免旧会话阻断新会话事件。
                                active_session_id = event_session_id
                            else:
                                continue
                        if not active_session_id:
                            if event.event_type != "session_started" or not event_session_id:
                                continue
                            active_session_id = event_session_id
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
