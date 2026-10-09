"""公共 Agent 管理入口，执行仍交由现有 Manager。"""

import asyncio
import base64
import sqlite3
from typing import Annotated, Any

from fastapi import HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from codepilot.session.agent_config import AgentConfigError
from codepilot.session.publications import check_public_content, public_identity
from codepilot.skills.store import SkillStore
from codepilot.hooks.store import HookStore, HookPayload
from codepilot.api.skill_routes import SkillPayload


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_agent_id: str = Field(max_length=64)
    include_dependencies: StrictBool = False


class Confirm(BaseModel):
    candidate_id: str = Field(max_length=64)
    digest: str = Field(max_length=64)
    expected_revision: str | None = Field(default=None, max_length=64)
    client_request_id: str = Field(min_length=1, max_length=100)


class Revision(BaseModel):
    expected_revision: str = Field(max_length=64)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str | None = Field(default=None, max_length=64)
    working_directory: str | None = Field(default=None, max_length=4096)
    connections: dict[Annotated[str, StringConstraints(max_length=64)], Annotated[list[Annotated[str, StringConstraints(max_length=200)]], Field(max_length=100)]] = Field(default_factory=dict, max_length=100)


class PublicationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise HTTPException(422, detail={"code": "publication_input_invalid", "message": "发布请求字段或文件格式无效，请检查输入"}) from exc
        return safe


