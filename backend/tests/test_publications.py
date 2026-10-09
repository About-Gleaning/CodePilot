import base64
import uuid

import pytest

from test_agent_config import _service, _payload
from codepilot.session.agent_config import AgentConfigError, MultiUserAgentConfigService
from codepilot.skills.store import SkillStore
from codepilot.hooks.store import HookStore, HookPayload


@pytest.fixture
def setup(tmp_path):
    base = _service(tmp_path)
    service = MultiUserAgentConfigService(settings=base.settings, shared_root=tmp_path / "shared",
        users_root=tmp_path / "users", builtin_profiles={}, tool_registry=base.tool_registry, mcp_manager=base.mcp_manager)
    return service, str(uuid.uuid4()), str(uuid.uuid4())


def skill_payload(body="初始内容"):
    return {"files": {"SKILL.md": base64.b64encode(f"---\nname: demo\ndescription: 测试技能\n---\n{body}".encode()).decode()}}


def publish(service, user, agent):
    preview = service.publications.preview(service, user, "作者", agent["agent_id"])
    return service.publications.confirm(service, user, preview["candidate_id"], preview["digest"], preview["expected_revision"], str(uuid.uuid4()))


def test_publish_resources_independent_and_live_skill(setup):
    service, author, reader = setup
    skill = SkillStore(service.users_root.parent).save(author, None, skill_payload())
    hook = HookStore(service.users_root.parent).save(author, None, HookPayload.model_validate({"definition": {"name": "补充", "plugin_type": "prompt", "hook_type": "llm.before", "content": "原提示"}}))
    agent = service.create(author, _payload(skill_ids=[skill["skill_id"]], hook_ids=[hook["hook_id"]], subagent_ids=[]))
    result = publish(service, author, agent)
    profile = service.get_active_profile_snapshot(reader, result["id"])
    assert profile.publication_id == result["id"]
    assert profile.connection_ids == []
    assert profile.working_directory is None
    resources = service.publications.get(result["id"])["manifest"]["resources"]
    assert service.publications.get(result["id"])["manifest"]["schema_version"] == 4
    resource = next(r for r in resources if r["kind"] == "skill")
    public = SkillStore(service.users_root.parent, publication_id=result["id"])
    current = public.get(author, resource["id"])
    public.save(author, resource["id"], {**skill_payload("公共修改"), "expected_revision": current["revision"]})
    again = publish(service, author, agent)
    assert again["number"] == 1
    assert "公共修改" in public.read(reader, resource["id"], "SKILL.md").decode()
    assert "公共修改" not in SkillStore(service.users_root.parent).read(author, skill["skill_id"], "SKILL.md").decode()


def test_confirm_idempotent_and_source_conflict(setup):
    service, author, reader = setup
    agent = service.create(author, _payload(skill_ids=[], hook_ids=[], subagent_ids=[]))
    preview = service.publications.preview(service, author, "作者", agent["agent_id"])
    args = (service, author, preview["candidate_id"], preview["digest"], None, "same")
    first = service.publications.confirm(*args)
    service.publications.change_status(first["id"], author, first["revision"])
    assert service.publications.confirm(*args) == first
    with pytest.raises(AgentConfigError):
        service.get_active_profile_snapshot(reader, first["id"])
    preview = service.publications.preview(service, author, "作者", agent["agent_id"])
    service.update(author, agent["agent_id"], _payload(description="修改", expected_revision_id=agent["revision_id"]))
    with pytest.raises(AgentConfigError, match="源配置"):
        service.publications.confirm(service, author, preview["candidate_id"], preview["digest"], preview["expected_revision"], "other")


def test_authorization_and_sensitive_content(setup):
    service, author, reader = setup
    agent = service.create(author, _payload(skill_ids=[], hook_ids=[], subagent_ids=[]))
    with pytest.raises(AgentConfigError):
        service.publications.preview(service, reader, "使用者", agent["agent_id"])
    result = publish(service, author, agent)
    with pytest.raises(AgentConfigError):
        service.publications.authorize(result["id"], reader, write=True)
    service.update(author, agent["agent_id"], _payload(system_prompt="读取 /Users/private/secret", expected_revision_id=agent["revision_id"]))
    with pytest.raises(AgentConfigError, match="私人路径"):
        service.publications.preview(service, author, "作者", agent["agent_id"])


