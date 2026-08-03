from __future__ import annotations

"""进程存活与运行时就绪探针。"""

import os
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from codepilot import __version__
from codepilot.utils import utc_now_iso


def register_health_routes(router: APIRouter, app_state: Any) -> None:
    @router.get("/health/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "alive", "version": __version__, "time": utc_now_iso()})

    @router.get("/health/ready")
    async def ready() -> JSONResponse:
        runtime = app_state.agent_runtime.readiness_snapshot()
        provider_count = len(app_state.settings.llm_runtime.activated_providers)
        workspace_dir = app_state.workspace.workspace_dir
        storage_writable = workspace_dir.is_dir() and os.access(workspace_dir, os.W_OK)
        mcp = app_state.mcp_manager.list_server_capabilities()
        unavailable_mcp = sum(1 for item in mcp if item.get("status") == "unavailable")
        ready_now = (
            runtime["recovered"]
            and runtime["error_code"] is None
            and provider_count > 0
            and storage_writable
        )
        status = "ready" if ready_now and unavailable_mcp == 0 else "degraded" if ready_now else "not_ready"
        payload = {
            "status": status,
            "version": __version__,
            "components": {
                "runtime": "ready" if runtime["recovered"] and runtime["error_code"] is None else "not_ready",
                "storage": "ready" if storage_writable else "not_ready",
                "llm_provider_count": provider_count,
                "mcp_configured_count": len(mcp),
                "mcp_unavailable_count": unavailable_mcp,
            },
            "error_code": runtime["error_code"],
        }
        return JSONResponse(payload, status_code=200 if ready_now else 503)

