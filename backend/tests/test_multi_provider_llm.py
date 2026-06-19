from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from codepilot.api import build_api_router
from codepilot.config import AppSettings, build_llm_runtime_settings, load_settings, resolve_llm_selection
from codepilot.config.settings import LLMProviderSettings, LLMSettings
from codepilot.events import StreamEvent
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.session import Message, SessionRunner, SessionState, SessionStatus, TextPart, build_user_message_info
from codepilot.session.message import FilePart


class DummyEventBus:
    async def publish_domain_event(self, event: object) -> None:
        return None

    async def publish_stream_event(self, event: object) -> None:
        return None


class DummyAgentLoop:
    async def run(self, **kwargs: object) -> SessionState:
        session = kwargs["session"]
        assert isinstance(session, SessionState)
        session.status = SessionStatus.COMPLETED
        return session


class NoopTitleService:
    async def generate_for_session(self, session: SessionState, event_bus: object) -> None:
        return None


def build_settings(environ: dict[str, str]) -> AppSettings:
    llm_settings = LLMSettings(
        providers={
            "openai": LLMProviderSettings(
                label="OpenAI",
                models=[
                    {
                        "id": "gpt-5.3-codex",
                        "thinking": {
                            "kind": "reasoning_effort",
                            "allowed_values": ["low", "medium", "high"],
                            "default_value": "medium",
                        },
                    },
                    "gpt-4.1",
                ],
            ),
            "qwen": LLMProviderSettings(
                label="Qwen",
                models=[
                    "qwen-plus",
                    "qwen-max",
                    {
                        "id": "qwen3.5-flash",
                        "thinking": {
                            "kind": "extra_body_boolean",
                            "extra_body_key": "enable_thinking",
                            "allowed_values": ["on", "off"],
                            "default_value": "on",
                        },
                    },
                ],
                litellm_model_prefix="openai/",
            ),
            "deepseek": LLMProviderSettings(
                label="DeepSeek",
                models=["deepseek-v4-flash", "deepseek-v4-pro"],
                litellm_model_prefix="openai/",
            ),
        }
    )
    settings = AppSettings(llm=llm_settings)
    runtime = build_llm_runtime_settings(settings.llm, environ=environ)
    return settings.model_copy(update={"llm_runtime": runtime})


def build_session_runner(settings: AppSettings, *, allow_human_interaction: bool = True) -> SessionRunner:
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    return SessionRunner(
        workspace=workspace,
        config=settings,
        event_bus=DummyEventBus(),
        hook_manager=None,
        agent_loop=DummyAgentLoop(),
        agent_profiles={"build": object(), "plan": object()},
        title_service=NoopTitleService(),
        allow_human_interaction=allow_human_interaction,
    )


def test_build_llm_runtime_settings_activates_only_complete_provider() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai", "QWEN_API_KEY": "sk-qwen"})

    assert list(settings.llm_runtime.activated_providers) == ["openai"]
    assert settings.llm_runtime.activated_providers["openai"].models == ["gpt-5.3-codex", "gpt-4.1"]


def test_build_llm_runtime_settings_activates_deepseek_with_api_key() -> None:
    settings = build_settings({"DEEPSEEK_API_KEY": "sk-deepseek"})

    assert list(settings.llm_runtime.activated_providers) == ["deepseek"]
    deepseek = settings.llm_runtime.activated_providers["deepseek"]
    assert deepseek.models == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert deepseek.litellm_model_prefix == "openai/"


def test_llm_provider_settings_accepts_legacy_string_models() -> None:
    provider = LLMProviderSettings(label="OpenAI", models=["gpt-4.1"])

    assert provider.models[0].id == "gpt-4.1"
    assert provider.models[0].thinking is None


