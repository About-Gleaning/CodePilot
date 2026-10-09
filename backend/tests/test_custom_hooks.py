import asyncio
import base64
import sys
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from codepilot.hooks import BaseHook, HookManager, HookResult, HookType
from codepilot.hooks.store import HookDefinition, HookPayload, HookStore
from codepilot.session import ApprovalRequest, ApprovalResult, SessionStatus
from codepilot.session.agent_config import AgentConfigError
from codepilot.session.connections import ConnectionStore
from codepilot.utils import utc_now_iso
from test_hook_plugins import context
from test_session_hooks import (AgentLoop, AgentProfile, EventBus, RuntimeHandles, SimpleNamespace,
                                StubLiteLLMClient, StubToolDispatcher, StubToolRegistry, build_session, build_settings)


def payload(content="初版", revision=None):
    return HookPayload(definition=HookDefinition(name="测试", plugin_type="prompt", hook_type="llm.before", content=content),
                       expected_revision=revision)


@pytest.mark.parametrize("kind", ["prompt", "http"])
def test_non_command_ignores_legacy_script_references(tmp_path, kind):
    store, owner = HookStore(tmp_path), str(uuid4())
    value = HookPayload(definition=HookDefinition(
        name="兼容旧表单", plugin_type=kind, hook_type="llm.before", content="检查",
        url="https://example.test/", argv=["{{unused}}", "@file:missing.py"]))
    first = store.save(owner, None, value)
    value.expected_revision = first["revision"]
    value.definition.description = "编辑后"
    saved = store.save(owner, first["hook_id"], value)
    assert store.load(owner, first["hook_id"], saved["version"])["definition"]["description"] == "编辑后"


def test_command_still_requires_referenced_script(tmp_path):
    value = HookPayload(definition=HookDefinition(
        name="缺失脚本", plugin_type="command", hook_type="llm.before",
        argv=[sys.executable, "@file:missing.py"]))
    with pytest.raises(AgentConfigError, match="命令引用的脚本文件不存在"):
        HookStore(tmp_path).save(str(uuid4()), None, value)


def test_hook_readable_numbers_follow_saves_not_content_hashes(tmp_path):
    store, owner = HookStore(tmp_path), str(uuid4())
    first = store.save(owner, None, payload())
    assert first["version_number"] == 1 and first["updated_at"]
    repeated = store.save(owner, first["hook_id"], payload(revision=first["revision"]))
    assert repeated == first
    second = store.save(owner, first["hook_id"], payload("第二版", first["revision"]))
    assert second["version_number"] == 2
    restored = store.save(owner, first["hook_id"], payload(revision=second["revision"]))
    assert restored["version"] == first["version"]
    assert restored["version_number"] == 3
    assert "version_number" not in store.load(owner, first["hook_id"], second["version"])
    assert store.save(owner, None, payload())["version_number"] == 1
    assert store.archive(owner, first["hook_id"], restored["revision"])["version_number"] == 3


def test_legacy_hook_numbers_start_from_current_content(tmp_path):
    store, owner = HookStore(tmp_path), str(uuid4())
    record = store.save(owner, None, payload())
    record.pop("version_number")
    record.pop("updated_at")
    store.files._publish(store.directory(owner, record["hook_id"]), record)
    assert store.get(owner, record["hook_id"])["version_number"] == 1
    updated = store.save(owner, record["hook_id"], payload("新正文", record["revision"]))
    assert updated["version_number"] == 2


