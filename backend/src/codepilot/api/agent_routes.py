from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from codepilot.session.agent_config import AgentConfigError


class AgentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str
    system_prompt: str
    default_provider: str
    default_model: str
    default_thinking_value: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    mcp_server_names: list[str] = Field(default_factory=list)
    readonly: bool = False
    expected_revision_id: str | None = None


def register_agent_routes(router: APIRouter, app_state: Any) -> None:
    def service() -> Any:
        return app_state.agent_config_service

    def call(name: str, user_id: str, *args: Any) -> Any:
        target = getattr(service(), name)
        return target(user_id, *args) if hasattr(service(), "shared") else target(*args)

    def invoke(action: Any) -> JSONResponse:
        try:
            return JSONResponse(action())
        except AgentConfigError as exc:
            raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.get("/agents")
    async def list_agents(request: Request, status: Literal["active", "archived", "all"] = "active") -> JSONResponse:
        return JSONResponse({"agents": call("list", _user_id(request), status)})

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str, request: Request) -> JSONResponse:
        return invoke(lambda: call("get", _user_id(request), agent_id))

    @router.post("/agents", status_code=201)
    async def create_agent(payload: AgentPayload, request: Request) -> JSONResponse:
        return invoke(lambda: call("create", _user_id(request), payload.model_dump()))

    @router.put("/agents/{agent_id}")
    async def update_agent(agent_id: str, payload: AgentPayload, request: Request) -> JSONResponse:
        return invoke(lambda: call("update", _user_id(request), agent_id, payload.model_dump()))

    @router.post("/agents/{agent_id}/archive")
    async def archive_agent(agent_id: str, request: Request) -> JSONResponse:
        return invoke(lambda: call("archive", _user_id(request), agent_id))

    @router.post("/agents/{agent_id}/restore")
    async def restore_agent(agent_id: str, request: Request) -> JSONResponse:
        return invoke(lambda: call("restore", _user_id(request), agent_id))

    @router.get("/agent-capabilities")
    async def get_agent_capabilities(request: Request) -> JSONResponse:
        return JSONResponse(call("capabilities", _user_id(request)))


def _user_id(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.user_id if principal is not None else "legacy"