def test_llm_provider_settings_validates_thinking_values() -> None:
    with pytest.raises(ValueError, match="default_value 必须属于 allowed_values"):
        LLMProviderSettings(
            label="OpenAI",
            models=[
                {
                    "id": "gpt-test",
                    "thinking": {
                        "kind": "reasoning_effort",
                        "allowed_values": ["low"],
                        "default_value": "medium",
                    },
                }
            ],
        )

    with pytest.raises(ValueError, match="reasoning_effort 只能使用"):
        LLMProviderSettings(
            label="OpenAI",
            models=[
                {
                    "id": "gpt-test",
                    "thinking": {
                        "kind": "reasoning_effort",
                        "allowed_values": ["on"],
                        "default_value": "on",
                    },
                }
            ],
        )


def test_resolve_llm_selection_requires_explicit_provider_and_model() -> None:
    settings = build_settings(
        {
            "OPENAI_API_KEY": "sk-openai",
            "QWEN_API_KEY": "sk-qwen",
            "QWEN_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }
    )

    with pytest.raises(ValueError, match="必须显式传入 provider"):
        resolve_llm_selection(settings, requested_provider=None, requested_model="qwen-plus")

    with pytest.raises(ValueError, match="必须显式传入 model"):
        resolve_llm_selection(settings, requested_provider="qwen", requested_model=None)

    with pytest.raises(ValueError, match="不属于"):
        resolve_llm_selection(settings, requested_provider="openai", requested_model="qwen-plus")

    activated_provider, selected_model = resolve_llm_selection(
        settings,
        requested_provider="qwen",
        requested_model="qwen-plus",
    )
    assert activated_provider.provider == "qwen"
    assert selected_model == "qwen-plus"


def test_resolve_llm_selection_supports_deepseek_models() -> None:
    settings = build_settings({"DEEPSEEK_API_KEY": "sk-deepseek"})

    activated_provider, selected_model = resolve_llm_selection(
        settings,
        requested_provider="deepseek",
        requested_model="deepseek-v4-pro",
    )

    assert activated_provider.provider == "deepseek"
    assert selected_model == "deepseek-v4-pro"

    with pytest.raises(ValueError, match="不属于"):
        resolve_llm_selection(settings, requested_provider="deepseek", requested_model="gpt-5.3-codex")


def test_session_runner_status_snapshot_has_no_default_llm() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    snapshot = runner.get_status_snapshot()

    assert snapshot["provider"] is None
    assert snapshot["model"] is None


def test_config_route_returns_models_without_default_fields() -> None:
    settings = build_settings(
        {
            "OPENAI_API_KEY": "sk-openai",
            "QWEN_API_KEY": "sk-qwen",
            "QWEN_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "DEEPSEEK_API_KEY": "sk-deepseek",
        }
    )
    app_state = SimpleNamespace(
        settings=settings,
        workspace=SimpleNamespace(
            workspace_id="ws_1",
            workspace_path=Path("/tmp/codepilot"),
            codepilot_home=Path("/tmp/codepilot-home"),
        ),
        agent_profiles={"build": object(), "plan": object()},
        session_runner=SimpleNamespace(
            handle_input=None,
            get_status_snapshot=lambda: {
                "workspace_id": "ws_1",
                "workspace_path": "/tmp/codepilot",
                "session_id": None,
                "status": "IDLE",
                "agent_name": "build",
                "provider": None,
                "model": None,
            },
        ),
        session_memory=SimpleNamespace(replay=lambda _session_id: {"session": None, "messages": [], "records": []}),
        event_bus=SimpleNamespace(create_stream_queue=lambda: None, remove_stream_queue=lambda _queue: None),
        event_store=SimpleNamespace(replay=lambda **_kwargs: []),
    )
    app = FastAPI()
    app.include_router(build_api_router(app_state))

    with TestClient(app) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert [item["provider"] for item in payload["activated_providers"]] == ["openai", "qwen", "deepseek"]
    assert payload["activated_providers"][1]["models"] == ["qwen-plus", "qwen-max", "qwen3.5-flash"]
    assert payload["activated_providers"][2]["models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert payload["activated_providers"][0]["model_capabilities"]["gpt-5.3-codex"]["thinking"]["default_value"] == "medium"
    assert "default_model" not in payload["activated_providers"][0]
    assert "provider_selection_required" not in payload


def test_session_input_route_excludes_messages_from_session_payload() -> None:
    async def handle_input(_payload: GatewayInput) -> SessionState:
        return SessionState(
            session_id="session_1",
            workspace_id="ws_1",
            workspace_path="/tmp/codepilot",
            agent_name="build",
            provider="openai",
            model="gpt-5.3-codex",
            status=SessionStatus.RUNNING,
            created_at="2026-04-29T00:00:00Z",
            updated_at="2026-04-29T00:00:00Z",
            messages=[
                Message(
                    info=build_user_message_info(
                        message_id="msg_1",
                        session_id="session_1",
                        created_at_ms=1_746_000_000_000,
                        agent="build",
                        provider_id="openai",
                        model_id="gpt-5.3-codex",
                    ),
                    parts=[TextPart(text="hello")],
                )
            ],
        )

    app_state = SimpleNamespace(
        settings=build_settings({"OPENAI_API_KEY": "sk-openai"}),
        workspace=SimpleNamespace(
            workspace_id="ws_1",
            workspace_path=Path("/tmp/codepilot"),
            codepilot_home=Path("/tmp/codepilot-home"),
        ),
        agent_profiles={"build": object()},
        session_runner=SimpleNamespace(
            handle_input=handle_input,
            get_status_snapshot=lambda: {
                "workspace_id": "ws_1",
                "workspace_path": "/tmp/codepilot",
                "session_id": None,
                "status": "IDLE",
                "agent_name": "build",
                "provider": None,
                "model": None,
            },
        ),
        session_memory=SimpleNamespace(replay=lambda _session_id: {"session": None, "messages": [], "records": []}),
        event_bus=SimpleNamespace(create_stream_queue=lambda: None, remove_stream_queue=lambda _queue: None),
        event_store=SimpleNamespace(replay=lambda **_kwargs: []),
    )
    app = FastAPI()
    app.include_router(build_api_router(app_state))

    with TestClient(app) as client:
        response = client.post(
            "/api/session/input",
            json={
                "type": "user_message",
                "content": "hello",
                "agent_name": "build",
                "provider": "openai",
                "model": "gpt-5.3-codex",
                "metadata": {},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session"]["session_id"] == "session_1"
    assert "messages" not in payload["session"]


def test_gateway_input_requires_agent_provider_and_model_for_user_message() -> None:
    with pytest.raises(ValueError, match="agent_name"):
        GatewayInput(type=GatewayInputType.USER_MESSAGE, content="hello", provider="qwen", model="qwen-plus")

    with pytest.raises(ValueError, match="provider"):
        GatewayInput(type=GatewayInputType.USER_MESSAGE, content="hello", agent_name="build", model="qwen-plus")

    with pytest.raises(ValueError, match="model"):
        GatewayInput(type=GatewayInputType.USER_MESSAGE, content="hello", agent_name="build", provider="qwen")


def test_gateway_input_requires_explicit_approved_for_human_reply() -> None:
    with pytest.raises(ValueError, match="approved"):
        GatewayInput(type=GatewayInputType.HUMAN_REPLY, approval_id="approval_1")

    gateway_input = GatewayInput(type=GatewayInputType.HUMAN_REPLY, approval_id="approval_1", approved=False)

    assert gateway_input.approved is False


def test_session_runner_new_session_rejects_missing_model() -> None:
    settings = build_settings(
        {
            "QWEN_API_KEY": "sk-qwen",
            "QWEN_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }
    )
    runner = build_session_runner(settings)
    gateway_input = GatewayInput(
        type=GatewayInputType.USER_MESSAGE,
        content="hello",
        agent_name="build",
        provider="qwen",
        model="qwen-plus",
    )

    session = runner._new_session(gateway_input)

    assert session.provider == "qwen"
    assert session.model == "qwen-plus"


def test_session_runner_writes_thinking_enabled_to_new_session_metadata() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    session = runner._new_session(
        GatewayInput(
            type=GatewayInputType.USER_MESSAGE,
            content="hello",
            agent_name="build",
            provider="openai",
            model="gpt-5.3-codex",
            metadata={"thinking_enabled": True},
        )
    )

    assert session.metadata["thinking_enabled"] is True
    assert session.metadata["thinking_value"] == "medium"
    runner._session = session
    assert runner.get_status_snapshot()["thinking_enabled"] is True
    assert runner.get_status_snapshot()["thinking_value"] == "medium"


def test_session_runner_persists_user_image_attachment(tmp_path: Path) -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    workspace = SimpleNamespace(
        workspace_id="ws_1",
        workspace_path=tmp_path / "repo",
        workspace_dir=tmp_path / "runtime",
    )
    workspace.workspace_path.mkdir()
    runner = SessionRunner(
        workspace=workspace,
        config=settings,
        event_bus=DummyEventBus(),
        hook_manager=None,
        agent_loop=DummyAgentLoop(),
        agent_profiles={"build": object()},
        title_service=NoopTitleService(),
    )
    png_data = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
    gateway_input = GatewayInput(
        type=GatewayInputType.USER_MESSAGE,
        content="分析图片",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        attachments=[
            {
                "filename": "../sample",
                "mime": "image/png",
                "data_base64": base64.b64encode(png_data).decode("ascii"),
            }
        ],
    )

    runner._session = runner._new_session(gateway_input)
    message = runner._build_user_message(gateway_input)

    file_parts = [part for part in message.parts if isinstance(part, FilePart)]
    assert len(file_parts) == 1
    assert file_parts[0].filename == "sample.png"
    assert file_parts[0].mime == "image/png"
    assert file_parts[0].source is not None
    assert Path(file_parts[0].source.value).read_bytes() == png_data
    assert "data_base64" not in message.model_dump_json()


def test_session_runner_rejects_invalid_thinking_value() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    with pytest.raises(ValueError, match="thinking_value `xhigh` 不属于"):
        runner._new_session(
            GatewayInput(
                type=GatewayInputType.USER_MESSAGE,
                content="hello",
                agent_name="build",
                provider="openai",
                model="gpt-5.3-codex",
                metadata={"thinking_value": "xhigh"},
            )
        )


def test_session_runner_reuses_session_when_request_carries_same_session_id() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    first = GatewayInput(
        type=GatewayInputType.USER_MESSAGE,
        content="first",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
    )
    second = GatewayInput(
        type=GatewayInputType.USER_MESSAGE,
        session_id="",
        content="second",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
    )

    first_session = asyncio.run(runner.handle_input(first))
    assert first_session is not None
    first_session.status = SessionStatus.COMPLETED
    second.session_id = first_session.session_id

    second_session = asyncio.run(runner.handle_input(second))

    assert second_session is not None
    assert second_session.session_id == first_session.session_id
    assert len(second_session.messages) == 2
    assert second_session.messages[-1].text_content() == "second"


def test_session_runner_creates_new_session_when_request_omits_session_id() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    first = GatewayInput(
        type=GatewayInputType.USER_MESSAGE,
        content="first",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
    )
    second = GatewayInput(
        type=GatewayInputType.USER_MESSAGE,
        content="second",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
    )

    first_session = asyncio.run(runner.handle_input(first))
    assert first_session is not None
    first_session.status = SessionStatus.COMPLETED

    second_session = asyncio.run(runner.handle_input(second))

    assert second_session is not None
    assert second_session.session_id != first_session.session_id
    assert len(second_session.messages) == 1
    assert second_session.messages[0].text_content() == "second"


def test_session_runner_rejects_user_message_while_stopping() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)
    first = GatewayInput(
        type=GatewayInputType.USER_MESSAGE,
        content="first",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
    )

    session = asyncio.run(runner.handle_input(first))
    assert session is not None
    session.status = SessionStatus.STOPPING
    message_count = len(session.messages)

    with pytest.raises(ValueError, match="停止中"):
        asyncio.run(
            runner.handle_input(
                GatewayInput(
                    type=GatewayInputType.USER_MESSAGE,
                    session_id=session.session_id,
                    content="second",
                    agent_name="build",
                    provider="openai",
                    model="gpt-5.3-codex",
                )
            )
        )

    assert len(session.messages) == message_count


def test_session_runner_rejects_unknown_session_id() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    with pytest.raises(ValueError, match="不存在或未加载"):
        asyncio.run(
            runner.handle_input(
                GatewayInput(
                    type=GatewayInputType.USER_MESSAGE,
                    session_id="session_missing",
                    content="hello",
                    agent_name="build",
                    provider="openai",
                    model="gpt-5.3-codex",
                )
            )
        )


def test_session_runner_allows_switching_agent_and_model_on_existing_session() -> None:
    settings = build_settings(
        {
            "OPENAI_API_KEY": "sk-openai",
            "QWEN_API_KEY": "sk-qwen",
            "QWEN_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }
    )
    runner = build_session_runner(settings)

    first_session = asyncio.run(
        runner.handle_input(
            GatewayInput(
                type=GatewayInputType.USER_MESSAGE,
                content="first",
                agent_name="build",
                provider="openai",
                model="gpt-5.3-codex",
            )
        )
    )
    assert first_session is not None
    first_session.status = SessionStatus.COMPLETED

    second_session = asyncio.run(
        runner.handle_input(
            GatewayInput(
                type=GatewayInputType.USER_MESSAGE,
                session_id=first_session.session_id,
                content="continue",
                agent_name="plan",
                provider="qwen",
                model="qwen-plus",
            )
        )
    )

    assert second_session is not None
    assert second_session.session_id == first_session.session_id
    assert second_session.agent_name == "plan"
    assert second_session.provider == "qwen"
    assert second_session.model == "qwen-plus"
    assert second_session.messages[-1].info.agent == "plan"
    assert second_session.messages[-1].info.model.provider_id == "qwen"
    assert second_session.messages[-1].info.model.model_id == "qwen-plus"


def test_session_runner_updates_thinking_enabled_on_existing_session() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    first_session = asyncio.run(
        runner.handle_input(
            GatewayInput(
                type=GatewayInputType.USER_MESSAGE,
                content="first",
                agent_name="build",
                provider="openai",
                model="gpt-5.3-codex",
                metadata={"thinking_enabled": True},
            )
        )
    )
    assert first_session is not None
    first_session.status = SessionStatus.COMPLETED

    second_session = asyncio.run(
        runner.handle_input(
            GatewayInput(
                type=GatewayInputType.USER_MESSAGE,
                session_id=first_session.session_id,
                content="continue",
                agent_name="build",
                provider="openai",
                model="gpt-5.3-codex",
                metadata={"thinking_enabled": False},
            )
        )
    )

    assert second_session is not None
    assert second_session.metadata["thinking_enabled"] is False
    assert second_session.metadata["thinking_value"] is None


def test_session_runner_resets_schedule_session_to_interactive_on_manual_continue() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    first_session = asyncio.run(
        runner.handle_input(
            GatewayInput(
                type=GatewayInputType.USER_MESSAGE,
                content="schedule run",
                agent_name="build",
                provider="openai",
                model="gpt-5.3-codex",
                metadata={
                    "source": "schedule",
                    "schedule_task_id": "scht_1",
                    "schedule_run_id": "run_1",
                    "schedule_task_name": "每日巡检",
                },
            )
        )
    )
    assert first_session is not None
    first_session.status = SessionStatus.COMPLETED
    first_session.metadata["allow_human_interaction"] = False

    second_session = asyncio.run(
        runner.handle_input(
            GatewayInput(
                type=GatewayInputType.USER_MESSAGE,
                session_id=first_session.session_id,
                content="manual continue",
                agent_name="build",
                provider="openai",
                model="gpt-5.3-codex",
            )
        )
    )

    assert second_session is not None
    assert second_session.metadata["allow_human_interaction"] is True
    assert second_session.metadata["source"] == "schedule"
    assert second_session.metadata["schedule_task_id"] == "scht_1"


def test_session_runner_keeps_non_interactive_metadata_for_worker_session() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings, allow_human_interaction=False)

    session = runner._new_session(
        GatewayInput(
            type=GatewayInputType.USER_MESSAGE,
            content="schedule run",
            agent_name="build",
            provider="openai",
            model="gpt-5.3-codex",
        )
    )

    assert session.metadata["allow_human_interaction"] is False


def test_session_runner_rejects_unknown_agent_name() -> None:
    settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    runner = build_session_runner(settings)

    with pytest.raises(ValueError, match="agent `missing` 不存在或不可用"):
        asyncio.run(
            runner.handle_input(
                GatewayInput(
                    type=GatewayInputType.USER_MESSAGE,
                    content="hello",
                    agent_name="missing",
                    provider="openai",
                    model="gpt-5.3-codex",
                )
            )
        )


def test_load_settings_requires_real_config_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "config.yaml"

    with pytest.raises(ValueError, match="未找到 backend/config.yaml"):
        load_settings(missing_path)


def test_resolve_repo_root_is_independent_of_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    from codepilot.main import _resolve_repo_root

    monkeypatch.chdir(Path("/tmp"))

    repo_root = _resolve_repo_root()

    assert repo_root.name == "CodePilot"
    assert (repo_root / "backend" / "config.yaml").exists()


def test_session_stream_uses_sse_comment_for_heartbeat() -> None:
    base_settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    settings = base_settings.model_copy(
        update={
            "sse": base_settings.sse.model_copy(
                update={"heartbeat_seconds": 0.01, "replay_on_connect": False}
            )
        }
    )
    queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
    app_state = SimpleNamespace(
        settings=settings,
        workspace=SimpleNamespace(
            workspace_id="ws_1",
            workspace_path=Path("/tmp/codepilot"),
            codepilot_home=Path("/tmp/codepilot-home"),
        ),
        agent_profiles={"build": object()},
        session_runner=SimpleNamespace(
            handle_input=None,
            current_session_id=lambda: "session_1",
            get_status_snapshot=lambda: {
                "workspace_id": "ws_1",
                "workspace_path": "/tmp/codepilot",
                "session_id": "session_1",
                "status": "RUNNING",
                "agent_name": "build",
                "provider": "openai",
                "model": "gpt-5.3-codex",
            },
        ),
        session_memory=SimpleNamespace(replay=lambda _session_id: {"session": None, "messages": [], "records": []}),
        event_bus=SimpleNamespace(create_stream_queue=lambda: queue, remove_stream_queue=lambda _queue: None),
        event_store=SimpleNamespace(replay=lambda **_kwargs: []),
    )
    app = FastAPI()
    app.include_router(build_api_router(app_state))
    route = next(route for route in app.router.routes if getattr(route, "path", None) == "/api/session/stream")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/api/session/stream", "headers": []}, receive)
    response = asyncio.run(route.endpoint(request, after_seq=0))
    chunk = asyncio.run(response.body_iterator.__anext__())
    asyncio.run(response.body_iterator.aclose())

    assert chunk.startswith(": heartbeat ")
    assert "event: heartbeat" not in chunk
    assert "data:" not in chunk


def test_session_replay_returns_empty_when_no_current_session() -> None:
    app_state = SimpleNamespace(
        session_runner=SimpleNamespace(
            get_status_snapshot=lambda: {
                "workspace_id": "ws_1",
                "workspace_path": "/tmp/codepilot",
                "session_id": None,
                "status": "IDLE",
                "agent_name": "build",
                "provider": None,
                "model": None,
            }
        ),
        session_memory=SimpleNamespace(
            replay=lambda _session_id: {
                "session": {"data": {"session_id": "session_latest"}},
                "messages": [{"unexpected": True}],
                "records": [{"unexpected": True}],
            }
        ),
    )
    app = FastAPI()
    app.include_router(build_api_router(app_state))
    client = TestClient(app)

    response = client.get("/api/session/replay")

    assert response.status_code == 200
    assert response.json() == {"session": None, "messages": [], "records": []}


def test_session_stream_skips_replay_when_no_current_session() -> None:
    base_settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    settings = base_settings.model_copy(
        update={
            "sse": base_settings.sse.model_copy(
                update={"heartbeat_seconds": 0.01, "replay_on_connect": True}
            )
        }
    )
    replay_event = StreamEvent(
        seq=12,
        event_id="evt_latest",
        event_type="session_started",
        session_id="session_latest",
        created_at="2026-04-30T00:00:00Z",
        data={"agent_name": "build"},
    )
    queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
    app_state = SimpleNamespace(
        settings=settings,
        workspace=SimpleNamespace(
            workspace_id="ws_1",
            workspace_path=Path("/tmp/codepilot"),
            codepilot_home=Path("/tmp/codepilot-home"),
        ),
        agent_profiles={"build": object()},
        session_runner=SimpleNamespace(
            handle_input=None,
            current_session_id=lambda: None,
            get_status_snapshot=lambda: {
                "workspace_id": "ws_1",
                "workspace_path": "/tmp/codepilot",
                "session_id": None,
                "status": "IDLE",
                "agent_name": "build",
                "provider": None,
                "model": None,
            },
        ),
        session_memory=SimpleNamespace(replay=lambda _session_id: {"session": None, "messages": [], "records": []}),
        event_bus=SimpleNamespace(create_stream_queue=lambda: queue, remove_stream_queue=lambda _queue: None),
        event_store=SimpleNamespace(replay=lambda **_kwargs: [replay_event]),
    )
    app = FastAPI()
    app.include_router(build_api_router(app_state))
    route = next(route for route in app.router.routes if getattr(route, "path", None) == "/api/session/stream")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/api/session/stream", "headers": []}, receive)
    response = asyncio.run(route.endpoint(request, after_seq=0))
    chunk = asyncio.run(response.body_iterator.__anext__())
    asyncio.run(response.body_iterator.aclose())

    assert chunk.startswith(": heartbeat ")
    assert "session_latest" not in chunk


def test_session_stream_replay_keeps_business_event_format() -> None:
    base_settings = build_settings({"OPENAI_API_KEY": "sk-openai"})
    settings = base_settings.model_copy(
        update={
            "sse": base_settings.sse.model_copy(
                update={"heartbeat_seconds": 15, "replay_on_connect": True}
            )
        }
    )
    replay_event = StreamEvent(
        seq=12,
        event_id="evt_1",
        event_type="session_started",
        session_id="session_1",
        created_at="2026-04-30T00:00:00Z",
        data={"agent_name": "build"},
    )
    queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
    app_state = SimpleNamespace(
        settings=settings,
        workspace=SimpleNamespace(
            workspace_id="ws_1",
            workspace_path=Path("/tmp/codepilot"),
            codepilot_home=Path("/tmp/codepilot-home"),
        ),
        agent_profiles={"build": object()},
        session_runner=SimpleNamespace(
            handle_input=None,
            current_session_id=lambda: "session_1",
            get_status_snapshot=lambda: {
                "workspace_id": "ws_1",
                "workspace_path": "/tmp/codepilot",
                "session_id": "session_1",
                "status": "RUNNING",
                "agent_name": "build",
                "provider": "openai",
                "model": "gpt-5.3-codex",
            },
        ),
        session_memory=SimpleNamespace(replay=lambda _session_id: {"session": None, "messages": [], "records": []}),
        event_bus=SimpleNamespace(create_stream_queue=lambda: queue, remove_stream_queue=lambda _queue: None),
        event_store=SimpleNamespace(replay=lambda **_kwargs: [replay_event]),
    )
    app = FastAPI()
    app.include_router(build_api_router(app_state))
    route = next(route for route in app.router.routes if getattr(route, "path", None) == "/api/session/stream")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/api/session/stream", "headers": []}, receive)
    response = asyncio.run(route.endpoint(request, after_seq=11))
    chunk = asyncio.run(response.body_iterator.__anext__())
    asyncio.run(response.body_iterator.aclose())

    assert "id: 12" in chunk
    assert "event: session_started" in chunk
    assert '"event_type": "session_started"' in chunk
