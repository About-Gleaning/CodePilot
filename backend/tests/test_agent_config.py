from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codepilot.config.settings import ActivatedLLMProvider, AppSettings, LLMModelSettings, LLMRuntimeSettings
from codepilot.session.agent_config import AgentConfigError, AgentConfigService
from codepilot.session.agents import build_agent_profiles
from codepilot.tools.base import BaseTool, ToolSpec
from codepilot.tools.registry import ToolRegistry
from codepilot.api.agent_routes import register_agent_routes


class _Tool(BaseTool):
    def __init__(self, name: str = "read_file", *, assignable: bool = True) -> None:
        self.spec = ToolSpec(name=name, description="测试工具", input_schema={"type": "object"}, timeout_seconds=1,
                             side_effect="read_only", assignable_to_custom_agents=assignable)

    async def execute(self, args, context=None): return {}


def _service(tmp_path: Path) -> AgentConfigService:
    settings = AppSettings(llm_runtime=LLMRuntimeSettings(activated_providers={"test": ActivatedLLMProvider(provider="test", label="测试", models=["model"], model_settings={"model": LLMModelSettings(id="model")})}))
    registry = ToolRegistry(); registry.register(_Tool())
    return AgentConfigService(settings=settings, root=tmp_path / "agents", agent_profiles=build_agent_profiles(5), tool_registry=registry,
                              mcp_manager=SimpleNamespace(list_server_capabilities=lambda: []))


def _payload(**changes):
    value = {"name": "reviewer", "description": "审查代码", "system_prompt": "请审查代码。", "default_provider": "test", "default_model": "model", "tool_names": ["read_file"], "mcp_server_names": []}
    value.update(changes); return value


def test_create_update_revision_and_archive_restore(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(_payload())
    assert created["revision_id"]
    assert service.create(_payload())["agent_id"] == created["agent_id"]
    assert "reviewer" in service.agent_profiles
    unchanged = service.update(created["agent_id"], _payload(expected_revision_id=created["revision_id"], name="reviewer"))
    assert unchanged["revision_id"] == created["revision_id"]
    changed = service.update(created["agent_id"], _payload(name="reviewer", description="新的描述", expected_revision_id=created["revision_id"]))
    assert changed["revision_id"] != created["revision_id"]
    assert (tmp_path / "agents" / ".revisions" / created["agent_id"] / f"{created['revision_id']}.md").exists()
    archived = service.archive(created["agent_id"])
    assert archived["archived"] is True and "reviewer" not in service.agent_profiles
    restored = service.restore(created["agent_id"])
    assert restored["archived"] is False and "reviewer" in service.agent_profiles


def test_update_rejects_conflict_and_unknown_tool(tmp_path: Path) -> None:
    service = _service(tmp_path); created = service.create(_payload())
    with pytest.raises(AgentConfigError) as conflict:
        service.update(created["agent_id"], _payload(name="reviewer", description="冲突", expected_revision_id="old"))
    assert conflict.value.status == 409
    with pytest.raises(AgentConfigError, match="Tool"):
        service.create(_payload(name="other", tool_names=["unknown"]))


def test_agent_routes_hide_prompt_from_list(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = FastAPI(); register_agent_routes(app, SimpleNamespace(agent_config_service=service))
    with TestClient(app) as client:
        created = client.post("/agents", json=_payload()).json()
        listed = client.get("/agents").json()["agents"]
        assert "system_prompt" not in listed[0]
        assert client.get(f"/agents/{created['agent_id']}").json()["system_prompt"] == "请审查代码。"
