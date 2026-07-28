from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from codepilot.session.attachments import AttachmentError, SUPPORTED_IMAGE_MIMES, attachment_message_dir, detect_image_mime, sanitize_attachment_filename

_SKIPPED_FILE_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "dist", "node_modules"}


def register_workspace_routes(router: APIRouter, app_state: Any) -> None:
    @router.get("/config")
    async def get_config() -> JSONResponse:
        settings = app_state.settings
        providers = [{"provider": item.provider, "label": item.label, "models": item.models, "model_capabilities": {model: {"thinking": config.thinking.model_dump() if config.thinking else None} for model, config in item.model_settings.items()}} for item in settings.llm_runtime.activated_providers.values()]
        return JSONResponse({"workspace_id": app_state.workspace.workspace_id, "workspace_path": str(app_state.workspace.workspace_path), "codepilot_home": str(app_state.workspace.codepilot_home), "activated_providers": providers, "agents": [name for name, profile in app_state.agent_profiles.items() if getattr(profile, "kind", "agent") == "agent"], "skills": app_state.skill_registry.list_briefs() if hasattr(app_state, "skill_registry") else [], "sse": settings.sse.model_dump()})

    @router.get("/workspace/files")
    async def get_workspace_files(q: str = "", limit: int = Query(default=40, ge=1, le=80)) -> JSONResponse:
        return JSONResponse({"files": await asyncio.to_thread(list_workspace_files, app_state.workspace.workspace_path, q, limit)})

    @router.get("/attachments/{session_id}/{message_id}/{filename}")
    async def get_attachment(session_id: str, message_id: str, filename: str) -> FileResponse:
        try:
            safe_filename = sanitize_attachment_filename(filename)
            if safe_filename != filename:
                raise AttachmentError("附件文件名非法。")
            target_dir = attachment_message_dir(app_state.workspace, session_id, message_id).resolve()
            target = (target_dir / safe_filename).resolve(strict=False)
            if not target.is_relative_to(target_dir) or not target.is_file():
                raise AttachmentError("附件不存在。")
        except AttachmentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with target.open("rb") as file:
            media_type = detect_image_mime(file.read(16))
        if media_type not in SUPPORTED_IMAGE_MIMES:
            raise HTTPException(status_code=415, detail="附件类型不支持预览")
        return FileResponse(target, media_type=media_type, filename=target.name)


def list_workspace_files(workspace_path: Path, query: str, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    stack = [workspace_path]
    query = query.strip().lower()
    scanned = 0
    while stack and len(results) < limit and scanned < max(limit * 80, 2000):
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in sorted(entries, key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.lower())):
                    if entry.is_dir(follow_symlinks=False) and entry.name not in _SKIPPED_FILE_DIRS:
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        scanned += 1
                        path = str(Path(entry.path).relative_to(workspace_path))
                        if not query or query in path.lower():
                            results.append({"path": path})
                        if len(results) >= limit:
                            break
        except OSError:
            continue
    return results
