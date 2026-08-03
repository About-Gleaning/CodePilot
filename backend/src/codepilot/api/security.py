from __future__ import annotations

"""本机单用户部署的 HTTP 安全边界。"""

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
    """拒绝非回环客户端，并为 API 响应补齐最小安全头。"""

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
        if origin and not _is_allowed_origin(origin):
            response = JSONResponse(
                status_code=403,
                content={"detail": {"code": "origin_not_allowed", "message": "请求 Origin 不在本机允许范围内。"}},
            )
            return _secure_response(response, request.url.path)
        response = await call_next(request)
        return _secure_response(response, request.url.path)


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
        path == "/api/config"
        or "/sessions/" in path and path.endswith("/replay")
        or path.startswith("/api/attachments/")
    )


def _is_allowed_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "http" and bool(parsed.hostname) and _is_loopback_hostname(parsed.hostname)


def _is_loopback_hostname(value: str) -> bool:
    return value.lower() == "localhost" or _is_loopback(value)
