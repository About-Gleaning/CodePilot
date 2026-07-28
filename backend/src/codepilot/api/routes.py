from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .schedule_routes import register_schedule_routes
from .session_routes import register_session_routes
from .workspace_routes import register_workspace_routes


def build_api_router(app_state: Any) -> APIRouter:
    """按既有领域顺序装配公开 API，保持路径与注册顺序兼容。"""
    router = APIRouter(prefix="/api")
    register_session_routes(router, app_state)
    register_workspace_routes(router, app_state)
    register_schedule_routes(router, app_state)
    return router
