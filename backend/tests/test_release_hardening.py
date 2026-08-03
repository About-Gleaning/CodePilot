from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from codepilot.api.health_routes import register_health_routes
from codepilot.api.security import LocalAccessMiddleware
from codepilot.api.session_routes import InteractionReplyRequest, StartRunRequest, _safe_replay
from codepilot.config import load_settings
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.logging.setup import _redact_value
from codepilot.session.attachments import MAX_IMAGE_ATTACHMENT_BASE64_CHARS


def _secured_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/config")
    async def config() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(LocalAccessMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r"^http://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


@pytest.mark.asyncio
async def test_local_access_security_boundary() -> None:
    app = _secured_app()
    local_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=local_transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/config", headers={"Origin": "http://localhost:5173"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"

        hostile_origin = await client.get("/api/config", headers={"Origin": "https://example.com"})
        assert hostile_origin.status_code == 403
        assert hostile_origin.json()["detail"]["code"] == "origin_not_allowed"
        assert hostile_origin.headers["x-frame-options"] == "DENY"
        assert hostile_origin.headers["cache-control"] == "no-store"

        local_https_origin = await client.get("/api/config", headers={"Origin": "https://localhost:5173"})
        assert local_https_origin.status_code == 403
        assert local_https_origin.json()["detail"]["code"] == "origin_not_allowed"

        hostile_host = await client.get("/api/config", headers={"Host": "example.com"})
        assert hostile_host.status_code == 400

    remote_transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 5000))
    async with httpx.AsyncClient(transport=remote_transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/config")
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "local_access_only"

    other_loopback_transport = httpx.ASGITransport(app=app, client=("127.0.0.2", 5000))
    async with httpx.AsyncClient(transport=other_loopback_transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/config")
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "local_access_only"


def test_request_limits_reject_oversized_content() -> None:
    with pytest.raises(ValueError):
        StartRunRequest(content="x" * 100_001, client_request_id="request_1")
    with pytest.raises(ValueError):
        GatewayInput(
            type=GatewayInputType.USER_MESSAGE,
            agent_name="build",
            content="x" * 100_001,
        )


def test_request_limits_reject_oversized_attachments_and_interactions() -> None:
    attachment = {"filename": "image.png", "mime": "image/png", "data_base64": "AAAA"}
    with pytest.raises(ValueError):
        StartRunRequest(
            content="test",
            client_request_id="request_1",
            attachments=[attachment] * 5,
        )
    with pytest.raises(ValueError):
        StartRunRequest(
            content="test",
            client_request_id="request_1",
            attachments=[
                {
                    **attachment,
                    "data_base64": "A" * (MAX_IMAGE_ATTACHMENT_BASE64_CHARS + 1),
                }
            ],
        )
    with pytest.raises(ValueError):
        InteractionReplyRequest(
            type=GatewayInputType.HUMAN_REPLY,
            approved=True,
            comment="x" * 2_001,
        )
    with pytest.raises(ValueError):
        GatewayInput(
            type=GatewayInputType.QUESTION_REPLY,
            question_id="question_1",
            answers={"answer": "x" * 100_001},
        )


def test_codepilot_home_environment_override(tmp_path: Path) -> None:
    settings = load_settings(
        Path(__file__).parents[1] / "config.yaml",
        environ={
            "DEEPSEEK_API_KEY": "test",
            "CODEPILOT_HOME": str(tmp_path / "isolated-home"),
        },
    )
    assert settings.storage.codepilot_home == str(tmp_path / "isolated-home")
    assert list(settings.llm_runtime.activated_providers) == ["deepseek"]


def test_recursive_log_redaction_hides_credentials_and_home() -> None:
    home = str(Path.home())
    payload = {
        "authorization": "Bearer demo",
        "nested": [
            f"Bearer sample {home}/project",
            "https://user:password@example.com/path",
            "data:image/png;base64,AAAA",
        ],
    }
    redacted = str(_redact_value(payload))
    assert "demo" not in redacted
    assert "sample" not in redacted
    assert "user:password" not in redacted
    assert "AAAA" not in redacted
    assert home not in redacted


def test_resource_replay_excludes_internal_records_and_paths() -> None:
    payload = _safe_replay(
        {
            "session": {
                "record_type": "session_started",
                "session_id": "session_1",
                "created_at": "2026-07-31T00:00:00Z",
                "data": {
                    "session_id": "session_1",
                    "agent_id": "agent_1",
                    "workspace_path": "/private/user/project",
                    "title": "测试",
                },
            },
            "messages": [
                {
                    "info": {"path": {"cwd": str(Path.home() / "project"), "root": "/private/user/project"}},
                    "parts": [{"type": "file", "source": {"type": "file", "value": str(Path.home() / "secret.txt")}}],
                }
            ],
            "records": [{"secret": "internal"}],
            "latest_event_seq": 4,
            "runtime": {"status": "COMPLETED"},
        }
    )
    assert "records" not in payload
    assert "workspace_path" not in payload["session"]["data"]
    assert "/private/user" not in str(payload)
    assert str(Path.home()) not in str(payload)


@pytest.mark.asyncio
async def test_health_readiness_reports_safe_component_state(tmp_path: Path) -> None:
    class Runtime:
        error_code: str | None = None

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "recovered": True,
                "error_code": self.error_code,
                "active_run_count": 0,
                "started_agent_count": 0,
            }

    runtime = Runtime()
    app_state = SimpleNamespace(
        agent_runtime=runtime,
        settings=SimpleNamespace(llm_runtime=SimpleNamespace(activated_providers={"deepseek": object()})),
        workspace=SimpleNamespace(workspace_dir=tmp_path),
        mcp_manager=SimpleNamespace(list_server_capabilities=lambda: []),
    )
    app = FastAPI()
    from fastapi import APIRouter

    router = APIRouter(prefix="/api")
    register_health_routes(router, app_state)
    app.include_router(router)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        live = await client.get("/api/health/live")
        ready = await client.get("/api/health/ready")
        app_state.mcp_manager = SimpleNamespace(
            list_server_capabilities=lambda: [{"name": "fixture", "status": "unavailable"}]
        )
        degraded = await client.get("/api/health/ready")
        runtime.error_code = "runtime_recovery_incomplete"
        not_ready = await client.get("/api/health/ready")
    assert live.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "degraded"
    assert not_ready.status_code == 503
    assert not_ready.json()["error_code"] == "runtime_recovery_incomplete"
    assert "/private/" not in str(ready.json())
