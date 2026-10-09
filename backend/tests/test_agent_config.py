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


@pytest.mark.parametrize("replacement,code", [
    ("name: [敏感内容", "agent_yaml_invalid"),
    ("readonly: wrong", "agent_field_invalid"),
    ("description: ''", "agent_field_required"),
])
def test_parse_failure_reports_safe_reason(tmp_path, replacement, code):
    service = _service(tmp_path)
    service.create(_payload())
    path = service.root / "reviewer.md"
    raw = path.read_text()
    import re
    raw = re.sub(rf"^{replacement.split(':')[0]}:.*$", replacement, raw, flags=re.M)
    path.write_text(raw)
    invalid = next(item for item in _service(tmp_path).list() if item["name"] == "reviewer")
    assert invalid["validation_issues"][0]["code"] == code
    assert "敏感内容" not in str(invalid)
    assert str(tmp_path) not in str(invalid)


def test_revision_failure_keeps_diagnosis_on_start(tmp_path):
    service = _service(tmp_path)
    service.create(_payload())
    path = service.root / "reviewer.md"
    path.write_text(path.read_text().replace("请审查代码。", "新的指令。"))
    reloaded = _service(tmp_path)
    invalid = next(item for item in reloaded.list() if item["name"] == "reviewer")
    with pytest.raises(AgentConfigError) as caught:
        reloaded.get_active_profile_snapshot(invalid["agent_id"])
    assert caught.value.code == "agent_invalid"
    assert caught.value.issues[0]["code"] == "agent_revision_mismatch"
    assert "新的指令" not in str(caught.value.issues)


def test_start_dependency_errors_include_resources(tmp_path):
    service = _service(tmp_path)
    record = service.create(_payload(tool_names=["missing_tool"], default_model="missing_model"))
    with pytest.raises(AgentConfigError) as caught:
        service.get_active_profile_snapshot(record["agent_id"])
    assert {issue["code"] for issue in caught.value.issues} == {"tool_unavailable", "model_unavailable"}
    assert "missing_tool" in str(caught.value.issues)
    assert "missing_model" in str(caught.value.issues)


def test_save_schema_error_does_not_echo_input(tmp_path):
    from fastapi import APIRouter
    app = FastAPI()
    router = APIRouter()
    register_agent_routes(router, SimpleNamespace(agent_config_service=_service(tmp_path)))
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post('/agents', json=_payload(max_iterations="敏感错误值", system_prompt="私密指令"))
    assert response.status_code == 422
    assert response.json()['detail']['issues'][0]['field'] == 'max_iterations'
    assert '敏感错误值' not in response.text
    assert '私密指令' not in response.text


