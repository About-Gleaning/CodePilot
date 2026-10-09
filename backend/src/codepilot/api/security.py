from __future__ import annotations

"""回环后端与可信 HTTPS 代理入口的 HTTP 安全边界。"""

import ipaddress
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

_ALLOWED_CLIENT_ADDRESSES = {
    ipaddress.ip_address("127.0.0.1"),
    ipaddress.ip_address("::1"),
}


class LocalAccessMiddleware(BaseHTTPMiddleware):
    """后端只接受回环直连；局域网请求必须由同机 HTTPS 代理转发。"""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host):
            response = JSONResponse(
                status_code=403,
                content={"detail": {"code": "local_access_only", "message": "CodePilot 仅允许本机访问。"}},
            )
            return _secure_response(response, request.url.path)
        origin = request.headers.get("origin")
        context = getattr(request.app.state, "context", None)
        auth_settings = getattr(getattr(context, "settings", None), "auth", None)
        mode = getattr(auth_settings, "mode", "local_dev")
        public_origins = _public_origins(auth_settings)
        if origin and not _is_allowed_origin(origin, mode=mode, public_origins=public_origins):
            response = JSONResponse(
                status_code=403,
                content={"detail": {"code": "origin_not_allowed", "message": "请求 Origin 不在本机允许范围内。"}},
            )
            return _secure_response(response, request.url.path)
        response = await call_next(request)
        return _secure_response(response, request.url.path)


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """为所有非公开 API 建立可信 Principal，并统一执行写请求 CSRF 校验。"""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        context = request.app.state.context
        if not context.settings.auth.enabled or request.method == "OPTIONS":
            return await call_next(request)
        if _is_public_request(request):
            if request.url.path == "/api/auth/login" and request.method == "POST":
                rejected = _validate_login_request(request)
                if rejected is not None:
                    return rejected
            return await call_next(request)

        cookie_name = session_cookie_name(context.settings.auth.mode)
        authenticated = await context.auth_service.authenticate(request.cookies.get(cookie_name, ""))
        if authenticated is None:
            return _auth_error(401, "authentication_required", "登录已失效，请重新登录。")
        request.state.authenticated_session = authenticated
        request.state.principal = authenticated.principal

        if (
            getattr(context, "migration_required", False)
            and not request.url.path.startswith("/api/auth/")
            and request.method not in {"GET", "HEAD", "OPTIONS"}
        ):
            return _auth_error(503, "migration_required", "旧数据尚未完成多用户迁移，副作用请求已禁用。")

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin", "")
            if not origin or not _is_allowed_origin(
                origin,
                mode=context.settings.auth.mode,
                public_origins=_public_origins(context.settings.auth),
            ):
                return _auth_error(403, "origin_invalid", "写请求 Origin 校验失败。")
            csrf_cookie = request.cookies.get(csrf_cookie_name(context.settings.auth.mode), "")
            csrf_header = request.headers.get("x-codepilot-csrf", "")
            if not context.auth_store.validate_csrf(
                authenticated,
                cookie_token=csrf_cookie,
                header_token=csrf_header,
            ):
                return _auth_error(403, "csrf_invalid", "CSRF 校验失败。")
        return await call_next(request)


def _secure_response(response: Response, path: str) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if _must_not_cache(path):
        response.headers["Cache-Control"] = "no-store"
    return response


def _is_loopback(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address in _ALLOWED_CLIENT_ADDRESSES


def _must_not_cache(path: str) -> bool:
    return (
        path.startswith("/api/auth/")
        or path == "/api/config"
        or "/sessions/" in path and path.endswith("/replay")
        or path.startswith("/api/attachments/")
    )


def _public_origins(settings: Any) -> set[str]:
    return {origin.rstrip("/") for origin in [getattr(settings, "public_origin", None), *getattr(settings, "public_origins", [])] if origin}


def _is_allowed_origin(value: str, *, mode: str = "local_dev", public_origins: set[str] | None = None) -> bool:
    normalized = value.rstrip("/")
    if mode == "lan_https":
        return normalized in (public_origins or set())
    parsed = urlsplit(value)
    return parsed.scheme == "http" and bool(parsed.hostname) and _is_loopback_hostname(parsed.hostname)


def _is_loopback_hostname(value: str) -> bool:
    return value.lower() == "localhost" or _is_loopback(value)


def session_cookie_name(mode: str) -> str:
    return "__Host-codepilot_session" if mode == "lan_https" else "codepilot_dev_session"


def csrf_cookie_name(mode: str) -> str:
    return "__Host-codepilot_csrf" if mode == "lan_https" else "codepilot_dev_csrf"


def _is_public_request(request: Request) -> bool:
    path = request.url.path
    if path in {"/api/health/live", "/api/health/ready", "/api/auth/login"}:
        return True
    return request.method == "POST" and path.startswith("/api/schedule-runs/") and path.endswith(("/report", "/tool"))


def _validate_login_request(request: Request) -> JSONResponse | None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return _auth_error(415, "content_type_invalid", "登录只接受 application/json。")
    if not request.headers.get("origin"):
        return _auth_error(403, "origin_required", "登录请求缺少 Origin。")
    return None


def _auth_error(status: int, code: str, message: str) -> JSONResponse:
    response = JSONResponse(status_code=status, content={"detail": {"code": code, "message": message}})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response
