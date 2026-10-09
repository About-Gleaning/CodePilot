from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from codepilot.memory.long_memory import LongMemoryError, memory_snapshot, save_memory_snapshot
from codepilot.session.agent_config import AgentConfigError


class MemoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=100_000)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def register_memory_routes(router: APIRouter, app_state: Any) -> None:
    def location(request: Request, memory_id: str):
        user_id = request.state.principal.user_id
        if memory_id != "_global":
            try:
                app_state.agent_config_service.get(user_id, memory_id)
            except AgentConfigError as exc:
                raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
        return app_state.workspace.for_user(user_id).user_home_dir

    @router.get("/memories/{memory_id}")
    async def get_memory(memory_id: str, request: Request) -> JSONResponse:
        home = location(request, memory_id)
        return JSONResponse(await asyncio.to_thread(memory_snapshot, home, memory_id), headers={"Cache-Control": "no-store"})

    @router.put("/memories/{memory_id}")
    async def put_memory(memory_id: str, payload: MemoryPayload, request: Request) -> JSONResponse:
        home = location(request, memory_id)
        try:
            result = await asyncio.to_thread(save_memory_snapshot, home, memory_id, payload.content, payload.expected_revision)
        except LongMemoryError as exc:
            status = 409 if exc.error_type == "LongMemoryRevisionConflict" else 422
            raise HTTPException(status, detail={"code": exc.error_type, "message": str(exc)}) from exc
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
