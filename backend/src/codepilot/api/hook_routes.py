from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from codepilot.api.skill_routes import SkillRevision
from codepilot.hooks.store import HookPayload, HookStore
from codepilot.hooks.testing import HookTestService, TestPayload
from codepilot.session.agent_config import AgentConfigError


def resource_references(service, user_id, field, identity):
    # 只读取当前配置，引用查询不扫描 Session 或历史 revision。
    records = service.shared.list() + service._private_service(user_id).list()
    result = []
    for record in records:
        source = service._private_service(user_id) if record["visibility"] == "private" else service.shared
        detail = source.get(record["agent_id"])
        selected = detail.get(field)
        legacy = selected is None and not identity.startswith("personal:") and (field != "skill_ids" or identity.startswith("shared:"))
        if identity in (selected or []) or legacy:
            result.append({"agent_id": record["agent_id"], "name": record["name"]})
    return result


def register_hook_routes(router: APIRouter, app_state: Any) -> None:
    store = HookStore(app_state.workspace.codepilot_home)
    tests = HookTestService(app_state.workspace, app_state.settings)
    app_state.hook_tests = tests

    async def invoke(method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(422, detail="Hook 配置无效") from exc

    def response(value, status=200):
        return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

    @router.get("/hooks")
    async def listing(request: Request, q: str = Query("", max_length=100), status: str = "active",
                      offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        records = await invoke(store.list, request.state.principal.user_id)
        records += [{"hook_id": item.hook_id, "name": item.hook_id, "description": "", "visibility": "platform",
                     "plugin_type": item.plugin_type, "hook_type": item.hook_type, "archived": not item.enabled}
                    for item in app_state.settings.hooks.plugins]
        records = [item for item in records if q.lower() in (item["name"] + item["description"]).lower()
                   and (status == "all" or item["archived"] == (status == "archived"))]
        return response({"hooks": records[offset:offset + limit], "total": len(records)})

    @router.post("/hooks")
    async def create(payload: HookPayload, request: Request):
        return response(await invoke(store.save, request.state.principal.user_id, None, payload), 201)

    @router.get("/hooks/{identity}")
    async def detail(identity: str, request: Request):
        if not identity.startswith("personal:"):
            plugin = next((item for item in app_state.settings.hooks.plugins if item.hook_id == identity), None)
            if plugin is None:
                raise HTTPException(404, detail="Hook 不存在")
            # 平台详情不能泄漏命令、地址或凭证映射；复制时由用户补充执行配置。
            return response({"hook_id": identity, "visibility": "platform", "definition": {
                "name": identity, "description": "", "plugin_type": plugin.plugin_type, "hook_type": plugin.hook_type,
                "parameters": {key: value.model_dump() for key, value in plugin.parameters.items()},
                "order": plugin.order, "on_error": plugin.on_error, "timeout_seconds": plugin.timeout_seconds,
            }, "files": {}})
        record = await invoke(store.get, request.state.principal.user_id, identity)
        return response(await invoke(store.load, request.state.principal.user_id, identity, record["version"]))

    @router.put("/hooks/{identity}")
    async def update(identity: str, payload: HookPayload, request: Request):
        return response(await invoke(store.save, request.state.principal.user_id, identity, payload))

    @router.post("/hooks/{identity}/archive")
    async def archive(identity: str, payload: SkillRevision, request: Request):
        return response(await invoke(store.archive, request.state.principal.user_id, identity, payload.expected_revision))

    @router.get("/hooks/{identity}/files")
    async def file(identity: str, request: Request, version: str, path: str):
        raw = await invoke(store.read, request.state.principal.user_id, identity, version, path)
        return Response(raw, media_type="application/octet-stream", headers={"Cache-Control": "no-store",
                        "Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})

    @router.get("/hooks/{identity}/references")
    async def references(identity: str, request: Request):
        await detail(identity, request)
        return response({"agents": await invoke(resource_references, app_state.agent_config_service,
                                                  request.state.principal.user_id, "hook_ids", identity)})

    @router.post("/hooks/{identity}/tests")
    async def test(identity: str, payload: TestPayload, request: Request):
        try:
            return response(await tests.start(request.state.principal.user_id, identity, payload), 202)
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(422, detail="测试参数无效") from exc

    @router.get("/hook-tests/{identity}")
    async def test_status(identity: str, request: Request):
        try:
            return response(await tests.get(request.state.principal.user_id, identity))
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail=str(exc)) from exc

    @router.post("/hook-tests/{identity}/cancel")
    async def test_cancel(identity: str, request: Request):
        try:
            return response(await tests.cancel(request.state.principal.user_id, identity))
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail=str(exc)) from exc