def test_assembly_survives_legacy_form_and_revision_reload(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(_payload(kind="subagent", max_iterations=7,
                                      hook_ids=["hook-a"], subagent_ids=[], memory_enabled=False))
    updated = service.update(created["agent_id"], _payload(description="更新描述", expected_revision_id=created["revision_id"]))
    snapshot = _service(tmp_path).get_profile_revision_snapshot(created["agent_id"], updated["revision_id"])
    assert snapshot.kind == "subagent"
    assert snapshot.max_iterations == 7
    assert snapshot.hook_ids == ["hook-a"]
    assert snapshot.memory_enabled is False
    cleared = service.update(created["agent_id"], _payload(hook_ids=[], expected_revision_id=updated["revision_id"]))
    assert cleared["hook_ids"] == []
    migrated = service.update(created["agent_id"], _payload(
        launch_modes=["direct", "delegated"],
        expected_revision_id=cleared["revision_id"],
    ))
    assert migrated["launch_modes"] == ["direct", "delegated"]
    saved = (service.root / "reviewer.md").read_text(encoding="utf-8")
    assert "launch_modes:" in saved
    assert "kind:" not in saved
    assert "can_call_subagent:" not in saved
    assert "subagent_ids:" not in saved


def test_http_legacy_form_preserves_assembly(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = FastAPI()
    register_agent_routes(app, SimpleNamespace(agent_config_service=service))
    with TestClient(app) as client:
        created = client.post("/agents", json=_payload(kind="subagent", max_iterations=3, memory_enabled=False)).json()
        response = client.put(f"/agents/{created['agent_id']}", json=_payload(expected_revision_id=created["revision_id"]))
        assert response.status_code == 200
        assert response.json()["kind"] == "subagent"
        assert response.json()["max_iterations"] == 3
        assert response.json()["memory_enabled"] is False


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


@pytest.mark.parametrize("delegate_field", ["allowed_subagent_ids", "subagent_ids"])
def test_legacy_agent_with_revision_uses_original_document_digest(tmp_path: Path, delegate_field: str) -> None:
    service = _service(tmp_path)
    service.tool_registry.register(_Tool("task"))
    root = service.root
    root.mkdir(parents=True, exist_ok=True)
    agent_id = "4b97b9ae-1f91-4009-9a93-ec24c36528c1"
    child_id = "16c69097-85b4-5a07-b1ac-2a6f102cfa51"
    without_revision = f"""---
agent_id: {agent_id}
can_call_subagent: true
default_model: model
default_provider: test
description: 旧版配置
format_version: 2
{delegate_field}:
- {child_id}
kind: agent
name: legacy
readonly: false
tools:
- read_file
- task
---
执行旧配置。
"""
    revision = service._revision(without_revision)
    content = without_revision.replace(f"agent_id: {agent_id}\n", f"agent_id: {agent_id}\nrevision_id: {revision}\n")
    path = root / "legacy.md"
    path.write_text(content, encoding="utf-8")

    reloaded = _service(tmp_path)
    reloaded.tool_registry.register(_Tool("task"))
    record = next(item for item in reloaded.list() if item["name"] == "legacy")
    snapshot = reloaded.get_active_profile_snapshot(record["agent_id"])

    assert record["revision_id"] == revision
    assert snapshot.delegate_agent_ids == [child_id]
    assert snapshot.launch_modes == ["direct"]
    assert snapshot.can_delegate is True
    assert path.read_text(encoding="utf-8") == content

    updated = reloaded.update(
        record["agent_id"],
        {"description": "保存后升级", "expected_revision_id": revision},
    )
    saved = path.read_text(encoding="utf-8")
    assert updated["revision_id"] != revision
    assert "launch_modes:" in saved
    assert "can_delegate: true" in saved
    assert "delegate_agent_ids:" in saved
    assert "kind:" not in saved
    assert "can_call_subagent:" not in saved
    assert "subagent_ids:" not in saved
    assert "allowed_subagent_ids:" not in saved
    saved_service = _service(tmp_path)
    saved_service.tool_registry.register(_Tool("task"))
    assert saved_service.get_active_profile_snapshot(record["agent_id"]).delegate_agent_ids == [child_id]


def test_update_rejects_conflict_and_unknown_tool(tmp_path: Path) -> None:
    service = _service(tmp_path); created = service.create(_payload())
    with pytest.raises(AgentConfigError) as conflict:
        service.update(created["agent_id"], _payload(name="reviewer", description="冲突", expected_revision_id="old"))
    assert conflict.value.status == 409
    pending = service.create(_payload(name="other", tool_names=["unknown"]))
    assert pending["validation_status"] == "needs_configuration"
    with pytest.raises(AgentConfigError) as missing:
        service.get_active_profile_snapshot(pending["agent_id"])
    assert missing.value.code == "agent_dependencies_missing"


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


def test_execution_checks_current_skills_and_child_dependencies(tmp_path):
    import base64
    from uuid import uuid4
    from codepilot.session.agent_config import MultiUserAgentConfigService
    from codepilot.skills.store import SkillStore

    base = _service(tmp_path)
    service = MultiUserAgentConfigService(settings=base.settings, shared_root=tmp_path / "shared",
                users_root=tmp_path / "users", builtin_profiles={}, tool_registry=base.tool_registry,
                mcp_manager=base.mcp_manager, workspace_path=tmp_path)
    owner = str(uuid4())
    store = SkillStore(tmp_path)
    def files(text):
        return {"files": {"SKILL.md": base64.b64encode(f"---\nname: skill\ndescription: 技能\n---\n{text}".encode()).decode()}}
    skill = store.save(owner, None, files("第一版"))
    child = service.create(owner, _payload(name="child", kind="subagent", skill_ids=[skill["skill_id"]]))
    parent = service.create(owner, _payload(subagent_ids=[child["agent_id"]], skill_ids=[skill["skill_id"]]))
    first = service.get_active_profile_snapshot(owner, parent["agent_id"])
    changed = store.save(owner, skill["skill_id"], {**files("第二版"), "expected_revision": skill["revision"]})
    assert service.resolve_execution_profile(owner, first).skill_ids == [skill["skill_id"]]
    assert service.get_active_profile_snapshot(owner, parent["agent_id"]).skill_ids == [skill["skill_id"]]
    assert "第二版" in store.read(owner, skill["skill_id"], "SKILL.md").decode()
    service.archive(owner, child["agent_id"])
    assert service.get(owner, parent["agent_id"])["validation_status"] == "needs_configuration"
    issue = service.get(owner, parent["agent_id"])["validation_issues"][0]
    assert issue["field"] == "delegate_agent_ids"
    assert "委派 Agent" in issue["message"]
    assert child["agent_id"] in issue["message"]
    with pytest.raises(AgentConfigError):
        service.get_profile_revision_snapshot(owner, parent["agent_id"], parent["revision_id"])


def test_default_skills_resolve_current_resources(tmp_path):
    from uuid import uuid4
    from codepilot.session.agent_config import MultiUserAgentConfigService

    base = _service(tmp_path)
    service = MultiUserAgentConfigService(settings=base.settings, shared_root=tmp_path / "shared",
                users_root=tmp_path / "users", builtin_profiles={}, tool_registry=base.tool_registry,
                mcp_manager=base.mcp_manager, workspace_path=tmp_path)
    owner = str(uuid4())
    agent = service.create(owner, _payload())
    frozen = service.get_active_profile_snapshot(owner, agent["agent_id"])
    shared = tmp_path / "skills" / "later"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("---\nname: later\ndescription: 后添加\n---\n技能正文")
    assert service.resolve_execution_profile(owner, frozen).skill_ids is None
    assert service.get_active_profile_snapshot(owner, agent["agent_id"]).skill_ids is None
    assert not (tmp_path / "skill-snapshots").exists()


@pytest.mark.parametrize("launch_modes", [["direct"], ["delegated"], ["direct", "delegated"]])
def test_http_current_form_saves_tool_binding_and_roles(tmp_path, launch_modes):
    from uuid import uuid4
    from codepilot.tools.code_store import ToolStore
    store = ToolStore(tmp_path)
    tool = store.save(str(uuid4()), None, {"definition": {"name": "test-tool", "description": "保存协议测试", "input_schema": {"type": "object"}}, "files": {"main.py": "def execute(arguments):\n    return {}\n"}})
    service = _service(tmp_path)
    service.tool_registry.register(_Tool("task"))
    app = FastAPI()
    register_agent_routes(app, SimpleNamespace(agent_config_service=service))
    # 使用网页完整表单的新角色字段经过真正的请求校验，不能绕过 HTTP 调服务。
    current = _payload(launch_modes=launch_modes, can_delegate=False, tool_ids=[])
    with TestClient(app) as client:
        created = client.post("/agents", json=current)
        assert created.status_code == 200, created.text
        record = created.json()
        response = client.put(f"/agents/{record['agent_id']}", json={**current, "can_delegate": True, "tool_names": ["read_file", "task"], "tool_ids": [tool['tool_id']], "expected_revision_id": record['revision_id']})
        assert response.status_code == 200, response.text
        saved = response.json()
        loaded = client.get(f"/agents/{record['agent_id']}").json()
        assert loaded['tool_ids'] == [tool['tool_id']]
        assert loaded['launch_modes'] == launch_modes and loaded['can_delegate'] is True
        snapshot = _service(tmp_path).get_profile_revision_snapshot(record['agent_id'], saved['revision_id'])
        assert snapshot.tool_ids == loaded['tool_ids'] and snapshot.launch_modes == launch_modes
        assert snapshot.can_delegate is True
        # 旧客户端省略新字段不能用 API 默认值覆盖已有绑定或角色。
        legacy = client.put(f"/agents/{record['agent_id']}", json=_payload(tool_names=["read_file", "task"], expected_revision_id=saved['revision_id']))
        assert legacy.status_code == 200
        assert legacy.json()['launch_modes'] == launch_modes and legacy.json()['can_delegate'] is True
        assert legacy.json()['tool_ids'] == [tool['tool_id']]
        cleared = client.put(f"/agents/{record['agent_id']}", json=_payload(tool_ids=[], tool_names=["read_file", "task"], expected_revision_id=legacy.json()['revision_id']))
        assert cleared.status_code == 200 and cleared.json()['tool_ids'] == []


@pytest.mark.parametrize("field,value", [
    ("launch_modes", []), ("launch_modes", ["invalid"]), ("launch_modes", "direct"),
    ("launch_modes", None), ("can_delegate", "false"), ("can_delegate", 1),
    ("can_delegate", None), ("tool_ids", None), ("unexpected", True),
])
def test_http_current_form_rejects_invalid_fields_safely(tmp_path, field, value):
    app = FastAPI()
    register_agent_routes(app, SimpleNamespace(agent_config_service=_service(tmp_path)))
    with TestClient(app) as client:
        response = client.post("/agents", json=_payload(**{field: value}, system_prompt="私密测试指令"))
    assert response.status_code == 422
    assert '私密测试指令' not in response.text
    assert response.json()['detail']['code'] == 'agent_config_invalid'
    assert response.json()['detail']['issues'][0]['field'] == (None if field == 'unexpected' else field)


def test_http_delegation_still_requires_task(tmp_path):
    app = FastAPI()
    register_agent_routes(app, SimpleNamespace(agent_config_service=_service(tmp_path)))
    with TestClient(app) as client:
        response = client.post("/agents", json=_payload(launch_modes=["direct"], can_delegate=True))
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'agent_delegate_tool_required'
