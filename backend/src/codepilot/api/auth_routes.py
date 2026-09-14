from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from codepilot.api.security import csrf_cookie_name, session_cookie_name


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


def register_auth_routes(router: APIRouter, app_state: Any) -> None:
    @router.post("/auth/login")
    async def login(payload: LoginRequest, request: Request) -> JSONResponse:
        if not app_state.auth_store.has_users():
            raise HTTPException(
                status_code=503,
                detail={"code": "auth_not_initialized", "message": "请先使用管理员 CLI 初始化账号。"},
            )
        result = await app_state.auth_service.login(
            payload.username,
            payload.password,
            client_key=_client_key(request, app_state.settings.auth),
        )
        if result is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_credentials", "message": "用户名或密码错误。"},
            )
        principal, tokens = result
        secure = app_state.settings.auth.mode == "lan_https"
        max_age = max(0, tokens.absolute_expires_at - int(__import__("time").time()))
        response = JSONResponse({"user": principal.model_dump(mode="json")})
        response.set_cookie(
            session_cookie_name(app_state.settings.auth.mode),
            tokens.session_token,
            max_age=max_age,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/",
        )
        response.set_cookie(
            csrf_cookie_name(app_state.settings.auth.mode),
            tokens.csrf_token,
            max_age=max_age,
            httponly=False,
            secure=secure,
            samesite="strict",
            path="/",
        )
        return response

    @router.post("/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        authenticated = request.state.authenticated_session
        await app_state.auth_service.logout(
            authenticated.session_id,
            actor_user_id=authenticated.principal.user_id,
        )
        response = JSONResponse({"ok": True})
        response.delete_cookie(session_cookie_name(app_state.settings.auth.mode), path="/")
        response.delete_cookie(csrf_cookie_name(app_state.settings.auth.mode), path="/")
        return response

    @router.get("/auth/me")
    async def me(request: Request) -> JSONResponse:
        return JSONResponse({"user": request.state.principal.model_dump(mode="json")})


def _client_key(request: Request, auth_settings: Any) -> str:
    direct = request.client.host if request.client else "unknown"
    if auth_settings.mode != "lan_https":
        return direct
    try:
        trusted = str(ipaddress.ip_address(direct)) in auth_settings.trusted_proxy_addresses
    except ValueError:
        trusted = False
    if not trusted:
        return direct
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if not forwarded or len(forwarded) > 64:
        return direct
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return direct
