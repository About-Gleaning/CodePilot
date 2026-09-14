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
    assert {profile.name for profile in service.list_active_subagent_profile_snapshots()} == {"explore"}
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


def test_revision_snapshot_survives_update_but_not_archive_or_corruption(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(_payload())
    old_revision = created["revision_id"]
    service.update(
        created["agent_id"],
        _payload(name="reviewer", description="新的描述", expected_revision_id=old_revision),
    )

    snapshot = service.get_profile_revision_snapshot(created["agent_id"], old_revision)
    assert snapshot.description == "审查代码"
    assert snapshot.revision_id == old_revision

    revision_path = tmp_path / "agents" / ".revisions" / created["agent_id"] / f"{old_revision}.md"
    revision_path.write_text(revision_path.read_text(encoding="utf-8") + "损坏", encoding="utf-8")
    with pytest.raises(AgentConfigError, match="revision"):
        service.get_profile_revision_snapshot(created["agent_id"], old_revision)
    revision_path.unlink()
    with pytest.raises(AgentConfigError) as missing:
        service.get_profile_revision_snapshot(created["agent_id"], old_revision)
    assert missing.value.code == "agent_revision_not_found"

    service.archive(created["agent_id"])
    with pytest.raises(AgentConfigError) as archived:
        service.get_profile_revision_snapshot(created["agent_id"], old_revision)
    assert archived.value.code == "agent_archived"


def test_current_revision_snapshot_must_exist_and_survive_service_reload(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(_payload())
    revision_path = tmp_path / "agents" / ".revisions" / created["agent_id"] / f"{created['revision_id']}.md"
    revision_path.unlink()

    with pytest.raises(AgentConfigError) as missing:
        service.get_profile_revision_snapshot(created["agent_id"], created["revision_id"])
    assert missing.value.code == "agent_revision_not_found"

    reloaded = _service(tmp_path)
    assert not revision_path.exists()
    with pytest.raises(AgentConfigError) as still_missing:
        reloaded.get_profile_revision_snapshot(created["agent_id"], created["revision_id"])
    assert still_missing.value.code == "agent_revision_not_found"


def test_current_revision_snapshot_rejects_symlink(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(_payload())
    revision_path = tmp_path / "agents" / ".revisions" / created["agent_id"] / f"{created['revision_id']}.md"
    target = tmp_path / "revision-copy.md"
    target.write_text(revision_path.read_text(encoding="utf-8"), encoding="utf-8")
    revision_path.unlink()
    revision_path.symlink_to(target)

    with pytest.raises(AgentConfigError) as invalid:
        service.get_profile_revision_snapshot(created["agent_id"], created["revision_id"])
    assert invalid.value.code == "agent_revision_not_found"


def test_current_revision_snapshot_rejects_revision_identity_mismatch(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(_payload())
    revision_path = tmp_path / "agents" / ".revisions" / created["agent_id"] / f"{created['revision_id']}.md"
    content = revision_path.read_text(encoding="utf-8")
    revision_path.write_text(
        content.replace(f"revision_id: {created['revision_id']}", f"revision_id: {'0' * 64}"),
        encoding="utf-8",
    )

    with pytest.raises(AgentConfigError) as invalid:
        service.get_profile_revision_snapshot(created["agent_id"], created["revision_id"])
    assert invalid.value.code == "agent_revision_corrupt"


def test_legacy_agent_without_declared_revision_gets_one_time_snapshot(tmp_path: Path) -> None:
    _service(tmp_path)
    root = tmp_path / "agents"
    root.mkdir(parents=True)
    (root / "legacy.md").write_text(
        """---
name: legacy
kind: agent
description: 旧配置
default_provider: test
default_model: model
tools:
  - read_file
readonly: false
can_call_subagent: false
---
执行旧配置。
""",
        encoding="utf-8",
    )

    reloaded = _service(tmp_path)
    record = next(item for item in reloaded.list() if item["name"] == "legacy")
    revision_path = root / ".revisions" / record["agent_id"] / f"{record['revision_id']}.md"

    assert revision_path.exists()
    assert reloaded.get_profile_revision_snapshot(record["agent_id"], record["revision_id"]).name == "legacy"


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
        assert all("system_prompt" not in item for item in listed)
        summary = next(item for item in listed if item["agent_id"] == created["agent_id"])
        assert summary["default_provider"] == "test"
        assert summary["default_model"] == "model"
        assert client.get(f"/agents/{created['agent_id']}").json()["system_prompt"] == "请审查代码。"