def test_public_children_and_hooks_are_frozen_per_run(setup):
    from test_agent_config import _Tool
    service, author, reader = setup
    service.tool_registry.register(_Tool("task"))
    hooks = HookStore(service.users_root.parent)
    hook = hooks.save(author, None, HookPayload.model_validate({"definition": {"name": "提示", "plugin_type": "prompt", "hook_type": "llm.before", "content": "旧提示"}}))
    child = service.create(author, _payload(name="child", kind="subagent", hook_ids=[hook["hook_id"]], skill_ids=[], subagent_ids=[]))
    parent = service.create(author, _payload(tool_names=["task"], subagent_ids=[child["agent_id"]], hook_ids=[], skill_ids=[]))
    result = publish(service, author, parent)
    before = service.get_active_profile_snapshot(reader, result["id"])
    resource = service.publications.get(result["id"])["manifest"]["resources"][0]
    public = HookStore(service.users_root.parent, publication_id=result["id"])
    identity = "personal:" + resource["id"]
    current = public.load(author, identity)
    public.save(author, identity, HookPayload.model_validate({"definition": {**current["definition"], "content": "新提示"}, "expected_revision": current["revision"]}))
    after = service.get_active_profile_snapshot(reader, result["id"])
    assert before.publication_children.keys() == after.publication_children.keys()
    first = next(iter(before.publication_children.values()))
    second = next(iter(after.publication_children.values()))
    assert first["resolved_hook_versions"] != second["resolved_hook_versions"]
    assert public.load(reader, identity, next(iter(first["resolved_hook_versions"].values())))["definition"]["content"] == "旧提示"
    assert child["agent_id"] not in before.subagent_ids


def test_usage_isolation_and_cross_scope_denied(setup):
    service, author, reader = setup
    skill = SkillStore(service.users_root.parent).save(author, None, skill_payload())
    agent = service.create(author, _payload(skill_ids=[skill["skill_id"]], hook_ids=[], subagent_ids=[]))
    result = publish(service, author, agent)
    service.publications.save_settings(reader, result["id"], {"working_directory": None, "connections": {}}, None)
    assert service.publications.settings(author, result["id"])["revision"] is None
    assert any(item["agent_id"] == result["id"] for item in service.list(reader))
    public = service.get_active_profile_snapshot(reader, result["id"])
    private = service.create(reader, _payload(skill_ids=public.skill_ids, hook_ids=[], subagent_ids=[]))
    with pytest.raises(AgentConfigError, match="不属于"):
        service.get_active_profile_snapshot(reader, private["agent_id"])


def test_publication_api_author_and_reader(setup):
    from fastapi import FastAPI, APIRouter
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from codepilot.api.publication_routes import register_publication_routes
    service, author, reader = setup
    skill = SkillStore(service.users_root.parent).save(author, None, skill_payload())
    agent = service.create(author, _payload(skill_ids=[skill["skill_id"]], hook_ids=[], subagent_ids=[]))
    result = publish(service, author, agent)
    resource = service.publications.get(result["id"])["manifest"]["resources"][0]
    app = FastAPI()
    @app.middleware("http")
    async def principal(request, call_next):
        request.state.principal = SimpleNamespace(user_id=request.headers.get("test-user", reader), username="测试用户", role="user")
        return await call_next(request)
    router = APIRouter(prefix="/api")
    register_publication_routes(router, SimpleNamespace(agent_config_service=service, settings=service.settings))
    app.include_router(router)
    with TestClient(app) as client:
        prefix = f"/api/agent-publications/{result['id']}"
        assert client.get(prefix).status_code == 200
        assert client.get(prefix).headers["cache-control"] == "no-store"
        current = client.get(f"{prefix}/resources/{resource['id']}").json()
        payload = {**skill_payload("已公开"), "expected_revision": current["revision"]}
        assert client.put(f"{prefix}/skills/{resource['id']}", json=payload).status_code == 403
        assert client.put(f"{prefix}/skills/{resource['id']}", json=payload, headers={"test-user": author}).status_code == 200
        assert client.get(f"{prefix}/resources/{resource['id']}/files", params={"path": "../private"}).status_code == 404
        assert client.post(prefix + "/block", json={"expected_revision": result["revision"]}).status_code == 403
        assert client.post(prefix + "/withdraw", json={"expected_revision": result["revision"]}, headers={"test-user": author}).status_code == 200
        assert client.get(prefix).status_code == 404


def test_failed_publish_can_retry_without_exposing_partial_agent(setup, monkeypatch):
    service, author, reader = setup
    skill = SkillStore(service.users_root.parent).save(author, None, skill_payload())
    agent = service.create(author, _payload(skill_ids=[skill["skill_id"]], hook_ids=[], subagent_ids=[]))
    preview = service.publications.preview(service, author, "作者", agent["agent_id"])
    original = SkillStore.save
    def fail(self, *args, **kwargs):
        if self.publication_id:
            raise OSError("测试磁盘错误")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SkillStore, "save", fail)
    args = (service, author, preview["candidate_id"], preview["digest"], None, "retry")
    with pytest.raises(OSError):
        service.publications.confirm(*args)
    assert service.publications.list(reader) == []
    monkeypatch.setattr(SkillStore, "save", original)
    result = service.publications.confirm(*args)
    assert service.get_active_profile_snapshot(reader, result["id"])


