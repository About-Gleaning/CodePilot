from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .schedule_routes import register_schedule_routes
from .auth_routes import register_auth_routes
from .agent_routes import register_agent_routes
from .health_routes import register_health_routes
from .memory_routes import register_memory_routes
from .skill_routes import register_skill_routes
from .hook_routes import register_hook_routes
from .tool_routes import register_tool_routes
from .connection_routes import register_connection_routes
from .session_routes import register_session_routes
from .workspace_routes import register_workspace_routes
from .publication_routes import register_publication_routes


def build_api_router(app_state: Any) -> APIRouter:
    """按既有领域顺序装配公开 API，保持路径与注册顺序兼容。"""
    router = APIRouter(prefix="/api")
    register_health_routes(router, app_state)
    register_auth_routes(router, app_state)
    register_session_routes(router, app_state)
    register_agent_routes(router, app_state)
    register_publication_routes(router, app_state)
    register_memory_routes(router, app_state)
    register_skill_routes(router, app_state)
    if hasattr(app_state, "workspace"):
        register_hook_routes(router, app_state)
        register_tool_routes(router, app_state)
    register_connection_routes(router, app_state)
    register_workspace_routes(router, app_state)
    register_schedule_routes(router, app_state)
    return router
