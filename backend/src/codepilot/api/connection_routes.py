from __future__ import annotations

import asyncio
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from codepilot.session.agent_config import AgentConfigError
from codepilot.session.connections import ConnectionStore
from codepilot.tools.mcp import _open_session, _resolve_env


class ConnectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(max_length=200)
    credentials: dict[str, Annotated[str, StringConstraints(min_length=1, max_length=8192)]] = Field(max_length=50)
    expected_revision: str | None = Field(default=None, max_length=64)


class ConnectionRevision(BaseModel):
    expected_revision: str = Field(max_length=64)


def register_connection_routes(router: APIRouter, app_state: Any) -> None:
    def store():
        return ConnectionStore(app_state.workspace.codepilot_home, app_state.settings)

    async def invoke(method, *args):
        try:
            result = await asyncio.to_thread(method, *args)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.get("/connections")
    async def list_connections(request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), publication: str | None = None, tool: str | None = Query(None, max_length=100)) -> JSONResponse:
        def read():
            user = request.state.principal.user_id
            catalog = store().catalog(user)
            if tool:
                entry = store().entry(user, "tool:" + tool)
                if entry:
                    catalog[entry["target"]] = entry
            if publication:
                record = app_state.agent_config_service.publications.authorize(publication, user)
                for profile in record["manifest"]["profiles"].values():
                    for identity in profile.get("tool_ids", []):
                        entry = store().entry(user, "tool:" + identity)
                        if entry:
                            catalog[entry["target"]] = entry
                for resource in record["manifest"]["resources"]:
                    if resource["kind"] == "hook":
                        target = f"hook:public:{publication}:{resource['id']}"
                        entry = store().entry(user, target)
                        if entry:
                            catalog[target] = entry
            return {"catalog": [{key: value for key, value in entry.items() if key in {"target", "fields", "kind", "personal"}} for entry in catalog.values()],
                    "connections": store().list(user, offset, limit)}
        return await invoke(read)

    @router.post("/connections")
    async def create_connection(payload: ConnectionPayload, request: Request) -> JSONResponse:
        return await invoke(store().save, request.state.principal.user_id, None, payload.target, payload.credentials, None)

    @router.put("/connections/{connection_id}")
    async def update_connection(connection_id: str, payload: ConnectionPayload, request: Request) -> JSONResponse:
        return await invoke(store().save, request.state.principal.user_id, connection_id, payload.target, payload.credentials, payload.expected_revision)

    @router.post("/connections/{connection_id}/revoke")
    async def revoke_connection(connection_id: str, payload: ConnectionRevision, request: Request) -> JSONResponse:
        return await invoke(store().revoke, request.state.principal.user_id, connection_id, payload.expected_revision)

    @router.post("/connections/{connection_id}/validate")
    async def validate_connection(connection_id: str, request: Request) -> JSONResponse:
        try:
            record, credentials = store().resolve(request.state.principal.user_id, connection_id)
            secret = None if connection_id.startswith("team:") else credentials
            target = record["target"]
            async with asyncio.timeout(30):
                if target.startswith("mcp:"):
                    config = app_state.settings.mcp.servers[target[4:]]
                    async with _open_session(config, app_state.workspace.workspace_path, secret) as session:
                        await session.initialize()
                        await session.list_tools()
                elif target.startswith("tool:"):
                    # 只验证字段、归属和解密，不为验证连接执行用户代码。
                    store().resolve(request.state.principal.user_id, connection_id)
                else:
                    if target.startswith(("hook:personal:", "hook:public:")):
                        entry = store().entry(request.state.principal.user_id, target)
                        url, mapping = entry["url"], entry["mapping"]
                    else:
                        hook = next(item for item in app_state.settings.hooks.plugins if item.hook_id == target[5:])
                        url, mapping = hook.config["url"], hook.config.get("headers_from_env", {})
                    headers = _resolve_env(mapping, secret)
                    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                        response = await client.head(url, headers=headers)
                        response.raise_for_status()
            return JSONResponse({"valid": True, "revision": record["revision"]}, headers={"Cache-Control": "no-store"})
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(503, detail={"code": "connection_validation_failed", "message": "连接验证失败，请检查凭证或联系管理员"}) from exc
