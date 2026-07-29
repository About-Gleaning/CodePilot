from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from codepilot.session.agent_config import AgentConfigError


class AgentPayload(BaseModel):
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

    def invoke(action: Any) -> JSONResponse:
        try:
            return JSONResponse(action())
        except AgentConfigError as exc:
            raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.get("/agents")
    async def list_agents(status: Literal["active", "archived", "all"] = "active") -> JSONResponse:
        return JSONResponse({"agents": service().list(status)})

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str) -> JSONResponse:
        return invoke(lambda: service().get(agent_id))

    @router.post("/agents", status_code=201)
    async def create_agent(payload: AgentPayload) -> JSONResponse:
        return invoke(lambda: service().create(payload.model_dump()))

    @router.put("/agents/{agent_id}")
    async def update_agent(agent_id: str, payload: AgentPayload) -> JSONResponse:
        return invoke(lambda: service().update(agent_id, payload.model_dump()))

    @router.post("/agents/{agent_id}/archive")
    async def archive_agent(agent_id: str) -> JSONResponse:
        return invoke(lambda: service().archive(agent_id))

    @router.post("/agents/{agent_id}/restore")
    async def restore_agent(agent_id: str) -> JSONResponse:
        return invoke(lambda: service().restore(agent_id))

    @router.get("/agent-capabilities")
    async def get_agent_capabilities(_: Request) -> JSONResponse:
        return JSONResponse(service().capabilities())