def test_hook_versions_conflicts_ownership_archive_and_scripts(tmp_path):
    store, owner, other = HookStore(tmp_path), str(uuid4()), str(uuid4())
    first = store.save(owner, None, payload())
    identity = first["hook_id"]
    second = store.save(owner, identity, payload("第二版", first["revision"]))
    assert store.load(owner, identity, first["version"])["definition"]["content"] == "初版"
    with pytest.raises(AgentConfigError):
        store.save(owner, identity, payload("冲突", first["revision"]))
    with pytest.raises(AgentConfigError):
        store.load(other, identity)
    store.archive(owner, identity, second["revision"])
    with pytest.raises(AgentConfigError):
        store.load(owner, identity)
    assert store.load(owner, identity, second["version"])["definition"]["content"] == "第二版"
    value = HookPayload(definition=HookDefinition(name="脚本", plugin_type="command", hook_type="tool.before", argv=[sys.executable, "@file:check.py"]),
                        files={"check.py": base64.b64encode(b'print("ok")').decode()})
    record = store.save(owner, None, value)
    assert store.read(owner, record["hook_id"], record["version"], "check.py") == b'print("ok")'
    value.files = {"../escape": "YQ=="}
    with pytest.raises(AgentConfigError):
        store.save(owner, None, value)


def test_changed_http_target_never_receives_old_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_CONNECTION_KEY", Fernet.generate_key().decode())
    store, owner = HookStore(tmp_path), str(uuid4())
    value = HookPayload(definition=HookDefinition(name="HTTP", plugin_type="http", hook_type="llm.before",
                                                url="https://one.test/", credential_headers={"Authorization": "auth"}))
    hook = store.save(owner, None, value)
    connections = ConnectionStore(tmp_path, build_settings())
    record = connections.save(owner, None, f"hook:{hook['hook_id']}", {"auth": "secret"}, None)
    assert connections.resolve(owner, record["connection_id"])[1] == {"auth": "secret"}
    value.definition.url = "https://two.test/"
    value.expected_revision = hook["revision"]
    store.save(owner, hook["hook_id"], value)
    with pytest.raises(AgentConfigError):
        connections.resolve(owner, record["connection_id"])


class WaitingHook(BaseHook):
    async def execute(self, ctx):
        ctx.session.metadata.setdefault("trace", []).append(self.hook_id)
        return HookResult(requires_human_input=True, human_request=ApprovalRequest(
            approval_id=self.hook_id, reason="确认", created_at=utc_now_iso()))


class FollowingHook(BaseHook):
    async def execute(self, ctx):
        ctx.session.metadata.setdefault("trace", []).append(self.hook_id)
        return HookResult()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [HookType.LOOP_BEFORE, HookType.LLM_BEFORE, HookType.LLM_AFTER, HookType.LOOP_AFTER, HookType.SESSION_AFTER])
async def test_human_hook_resumes_same_turn_and_remaining_chain(stage):
    manager = HookManager()
    manager.register(WaitingHook(hook_id="first", hook_type=stage, name="等待", order=1))
    manager.register(FollowingHook(hook_id="second", hook_type=stage, name="后续", order=2))
    llm = StubLiteLLMClient()
    loop = AgentLoop(llm_client=llm, tool_registry=StubToolRegistry(), tool_dispatcher=StubToolDispatcher(), hook_manager=manager)
    session, bus = build_session(), EventBus()
    event, holder = asyncio.Event(), {"result": None}
    async def approve(message):
        if message.event_type == "human_approval_required":
            holder["result"] = ApprovalResult(approval_id="first", approved=True, created_at=utc_now_iso())
            event.set()
    bus.subscribe_stream(approve)
    await asyncio.wait_for(loop.run(session, SimpleNamespace(workspace_path="/tmp/codepilot"),
                                   AgentProfile(name="build", system_prompt="测试", max_iterations=1),
                                   RuntimeHandles(event_bus=bus), build_settings(), event, holder, asyncio.Event(),
                                   allow_manual_approval=False), 3)
    assert session.status == SessionStatus.COMPLETED
    assert llm.calls == 1
    assert session.metadata["trace"] == ["first", "second"]


@pytest.mark.asyncio
async def test_external_unknown_outcome_cannot_continue(tmp_path):
    from codepilot.hooks.plugins import CommandPluginHook
    hook = CommandPluginHook(hook_id="test", hook_type="loop.before", name="测试", protocol_version=1,
                             on_error="continue", config={"argv": [sys.executable, "-c", 'print("bad-json")']})
    manager = HookManager()
    manager.register(hook)
    result = await manager.run(HookType.LOOP_BEFORE, context(tmp_path))
    assert result.fail_session and result.error.code == "hook_outcome_uncertain"