def test_public_http_credentials_belong_to_reader_and_target(setup, monkeypatch):
    from cryptography.fernet import Fernet
    from codepilot.session.connections import ConnectionStore
    monkeypatch.setenv("CODEPILOT_CONNECTION_KEY", Fernet.generate_key().decode())
    service, author, reader = setup
    hook = HookStore(service.users_root.parent).save(author, None, HookPayload.model_validate({"definition": {
        "name": "测试 HTTP", "plugin_type": "http", "hook_type": "llm.before", "url": "http://127.0.0.1:19999/hook", "credential_headers": {"Authorization": "token"}}}))
    agent = service.create(author, _payload(skill_ids=[], hook_ids=[hook["hook_id"]], subagent_ids=[]))
    published = publish(service, author, agent)
    resource = service.publications.get(published["id"])["manifest"]["resources"][0]
    target = f"hook:public:{published['id']}:{resource['id']}"
    connections = ConnectionStore(service.users_root.parent, service.settings)
    connection = connections.save(reader, None, target, {"token": "reader-value"}, None)
    with pytest.raises(AgentConfigError):
        connections.resolve(author, connection["connection_id"])
    service.publications.save_settings(reader, published["id"], {"working_directory": None, "connections": {published["id"]: [connection["connection_id"]]}}, None)
    profile = service.get_active_profile_snapshot(reader, published["id"])
    assert profile.resolved_connection_versions
    assert "reader-value" not in str(service.publications.view(service.publications.get(published["id"]), reader, True))
    public_hooks = HookStore(service.users_root.parent, publication_id=published["id"])
    identity = "personal:" + resource["id"]
    current = public_hooks.load(author, identity)
    public_hooks.save(author, identity, HookPayload.model_validate({"definition": {**current["definition"], "url": "http://127.0.0.1:19999/new"}, "expected_revision": current["revision"]}))
    with pytest.raises(AgentConfigError, match="变更"):
        connections.resolve(reader, connection["connection_id"])


def test_public_command_script_is_copied_and_uses_public_path(setup):
    service, author, reader = setup
    content = base64.b64encode(b'print("test")').decode()
    hook = HookStore(service.users_root.parent).save(author, None, HookPayload.model_validate({"definition": {
        "name": "脚本", "plugin_type": "command", "hook_type": "llm.before", "argv": ["python3", "@file:hook.py"]}, "files": {"hook.py": content}}))
    agent = service.create(author, _payload(skill_ids=[], hook_ids=[hook["hook_id"]], subagent_ids=[]))
    result = publish(service, author, agent)
    profile = service.get_active_profile_snapshot(reader, result["id"])
    resource = service.publications.get(result["id"])["manifest"]["resources"][0]
    public_hooks = HookStore(service.users_root.parent, publication_id=result["id"])
    instance = public_hooks.instantiate(reader, "personal:" + resource["id"], next(iter(profile.resolved_hook_versions.values())))
    assert "/publications/" in instance.config["argv"][1]
    assert author not in instance.config["argv"][1]


def test_competing_initial_previews_do_not_replace_identity(setup):
    service, author, reader = setup
    agent = service.create(author, _payload(skill_ids=[], hook_ids=[], subagent_ids=[]))
    previews = [service.publications.preview(service, author, "作者", agent["agent_id"]) for _ in range(2)]
    first = previews[0]
    result = service.publications.confirm(service, author, first["candidate_id"], first["digest"], None, "first")
    second = previews[1]
    with pytest.raises(AgentConfigError, match="其他预览"):
        service.publications.confirm(service, author, second["candidate_id"], second["digest"], None, "second")
    assert service.publications.list(reader)[0]["id"] == result["id"]


def test_public_new_run_uses_latest_model_in_existing_session(setup, tmp_path, monkeypatch):
    from test_agent_runtime_manager import build_manager
    from codepilot.gateway import GatewayInput
    service, author, reader = setup
    agent = service.create(author, _payload(skill_ids=[], hook_ids=[], subagent_ids=[]))
    result = publish(service, author, agent)
    profile = service.get_active_profile_snapshot(reader, result["id"])
    manager, _ = build_manager(tmp_path / "runtime")
    monkeypatch.setattr(manager, "_resolve_new_session_llm", lambda p, r: ("new-provider", "new-model", None))
    replay = {"session": {"data": {"user_id": reader, "agent_id": result["id"], "provider": "old", "model": "old"}}}
    value = manager._locked_session_llm(reader, result["id"], profile, replay, GatewayInput(type="user_message", content="继续", agent_name=profile.name))
    assert value == ("new-provider", "new-model", None)
