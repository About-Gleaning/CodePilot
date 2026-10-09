from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from codepilot.session.agent_config import AgentConfigError
from codepilot.skills.store import SkillStore
from codepilot.skills import SkillRegistry


class SkillPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: dict[str, Annotated[str, StringConstraints(max_length=1400000)]] = Field(min_length=1, max_length=100)
    expected_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SkillRevision(BaseModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def register_skill_routes(router: APIRouter, app_state: Any) -> None:
    def store():
        return SkillStore(app_state.workspace.codepilot_home)

    def shared_registry():
        registry = SkillRegistry(app_state.skill_registry.skills_root)
        registry.discover()
        return registry

    async def invoke(method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except AgentConfigError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

    @router.get("/skills")
    async def list_skills(request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                          q: str = Query("", max_length=100), status: str = "all") -> JSONResponse:
        personal = await invoke(store().list, request.state.principal.user_id, 0, 100000)
        shared = [{"skill_id": f"shared:{skill.name}", "name": skill.name, "description": skill.description,
                   "visibility": "shared", "archived": False} for skill in (await invoke(shared_registry)).skills]
        def matches(item):
            return q.lower() in (item["name"] + item["description"]).lower() and (status == "all" or item["archived"] == (status == "archived"))
        personal, shared = list(filter(matches, personal)), list(filter(matches, shared))
        return JSONResponse({"skills": personal[offset:offset + limit], "shared": shared[offset:offset + limit], "offset": offset}, headers={"Cache-Control": "no-store"})

    def detail(user_id, skill_id):
        if not skill_id.startswith("shared:"):
            return store().get(user_id, skill_id)
        resolved = store().resolve(user_id, [skill_id], shared_registry()).skills[0]
        return {"skill_id": skill_id, "name": resolved.name,
                "description": resolved.description, "visibility": "shared", "archived": False,
                "files": store().shared_files(resolved)}

    @router.get("/skills/{skill_id}")
    async def get_skill(skill_id: str, request: Request):
        return JSONResponse(await invoke(detail, request.state.principal.user_id, skill_id), headers={"Cache-Control": "no-store"})

    @router.get("/skills/{skill_id}/references")
    async def references(skill_id: str, request: Request):
        from codepilot.api.hook_routes import resource_references
        await invoke(detail, request.state.principal.user_id, skill_id)
        records = await invoke(resource_references, app_state.agent_config_service, request.state.principal.user_id, "skill_ids", skill_id)
        return JSONResponse({"agents": records}, headers={"Cache-Control": "no-store"})

    @router.post("/skills")
    async def create_skill(payload: SkillPayload, request: Request) -> JSONResponse:
        record = await invoke(store().save, request.state.principal.user_id, None, payload.model_dump())
        return JSONResponse(record, status_code=201, headers={"Cache-Control": "no-store"})

    @router.put("/skills/{skill_id}")
    async def update_skill(skill_id: str, payload: SkillPayload, request: Request) -> JSONResponse:
        record = await invoke(store().save, request.state.principal.user_id, skill_id, payload.model_dump())
        return JSONResponse(record, headers={"Cache-Control": "no-store"})

    @router.post("/skills/{skill_id}/archive")
    async def archive_skill(skill_id: str, payload: SkillRevision, request: Request) -> JSONResponse:
        record = await invoke(store().archive, request.state.principal.user_id, skill_id, payload.expected_revision)
        return JSONResponse(record, headers={"Cache-Control": "no-store"})

    @router.get("/skills/{skill_id}/files")
    async def get_skill_file(skill_id: str, request: Request, path: str = "SKILL.md", revision: str | None = None) -> Response:
        def read():
            if skill_id.startswith("shared:"):
                skill = store().resolve(request.state.principal.user_id, [skill_id], shared_registry()).skills[0]
                return store().read_shared_path(skill.path, path)
            return store().read(request.state.principal.user_id, skill_id, path, expected_revision=revision)
        raw = await invoke(read)
        return Response(raw, media_type="application/octet-stream", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": "attachment",
        })