def test_agent_snapshots_pin_hooks_and_archive_blocks_new_runs(tmp_path):
    from test_agent_config import _payload, _service
    from codepilot.session.agent_config import MultiUserAgentConfigService
    base = _service(tmp_path)
    service = MultiUserAgentConfigService(settings=base.settings, shared_root=tmp_path / "shared",
        users_root=tmp_path / "users", builtin_profiles={}, tool_registry=base.tool_registry,
        mcp_manager=base.mcp_manager, workspace_path=tmp_path)
    owner, store = str(uuid4()), HookStore(tmp_path)
    hook = store.save(owner, None, payload())
    record = service.create(owner, _payload(hook_ids=[hook["hook_id"]]))
    assert record["validation_status"] == "valid"
    frozen = service.get_active_profile_snapshot(owner, record["agent_id"])
    changed = store.save(owner, hook["hook_id"], payload("新版", hook["revision"]))
    assert service.resolve_execution_profile(owner, frozen).resolved_hook_versions == {hook["hook_id"]: hook["version"]}
    assert service.get_active_profile_snapshot(owner, record["agent_id"]).resolved_hook_versions == {hook["hook_id"]: changed["version"]}
    store.archive(owner, hook["hook_id"], changed["revision"])
    assert service.get(owner, record["agent_id"])["validation_status"] == "needs_configuration"
    with pytest.raises(AgentConfigError):
        service.get_active_profile_snapshot(owner, record["agent_id"])


@pytest.mark.asyncio
async def test_hook_test_receipt_retry_restart_and_cancellation(tmp_path):
    from codepilot.config.workspace import WorkspaceState
    from codepilot.hooks.testing import HookTestService, TestPayload as Payload
    workspace = WorkspaceState("ws", tmp_path, tmp_path / "home", tmp_path / "runtime",
                               tmp_path / "sessions", tmp_path / "logs", tmp_path / "workspace.json")
    service, owner = HookTestService(workspace, build_settings()), str(uuid4())
    value = HookPayload(definition=HookDefinition(name="等待脚本", plugin_type="command", hook_type="llm.before",
        argv=[sys.executable, "-c", 'import time; time.sleep(30)']))
    hook = service.store.save(owner, None, value)
    request = Payload(version=hook["version"], client_request_id="request")
    record = await service.start(owner, hook["hook_id"], request)
    assert (await service.start(owner, hook["hook_id"], request))["test_id"] == record["test_id"]
    with pytest.raises(AgentConfigError):
        await service.start(owner, hook["hook_id"], request.model_copy(update={"client_request_id": "other"}))
    await asyncio.sleep(0.05)
    assert (await service.cancel(owner, record["test_id"]))["status"] == "cancelled"
    restarted = HookTestService(workspace, build_settings())
    assert (await restarted.start(owner, hook["hook_id"], request))["status"] == "cancelled"
    assert not restarted.tasks
    with pytest.raises(AgentConfigError):
        await restarted.get(str(uuid4()), record["test_id"])
    with pytest.raises(AgentConfigError):
        await restarted.start(owner, hook["hook_id"], request.model_copy(update={"tool_name": "different"}))


def test_hook_failed_publish_and_symlink_preserve_version(tmp_path, monkeypatch):
    store, owner = HookStore(tmp_path), str(uuid4())
    hook = store.save(owner, None, payload())
    original = store.files._write
    def fail(path, data):
        if path.name == "check.py":
            raise OSError("模拟发布失败")
        return original(path, data)
    monkeypatch.setattr(store.files, "_write", fail)
    changed = payload("下一版", hook["revision"])
    changed.files = {"check.py": "YQ=="}
    with pytest.raises(OSError):
        store.save(owner, hook["hook_id"], changed)
    assert store.get(owner, hook["hook_id"])["revision"] == hook["revision"]
    (store.directory(owner, hook["hook_id"]) / "versions" / hook["version"] / "files").symlink_to(tmp_path)
    with pytest.raises(AgentConfigError):
        store.files.resource_path(store.directory(owner, hook["hook_id"]), f"versions/{hook['version']}/files/check.py")