def register_publication_routes(router, app_state: Any):
    original_route_class = router.route_class
    router.route_class = PublicationRoute
    def service():
        return app_state.agent_config_service

    def store():
        return service().publications

    async def invoke(action):
        try:
            value = await asyncio.to_thread(action)
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), "issues": exc.issues,
                "progress": getattr(exc, "progress", []), "retryable": getattr(exc, "retryable", False)}) from exc
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, detail={"code": "publication_input_invalid", "message": "发布内容格式无效，请检查资源及文件编码"}) from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, detail={"code": "publication_storage_error", "message": "公共资源保存失败，请稍后重试或联系管理员", "retryable": True}) from exc

    @router.get("/agent-publications")
    async def listing(request: Request, q: str = Query("", max_length=100), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return await invoke(lambda: {"publications": store().list(request.state.principal.user_id, q, offset, limit)})

    @router.post("/agent-publications/preview")
    async def preview(payload: Preview, request: Request):
        principal = request.state.principal
        return await invoke(lambda: store().preview(service(), principal.user_id, principal.username, payload.source_agent_id, payload.include_dependencies))

    @router.post("/agent-publications/confirm")
    async def confirm(payload: Confirm, request: Request):
        return await invoke(lambda: store().confirm(service(), request.state.principal.user_id, payload.candidate_id, payload.digest, payload.expected_revision, payload.client_request_id))

    @router.get("/agent-publications/{identity}")
    async def detail(identity: str, request: Request):
        return await invoke(lambda: {**store().view(store().authorize(identity, request.state.principal.user_id), request.state.principal.user_id, detail=True), "can_moderate": request.state.principal.role == "admin"})

    @router.post("/agent-publications/{identity}/withdraw")
    async def withdraw(identity: str, payload: Revision, request: Request):
        return await invoke(lambda: store().change_status(identity, request.state.principal.user_id, payload.expected_revision))

    @router.post("/agent-publications/{identity}/block")
    async def block(identity: str, payload: Revision, request: Request):
        if request.state.principal.role != "admin":
            raise HTTPException(403, detail={"code": "forbidden", "message": "需要管理员权限"})
        return await invoke(lambda: store().change_status(identity, request.state.principal.user_id, payload.expected_revision, blocked=True))

    @router.get("/agent-publications/{identity}/usage")
    async def usage(identity: str, request: Request):
        def read():
            store().authorize(identity, request.state.principal.user_id)
            return store().settings(request.state.principal.user_id, identity)
        return await invoke(read)

    @router.put("/agent-publications/{identity}/usage")
    async def save_usage(identity: str, payload: Usage, request: Request):
        def save():
            user = request.state.principal.user_id
            publication = store().authorize(identity, user)
            if payload.working_directory:
                from codepilot.session.working_directory import resolve_working_directory
                resolve_working_directory(app_state.settings, app_state.workspace.workspace_path, payload.working_directory)
            from codepilot.session.connections import ConnectionStore
            connections = ConnectionStore(store().home, app_state.settings)
            for agent, selected in payload.connections.items():
                if agent not in publication["manifest"]["profiles"] or len(selected) > 100:
                    raise AgentConfigError("使用设置引用了无关 Agent")
                for connection in selected:
                    connections.resolve(user, connection)
            return store().save_settings(user, identity, payload.model_dump(exclude={"expected_revision"}), payload.expected_revision)
        return await invoke(save)

    def resource(identity, resource_id, user, write=False):
        publication = store().authorize(identity, user, write=write)
        item = next((r for r in publication["manifest"]["resources"] if r["id"] == resource_id), None)
        if item is None:
            raise AgentConfigError("附属资源不存在", status=404)
        return item

    @router.get("/agent-publications/{identity}/resources/{resource_id}")
    async def get_resource(identity: str, resource_id: str, request: Request):
        def read():
            user = request.state.principal.user_id
            item = resource(identity, resource_id, user)
            if item["kind"] == "skill":
                return {**SkillStore(store().home, publication_id=identity).get(user, resource_id), "kind": "skill", "visibility": "public", "skill_id": public_identity(identity, resource_id)}
            result = HookStore(store().home, publication_id=identity).load(user, "personal:" + resource_id)
            return {**result, "kind": "hook", "visibility": "public", "hook_id": public_identity(identity, resource_id)}
        return await invoke(read)

    @router.get("/agent-publications/{identity}/resources/{resource_id}/files")
    async def file(identity: str, resource_id: str, request: Request, path: str = Query(max_length=500), revision: str | None = Query(None, max_length=64), version: str | None = Query(None, max_length=64)):
        def read():
            user = request.state.principal.user_id
            item = resource(identity, resource_id, user)
            if item["kind"] == "skill":
                return SkillStore(store().home, publication_id=identity).read(user, resource_id, path, expected_revision=revision)
            hooks = HookStore(store().home, publication_id=identity)
            hook = hooks.load(user, "personal:" + resource_id)
            return hooks.read(user, "personal:" + resource_id, version or hook["version"], path)
        try:
            raw = await asyncio.to_thread(read)
            return Response(raw, media_type="application/octet-stream", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": "attachment"})
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.put("/agent-publications/{identity}/skills/{resource_id}")
    async def save_skill(identity: str, resource_id: str, payload: SkillPayload, request: Request):
        def save():
            user = request.state.principal.user_id
            for name, value in payload.files.items():
                check_public_content(base64.b64decode(value, validate=True).decode("utf-8", errors="replace"), name)
            with store().db() as db:
                # 下架、重新发布和作者写入共用短事务，校验通过后身份不能被并发替换。
                db.execute("BEGIN IMMEDIATE")
                if resource(identity, resource_id, user, True)["kind"] != "skill":
                    raise AgentConfigError("资源类型不匹配")
                return SkillStore(store().home, publication_id=identity).save(user, resource_id, payload.model_dump())
        return await invoke(save)

    @router.put("/agent-publications/{identity}/hooks/{resource_id}")
    async def save_hook(identity: str, resource_id: str, payload: HookPayload, request: Request):
        def save():
            user = request.state.principal.user_id
            check_public_content(payload.definition.model_dump(mode="json"))
            for name, value in payload.files.items():
                check_public_content(base64.b64decode(value, validate=True).decode("utf-8", errors="replace"), name)
            with store().db() as db:
                db.execute("BEGIN IMMEDIATE")
                if resource(identity, resource_id, user, True)["kind"] != "hook":
                    raise AgentConfigError("资源类型不匹配")
                return HookStore(store().home, publication_id=identity).save(user, "personal:" + resource_id, payload)
        return await invoke(save)

    router.route_class = original_route_class
