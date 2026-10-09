from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ConfigDict, Field

from codepilot.session.agent_config import AgentConfigError
from codepilot.session.agents import AgentAssembly


class AgentPayload(AgentAssembly):
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
    kind: Literal["agent", "subagent"] = "agent"
    # 与当前网页表单保持一致；省略值由 exclude_unset 留给配置服务兼容旧客户端。
    launch_modes: list[Literal["direct", "delegated"]] = Field(default_factory=lambda: ["direct"], min_length=1)
    can_delegate: bool = Field(default=False, strict=True)
    max_iterations: int = Field(default=50, ge=1, le=10000, strict=True)
    expected_revision_id: str | None = None


class AgentConfigRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_validation(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # 默认校验响应包含 input，可能带出完整指令或凭证；只返回已声明字段。
                issues = []
                for error in exc.errors():
                    location = error.get("loc", ())
                    field = location[1] if len(location) > 1 and location[1] in AgentPayload.model_fields else None
                    issues.append({"code": "agent_field_invalid", "field": field,
                                   "message": f"字段 {field} 缺失或格式不符合要求" if field else "请求格式不符合 Agent 配置要求",
                                   "suggestion": "请检查必填项及字段类型，修正后重新保存。"})
                raise HTTPException(422, detail={"code": "agent_config_invalid", "message": "Agent 配置字段校验失败", "issues": issues}) from exc
        return safe_validation


def register_agent_routes(router: APIRouter, app_state: Any) -> None:
    route_owner = getattr(router, "router", router)
    original_route_class = route_owner.route_class
    route_owner.route_class = AgentConfigRoute
    def service() -> Any:
        return app_state.agent_config_service

    def call(name: str, user_id: str, *args: Any) -> Any:
        target = getattr(service(), name)
        return target(user_id, *args) if hasattr(service(), "shared") else target(*args)

    def invoke(action: Any) -> JSONResponse:
        try:
            return JSONResponse(action())
        except AgentConfigError as exc:
            raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc), "issues": exc.issues}) from exc

    @router.get("/agents")
    async def list_agents(request: Request, status: Literal["active", "archived", "all"] = "active") -> JSONResponse:
        return JSONResponse({"agents": call("list", _user_id(request), status)})

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str, request: Request) -> JSONResponse:
        return invoke(lambda: call("get", _user_id(request), agent_id))

    @router.post("/agents", status_code=201)
    async def create_agent(payload: AgentPayload, request: Request) -> JSONResponse:
        return invoke(lambda: call("create", _user_id(request), payload.model_dump(exclude_unset=True)))

    @router.put("/agents/{agent_id}")
    async def update_agent(agent_id: str, payload: AgentPayload, request: Request) -> JSONResponse:
        return invoke(lambda: call("update", _user_id(request), agent_id, payload.model_dump(exclude_unset=True)))

    @router.post("/agents/{agent_id}/archive")
    async def archive_agent(agent_id: str, request: Request) -> JSONResponse:
        return invoke(lambda: call("archive", _user_id(request), agent_id))

    @router.post("/agents/{agent_id}/restore")
    async def restore_agent(agent_id: str, request: Request) -> JSONResponse:
        return invoke(lambda: call("restore", _user_id(request), agent_id))

    @router.get("/agent-capabilities")
    async def get_agent_capabilities(request: Request) -> JSONResponse:
        return JSONResponse(call("capabilities", _user_id(request)))

    route_owner.route_class = original_route_class


def _user_id(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.user_id if principal is not None else "legacy"