def test_hook_http_api_isolation_cas_and_redacted_platform_detail(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from codepilot.api.hook_routes import register_hook_routes
    from codepilot.config.workspace import WorkspaceState
    from codepilot.session.agent_config import MultiUserAgentConfigService
    from test_agent_config import _service
    base = _service(tmp_path)
    service = MultiUserAgentConfigService(settings=base.settings, shared_root=tmp_path / "shared", users_root=tmp_path / "users",
        builtin_profiles={}, tool_registry=base.tool_registry, mcp_manager=base.mcp_manager, workspace_path=tmp_path)
    workspace = WorkspaceState("ws", tmp_path, tmp_path, tmp_path / "runtime", tmp_path / "sessions", tmp_path / "logs", tmp_path / "meta.json")
    app, owner, other = FastAPI(), str(uuid4()), str(uuid4())
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.principal = SimpleNamespace(user_id=request.headers.get("test-owner", owner))
        return await call_next(request)
    register_hook_routes(app, SimpleNamespace(workspace=workspace, settings=base.settings, agent_config_service=service))
    with TestClient(app) as client:
        created = client.post("/hooks", json=payload().model_dump(mode="json"))
        assert created.status_code == 201
        hook = created.json()
        assert created.headers["cache-control"] == "no-store"
        assert client.get(f"/hooks/{hook['hook_id']}", headers={"test-owner": other}).status_code == 404
        assert client.get("/hooks?q=测试").json()["total"] == 1
        assert client.get(f"/hooks/{hook['hook_id']}/references").json() == {"agents": []}
        assert client.put(f"/hooks/{hook['hook_id']}", json=payload().model_dump(mode="json")).status_code == 409
        invalid = payload().model_dump(mode="json")
        invalid["definition"]["hook_type"] = "session.after"
        assert client.post("/hooks", json=invalid).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [HookType.TOOL_BEFORE, HookType.TOOL_AFTER])
async def test_tool_hook_approval_keeps_facts_and_serial_order(tmp_path, stage):
    from test_hook_plugins import CountingTool
    from codepilot.tools import ToolDispatcher, ToolRegistry
    manager, registry, ctx = HookManager(), ToolRegistry(), context(tmp_path)
    tool = CountingTool()
    tool.spec = tool.spec.model_copy(update={"can_parallel": True})
    registry.register(tool)
    manager.register(WaitingHook(hook_id="first", hook_type=stage, name="等待", selectable=True, order=1))
    manager.register(FollowingHook(hook_id="second", hook_type=stage, name="后续", selectable=True, order=2))
    ctx.agent.allowed_tools = ["counter"]
    async def approved(result):
        await asyncio.sleep(0)
        return result.model_copy(update={"requires_human_input": False, "human_request": None})
    ctx.runtime.hook_approval = approved
    batch = await ToolDispatcher(registry, manager).execute_tool_calls(session=ctx.session, workspace=ctx.workspace,
        agent=ctx.agent, runtime=ctx.runtime, config=SimpleNamespace(),
        tool_calls=[{"tool_name": "counter", "tool_call_id": str(i), "arguments": {}} for i in range(2)])
    assert tool.calls == 2
    assert len(batch.tool_parts) == 2
    assert ctx.session.metadata["trace"] == ["first", "second", "first", "second"]


@pytest.mark.asyncio
async def test_http_public_protocol_and_unknown_result_fail_closed(tmp_path, monkeypatch):
    import httpx
    from codepilot.hooks.plugins import HttpPluginHook
    original = httpx.AsyncClient
    def handler(request):
        import json
        data = json.loads(request.content)
        assert data["protocol_version"] == 1
        assert "password" not in data["tool_call"]["args"]
        return httpx.Response(200, json={"protocol_version": 1, "action": "continue", "tool_calls": []})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    ctx = context(tmp_path)
    ctx.tool_call = {"args": {"password": "do-not-send", "path": "test"}}
    manager = HookManager()
    manager.register(HttpPluginHook(hook_id="http", hook_type="loop.before", name="HTTP", protocol_version=1,
                                    on_error="continue", config={"url": "https://test.invalid/"}))
    result = await manager.run(HookType.LOOP_BEFORE, ctx)
    assert result.fail_session and result.error.code == "hook_outcome_uncertain"


