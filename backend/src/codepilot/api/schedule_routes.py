from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

from codepilot.scheduler.models import ScheduleRunStatus, ScheduleTrigger, compute_next_run_at
from codepilot.scheduler.service import ScheduleValidationError, validate_schedule_task_payload


class ScheduleTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=32000)
    agent_id: str
    provider: str
    model: str
    follow_agent_model: bool = False
    follow_agent_directory: bool = False
    trigger: ScheduleTrigger
    working_dir: str
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    isolation_mode: str = "subprocess"


class ScheduleTaskPatchRequest(ScheduleTaskRequest):
    name: str | None = None
    prompt: str | None = None
    agent_id: str | None = None
    provider: str | None = None
    model: str | None = None
    trigger: ScheduleTrigger | None = None
    working_dir: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None
    isolation_mode: str | None = None
    follow_agent_model: bool | None = None
    follow_agent_directory: bool | None = None


class ScheduleRunReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    user_id: str
    agent_id: str
    revision_id: str
    report_seq: int = Field(ge=1)
    status: ScheduleRunStatus
    session_id: str | None = None
    summary: str | None = Field(default=None, max_length=1000)
    error: str | None = Field(default=None, max_length=1000)
    phase: str = Field(default="", max_length=32)
    iteration: int = Field(default=0, ge=0, le=10000)
    stop_reason: str | None = Field(default=None, max_length=80)


class WorkerToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(max_length=128)
    agent_id: str = Field(max_length=256)
    revision_id: str = Field(max_length=128)
    session_id: str = Field(max_length=128)
    sender_agent_id: str = Field(max_length=256)
    sender_revision_id: str | None = Field(default=None, max_length=128)
    arguments: dict[str, Any]


class ScheduleRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                response = await handler(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except RequestValidationError:
                raise HTTPException(422, detail={"code": "schedule_input_invalid", "message": "定时请求格式不正确，请检查必填项、时间及字段类型。"})
        return safe_handler


def register_schedule_routes(router: APIRouter, app_state: Any) -> None:
    route_owner = getattr(router, "router", router)
    original_route_class = route_owner.route_class
    route_owner.route_class = ScheduleRoute
    def authenticate_worker(run_id, payload, request, token):
        coordinator = app_state.schedule_coordinator
        host = request.client.host if request.client else ""
        store = coordinator._stores.get(payload.user_id)
        run = store.get_run(run_id) if store else None
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"} or run is None or (run.agent_id, run.revision_id, run.session_id) != (payload.agent_id, payload.revision_id, payload.session_id):
            raise HTTPException(403, detail={"code": "worker_identity_invalid", "message": "执行身份校验失败。"})
        token_file = coordinator.runner(payload.user_id).token_path(run_id)
        if not token_file.is_file() or not token or not secrets.compare_digest(token, token_file.read_text()):
            raise HTTPException(403, detail={"code": "worker_token_invalid", "message": "执行凭据无效。"})
        return run

    @router.post("/schedule-runs/{run_id}/tool")
    async def worker_schedule_tool(run_id: str, payload: WorkerToolRequest, request: Request, x_codepilot_schedule_token: str | None = Header(None)):
        import json
        from types import SimpleNamespace
        from codepilot.tools.schedule_manage_tool import ScheduleManageTool
        from codepilot.session.agents import AgentProfile
        from codepilot.session.state import RunRef
        run = authenticate_worker(run_id, payload, request, x_codepilot_schedule_token)
        if run.status != ScheduleRunStatus.RUNNING or len(json.dumps(payload.arguments).encode()) > 65536:
            raise HTTPException(409, detail={"code": "worker_tool_unavailable", "message": "本轮已结束或工具输入超过限制。"})
        coordinator = app_state.schedule_coordinator
        profile = coordinator.runner(run.user_id).execution_profiles.get(run_id)
        if profile is None:
            raise HTTPException(409, detail={"code": "worker_snapshot_missing", "message": "执行快照不可用。"})
        if payload.sender_agent_id != profile.agent_id:
            if not profile.can_delegate or (profile.delegate_agent_ids is not None and payload.sender_agent_id not in profile.delegate_agent_ids):
                raise HTTPException(403, detail="委派身份不允许调用调度工具")
            if profile.publication_id:
                child = profile.publication_children.get(payload.sender_agent_id)
                if child is None:
                    raise HTTPException(403, detail="委派身份不存在")
                profile = AgentProfile.model_validate(child)
            else:
                if not payload.sender_revision_id:
                    raise HTTPException(403, detail="委派快照身份缺失")
                try:
                    profile = app_state.agent_config_service.get_profile_revision_snapshot(run.user_id, payload.sender_agent_id, payload.sender_revision_id)
                except (ValueError, OSError):
                    raise HTTPException(409, detail={"code": "worker_child_invalid", "message": "子 Agent 的执行配置已不可用。"})
            if not profile.supports_delegated:
                raise HTTPException(403, detail="委派身份不匹配")
        tool = ScheduleManageTool(store=coordinator, runner=coordinator, settings=app_state.settings,
                                 agent_profiles={}, timeout_seconds=15)
        ref = RunRef(user_id=run.user_id, agent_id=run.agent_id, session_id=run.session_id, run_id=run.id, revision_id=run.revision_id)
        return JSONResponse(await tool.execute(payload.arguments, SimpleNamespace(agent=profile, run_ref=ref)))
    @router.get("/schedules")
    async def get_schedules(request: Request, agent_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
        store = app_state.schedule_coordinator.store(_user_id(request))
        tasks = [task for task in store.list_tasks() if agent_id is None or task.agent_id == agent_id]
        return JSONResponse({"schedules": [task.model_dump() for task in tasks[offset:offset + limit]], "total": len(tasks)}, headers={"Cache-Control": "no-store"})

    @router.post("/schedules")
    async def post_schedule(payload: ScheduleTaskRequest, request: Request) -> JSONResponse:
        user_id = _user_id(request)
        try:
            validated = validate_schedule_task_payload(settings=app_state.settings, agent_profiles=None, payload=payload.model_dump(), profile_resolver=app_state.agent_config_service.get_active_profile_snapshot, user_id=user_id)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.error_type, "message": exc.message}) from exc
        runner = await app_state.schedule_coordinator.ensure_started(user_id)
        return JSONResponse({"ok": True, "schedule": runner.create_task(**validated).model_dump()})

    @router.patch("/schedules/{task_id}")
    async def patch_schedule(task_id: str, payload: ScheduleTaskPatchRequest, request: Request) -> JSONResponse:
        user_id = _user_id(request)
        store = app_state.schedule_coordinator.store(user_id)
        current = store.get_task(task_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"schedule `{task_id}` 不存在")
        raw_updates = payload.model_dump(exclude_unset=True)
        if raw_updates == {"enabled": False}:
            task = app_state.schedule_coordinator.runner(user_id).update_task(task_id, raw_updates)
            return JSONResponse({"ok": True, "schedule": task.model_dump()})
        merged = current.model_dump() | raw_updates
        try:
            validated = validate_schedule_task_payload(settings=app_state.settings, agent_profiles=None, payload=merged, profile_resolver=app_state.agent_config_service.get_active_profile_snapshot, user_id=user_id)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.error_type, "message": exc.message}) from exc
        updates = {key: validated[key] for key in raw_updates if key in validated}
        updates.update({key: validated[key] for key in ("agent_id", "agent_name", "revision_id")})
        if "trigger" in raw_updates:
            updates["trigger"] = validated["trigger"]
            updates["next_run_at"] = compute_next_run_at(validated["trigger"]) if merged.get("enabled", current.enabled) else None
        runner = await app_state.schedule_coordinator.ensure_started(user_id) if validated["enabled"] else app_state.schedule_coordinator.runner(user_id)
        task = runner.update_task(task_id, updates)
        if task is None:
            raise HTTPException(status_code=404, detail=f"schedule `{task_id}` 不存在")
        return JSONResponse({"ok": True, "schedule": task.model_dump()})

    @router.delete("/schedules/{task_id}")
    async def delete_schedule(task_id: str, request: Request) -> JSONResponse:
        if not app_state.schedule_coordinator.runner(_user_id(request)).delete_task(task_id):
            raise HTTPException(status_code=404, detail=f"schedule `{task_id}` 不存在")
        return JSONResponse({"ok": True})

    @router.get("/schedule-runs")
    async def get_schedule_runs(request: Request, agent_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
        store = app_state.schedule_coordinator.store(_user_id(request))
        active = [run for run in store.active_runs() if agent_id is None or run.agent_id == agent_id]
        active_ids = {run.id for run in active}
        recent = [run for run in store.list_runs() if run.id not in active_ids and (agent_id is None or run.agent_id == agent_id)]
        return JSONResponse({"active": [_public_run(run) for run in active[:100]], "recent": [_public_run(run) for run in recent[offset:offset + limit]], "total": len(recent)}, headers={"Cache-Control": "no-store"})

    @router.post("/schedule-runs/{run_id}/stop")
    async def stop_schedule_run(run_id: str, request: Request) -> JSONResponse:
        user_id = _user_id(request)
        if app_state.schedule_coordinator.store(user_id).get_run(run_id) is None:
            raise HTTPException(404, detail={"code": "schedule_run_not_found", "message": "定时执行不存在。"})
        run = await app_state.schedule_coordinator.runner(user_id).stop_run(run_id)
        return JSONResponse({"run": _public_run(run)}, headers={"Cache-Control": "no-store"})

    @router.get("/schedule-runs/{run_id}/events")
    async def schedule_events(run_id: str, request: Request, cursor: str = Query("", max_length=40)) -> JSONResponse:
        import asyncio
        from codepilot.scheduler.events import read_events
        from codepilot.api.session_routes import _redact_local_paths
        user_id = _user_id(request)
        run = app_state.schedule_coordinator.store(user_id).get_run(run_id)
        if run is None or not run.session_id:
            raise HTTPException(404, detail={"code": "schedule_run_not_found", "message": "定时会话尚未创建或不存在。"})
        directory = app_state.workspace.for_user(user_id).sessions_dir
        try:
            batch = await asyncio.to_thread(read_events, directory, run.session_id, cursor)
        except (ValueError, OSError):
            raise HTTPException(409, detail={"code": "schedule_events_invalid", "message": "执行记录暂不可读取，请重新加载会话。"})
        batch["events"] = [event for event in batch["events"] if event.get("user_id") == user_id and event.get("agent_id") == run.agent_id and event.get("run_id") == run.id]
        from pathlib import Path
        from codepilot.session.workbench import decorate_message
        for event in batch["events"]:
            data = event.get("data") or {}
            if isinstance(data.get("message"), dict):
                data["message"] = decorate_message(data["message"], run_id=run.id, agent_id=run.agent_id,
                    session_id=run.session_id, root=Path(run.working_dir))
        batch["runtime"] = app_state.schedule_coordinator.runtime_snapshot(run)
        return JSONResponse(_redact_local_paths(batch), headers={"Cache-Control": "no-store"})

    @router.post("/schedule-runs/{run_id}/report")
    async def post_schedule_run_report(run_id: str, payload: ScheduleRunReportRequest, request: Request, x_codepilot_schedule_token: str | None = Header(default=None)) -> JSONResponse:
        if payload.run_id != run_id:
            raise HTTPException(status_code=400, detail="report run_id 与路径不一致")
        if payload.status not in {ScheduleRunStatus.RUNNING, ScheduleRunStatus.COMPLETED, ScheduleRunStatus.FAILED, ScheduleRunStatus.TIMEOUT, ScheduleRunStatus.CANCELLED}:
            raise HTTPException(status_code=400, detail="无效的执行状态")
        coordinator = app_state.schedule_coordinator
        authenticate_worker(run_id, payload, request, x_codepilot_schedule_token)
        try:
            run = await coordinator.report(run_id, user_id=payload.user_id, status=payload.status,
                session_id=payload.session_id, summary=None, error=payload.error, report_seq=payload.report_seq,
                phase=payload.phase, iteration=payload.iteration, stop_reason=payload.stop_reason)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "run": run.model_dump()})

    route_owner.route_class = original_route_class


def _public_run(run: Any) -> dict[str, Any]:
    from codepilot.api.session_routes import _redact_local_paths
    return _redact_local_paths(run.model_dump(exclude={"working_dir", "pid", "user_id"}))


def _user_id(request: Request) -> str:
    return request.state.principal.user_id
