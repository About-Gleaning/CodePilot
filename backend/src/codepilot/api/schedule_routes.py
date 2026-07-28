from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from codepilot.scheduler.models import ScheduleRunStatus, ScheduleTrigger, compute_next_run_at
from codepilot.scheduler.service import ScheduleValidationError, validate_schedule_task_payload


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


class ScheduleTaskPatchRequest(ScheduleTaskRequest):
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


def register_schedule_routes(router: APIRouter, app_state: Any) -> None:
    @router.get("/schedules")
    async def get_schedules() -> JSONResponse:
        return JSONResponse({"schedules": [task.model_dump() for task in app_state.schedule_store.list_tasks()]})

    @router.post("/schedules")
    async def post_schedule(payload: ScheduleTaskRequest) -> JSONResponse:
        try:
            validated = validate_schedule_task_payload(settings=app_state.settings, agent_profiles=app_state.agent_profiles, payload=payload.model_dump())
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        return JSONResponse({"ok": True, "schedule": app_state.schedule_runner.create_task(**validated).model_dump()})

    @router.patch("/schedules/{task_id}")
    async def patch_schedule(task_id: str, payload: ScheduleTaskPatchRequest) -> JSONResponse:
        current = app_state.schedule_store.get_task(task_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"schedule `{task_id}` 不存在")
        raw_updates = payload.model_dump(exclude_unset=True)
        merged = current.model_dump() | raw_updates
        try:
            validated = validate_schedule_task_payload(settings=app_state.settings, agent_profiles=app_state.agent_profiles, payload=merged)
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
        if not app_state.schedule_runner.delete_task(task_id):
            raise HTTPException(status_code=404, detail=f"schedule `{task_id}` 不存在")
        return JSONResponse({"ok": True})

    @router.get("/schedule-runs")
    async def get_schedule_runs() -> JSONResponse:
        return JSONResponse({"active": [run.model_dump() for run in app_state.schedule_store.active_runs()], "recent": [run.model_dump() for run in app_state.schedule_store.recent_runs(limit=20)]})

    @router.post("/schedule-runs/{run_id}/report")
    async def post_schedule_run_report(run_id: str, payload: ScheduleRunReportRequest, request: Request, x_codepilot_schedule_token: str | None = Header(default=None)) -> JSONResponse:
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
            run = await app_state.schedule_runner.report(run_id, status=payload.status, session_id=payload.session_id, summary=payload.summary, error=payload.error)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "run": run.model_dump()})