@pytest.mark.asyncio
async def test_personal_hook_snapshot_reaches_model_request(tmp_path):
    owner, store = str(uuid4()), HookStore(tmp_path)
    hook = store.save(owner, None, payload("固定版本提示"))
    store.save(owner, hook["hook_id"], payload("后来修改的提示", hook["revision"]))
    profile = AgentProfile(name="build", system_prompt="测试", hook_ids=[hook["hook_id"]],
        resolved_hook_versions={hook["hook_id"]: hook["version"]}, max_iterations=1, skill_ids=[])
    manager, llm = HookManager(), StubLiteLLMClient()
    from codepilot.tools import ToolRegistry, ToolDispatcher
    registry = ToolRegistry()
    loop = AgentLoop(llm_client=llm, tool_registry=registry, tool_dispatcher=ToolDispatcher(registry, manager), hook_manager=manager)
    workspace = SimpleNamespace(workspace_path=tmp_path, codepilot_home=tmp_path, user_id=owner)
    session = build_session()
    await loop.run(session, workspace, profile, RuntimeHandles(event_bus=EventBus()), build_settings(),
                   asyncio.Event(), {"result": None}, asyncio.Event())
    messages = [part.text for message in llm.last_provider_messages if hasattr(message, "parts") for part in message.parts if hasattr(part, "text")]
    assert any("固定版本提示" in value for value in messages)
    assert not any("后来修改的提示" in value for value in messages)
    assert not manager.get_hooks(HookType.LLM_BEFORE)


@pytest.mark.asyncio
async def test_public_command_stop_and_minimal_environment(tmp_path, monkeypatch):
    from codepilot.hooks.plugins import CommandPluginHook
    from codepilot.session.interactions import SessionMessageAppender
    monkeypatch.setenv("HOOK_TEST_PRIVATE_KEY", "must-not-inherit")
    hook = CommandPluginHook(hook_id="test", hook_type="loop.before", name="测试", protocol_version=1,
        config={"argv": [sys.executable, "-c", 'import os,json; assert "HOOK_TEST_PRIVATE_KEY" not in os.environ; print(json.dumps({"protocol_version":1,"action":"stop"}))']})
    ctx = context(tmp_path)
    result = await hook.execute(ctx)
    await SessionMessageAppender().apply_hook_result(ctx.session, result, ctx.runtime)
    assert ctx.session.status == SessionStatus.CANCELLED
    assert ctx.session.stop_reason == "hook_stopped"


@pytest.mark.asyncio
async def test_stop_interrupts_hook_human_wait_without_replay():
    manager, llm = HookManager(), StubLiteLLMClient()
    manager.register(WaitingHook(hook_id="wait", hook_type="llm.before", name="等待"))
    loop = AgentLoop(llm_client=llm, tool_registry=StubToolRegistry(), tool_dispatcher=StubToolDispatcher(), hook_manager=manager)
    session, stop, bus = build_session(), asyncio.Event(), EventBus()
    async def cancel(message):
        if message.event_type == "human_approval_required":
            stop.set()
    bus.subscribe_stream(cancel)
    await asyncio.wait_for(loop.run(session, SimpleNamespace(workspace_path="/tmp/codepilot"),
        AgentProfile(name="build", system_prompt="测试", max_iterations=1), RuntimeHandles(event_bus=bus),
        build_settings(), asyncio.Event(), {"result": None}, stop), 2)
    assert session.status == SessionStatus.CANCELLED
    assert llm.calls == 0
    assert session.metadata["trace"] == ["wait"]
