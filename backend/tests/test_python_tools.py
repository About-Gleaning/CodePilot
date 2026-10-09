"""代码工具的隔离、发布与旧工具兼容回归。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codepilot.session.agent_config import AgentConfigError
from codepilot.tools.code_store import ToolStore
from codepilot.tools.python_code_tool import PythonCodeTool
from codepilot.tools.base import ToolExecutionContext
from codepilot.tools.registry import ToolRegistry
from codepilot.tools.read_file_tool import ReadFileTool


def payload(code='def execute(arguments):\n    return {"value": arguments["value"]}\n'):
    return {"definition": {"name": "示例工具", "description": "返回参数", "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}}, "files": {"main.py": code}}


def publish(store, user, record):
    preview = store.preview(user, record['tool_id'], record['revision'])
    return store.publish(user, record['tool_id'], preview['token'])


def named_payload(name='test_tool'):
    item = payload()
    item['definition']['call_name'] = name
    return item


@pytest.mark.parametrize('name', ['', None, '中文', 'test-tool', '1tool', 'x' * 65, 'mcp__demo', 'code_tool_demo'])
def test_invalid_call_names_are_rejected(tmp_path, name):
    with pytest.raises(AgentConfigError):
        ToolStore(tmp_path).save(str(uuid4()), None, named_payload(name))


@pytest.mark.parametrize('name', ['test_tool', '_tool2', 'T', 'x' * 64])
def test_valid_call_name_boundaries(tmp_path, name):
    store = ToolStore(tmp_path)
    user = str(uuid4())
    record = store.save(user, None, named_payload(name))
    assert PythonCodeTool(store, user, record['tool_id'], record['version']).spec.name == name


@pytest.mark.asyncio
async def test_http_call_name_versions_and_publication(tmp_path):
    from fastapi import APIRouter, FastAPI, Request
    from fastapi.testclient import TestClient
    from codepilot.api.tool_routes import register_tool_routes
    from codepilot.config.settings import AppSettings
    from codepilot.tools.code_store import call_name
    user = str(uuid4())
    app = FastAPI()
    @app.middleware('http')
    async def principal(request: Request, call_next):
        request.state.principal = SimpleNamespace(user_id=user, role='user')
        return await call_next(request)
    router = APIRouter()
    register_tool_routes(router, SimpleNamespace(workspace=SimpleNamespace(codepilot_home=tmp_path), settings=AppSettings()))
    app.include_router(router)
    store = ToolStore(tmp_path)
    with TestClient(app) as client:
        assert client.post('/tools', json=payload()).status_code == 422
        created = client.post('/tools', json=named_payload()).json()
        assert created['call_name'] == 'test_tool'
        # 模拟升级前已存在的版本，不允许新 HTTP 请求继续创建不可读名称。
        legacy = store.save(user, None, payload())
        identity = legacy['tool_id']
        old_path = store._version_path(identity, legacy['version'])
        old_bytes = old_path.read_bytes()
        old_tool = PythonCodeTool(store, user, identity, legacy['version'])
        assert old_tool.spec.name == call_name(identity)
        item = named_payload()
        item['expected_revision'] = legacy['revision']
        response = client.put('/tools/' + identity, json=item)
        assert response.status_code == 200
        saved = response.json()
        assert saved['call_name'] == 'test_tool'
        detail = client.get('/tools/' + identity).json()
        assert detail['definition']['name'] == '示例工具'
        assert detail['definition']['call_name'] == 'test_tool'
        assert next(item for item in client.get('/tools').json()['tools'] if item['tool_id'] == identity)['call_name'] == 'test_tool'
        preview = client.post('/tools/' + identity + '/publication-preview', json={'expected_revision': saved['revision']}).json()
        assert preview['call_name'] == preview['definition']['call_name'] == 'test_tool'
        public = client.post('/tools/' + identity + '/publish', json={'token': preview['token']}).json()
        tool = PythonCodeTool(store, user, identity, saved['version'])
        agent = SimpleNamespace(tool_ids=[identity], readonly=False)
        registry = ToolRegistry(); registry.register(tool)
        assert registry.get_llm_tool_schemas([], agent_profile=agent)[0]['function']['name'] == 'test_tool'
        context = ToolExecutionContext(None, SimpleNamespace(workspace_path=tmp_path, user_id=user), agent)
        assert (await tool.execute({'value': 7}, context)) == {'status': 'ok', 'tool_name': 'test_tool', 'output': {'value': 7}}
        assert registry.get(call_name(identity)) is None
        # 旧客户端完整覆盖其他字段，但省略调用名称时保留已配置值。
        omitted = payload(); omitted['expected_revision'] = saved['revision']
        retained = client.put('/tools/' + identity, json=omitted).json()
        assert client.get('/tools/' + identity).json()['definition']['call_name'] == 'test_tool'
        renamed = named_payload('renamed_tool'); renamed['expected_revision'] = retained['revision']
        updated = client.put('/tools/' + identity, json=renamed).json()
        assert PythonCodeTool(store, user, identity, updated['version']).spec.name == 'renamed_tool'
        assert tool.spec.name == 'test_tool' and old_tool.spec.name == call_name(identity)
        assert old_path.read_bytes() == old_bytes
        assert PythonCodeTool(store, user, identity, legacy['version']).spec.name == call_name(identity)
        from codepilot.session.message import ToolPart, ToolPartState
        from codepilot.llm.client import LiteLLMClient
        # 改名后重新读取历史工具片段，Provider 请求仍保留当时的调用名称。
        history = ToolPart(call_id='legacy-call', tool=old_tool.spec.name, state=ToolPartState(status='completed', input={'value': 1}, output={'value': 1}))
        history_path = tmp_path / 'history.json'
        history_path.write_text(history.model_dump_json())
        restored = ToolPart.model_validate_json(history_path.read_text())
        llm = LiteLLMClient()
        assert llm._build_provider_tool_call(restored)['function']['name'] == call_name(identity)
        assert llm._build_provider_tool_results([restored])[0]['name'] == call_name(identity)
        other = str(uuid4())
        assert PythonCodeTool(store, other, public['tool_id'], public['version']).spec.name == 'test_tool'
        republished = publish(store, user, updated)
        assert PythonCodeTool(store, other, republished['tool_id'], republished['version']).spec.name == 'renamed_tool'
        invalid = named_payload('私密非法名称'); invalid['expected_revision'] = updated['revision']
        failed = client.put('/tools/' + identity, json=invalid)
        assert failed.status_code == 422 and '私密' not in failed.text


@pytest.mark.parametrize('kind', ['platform', 'mcp', 'code'])
@pytest.mark.parametrize('reverse', [False, True])
def test_registry_call_name_collisions_never_replace(tmp_path, kind, reverse):
    store = ToolStore(tmp_path)
    user = str(uuid4())
    record = store.save(user, None, named_payload())
    tool = PythonCodeTool(store, user, record['tool_id'], record['version'])
    if kind == 'code':
        other = store.save(user, None, named_payload())
        existing = PythonCodeTool(store, user, other['tool_id'], other['version'])
    else:
        existing = ReadFileTool(10)
        existing.spec.name = 'test_tool'
        if kind == 'mcp':
            existing.mcp_server_name = 'demo'
    first, second = (tool, existing) if reverse else (existing, tool)
    registry = ToolRegistry(); registry.register(first)
    with pytest.raises(AgentConfigError) as caught:
        registry.register(second)
    assert caught.value.code == 'tool_name_conflict'
    assert registry.get('test_tool') is first


@pytest.mark.parametrize('platform', [False, True])
def test_config_reports_call_name_conflicts_before_execution(tmp_path, platform):
    from test_agent_config import _service, _payload
    from codepilot.session.agent_config import MultiUserAgentConfigService
    base = _service(tmp_path)
    service = MultiUserAgentConfigService(settings=base.settings, shared_root=tmp_path / 'shared', users_root=tmp_path / 'users', builtin_profiles={}, tool_registry=base.tool_registry, mcp_manager=base.mcp_manager)
    user = str(uuid4())
    store = ToolStore(tmp_path)
    first = store.save(user, None, named_payload('read_file' if platform else 'test_tool'))
    identities = [first['tool_id']]
    if not platform:
        identities.append(store.save(user, None, named_payload())['tool_id'])
    agent = service.create(user, _payload(tool_ids=identities, skill_ids=[], hook_ids=[], delegate_agent_ids=[]))
    assert agent['validation_status'] == 'needs_configuration'
    issue = next(item for item in agent['validation_issues'] if item['code'] == 'tool_name_conflict')
    assert issue['field'] == 'tool_ids'
    with pytest.raises(AgentConfigError, match='调用名称'):
        service.get_active_profile_snapshot(user, agent['agent_id'])


def test_store_publication_and_ownership(tmp_path):
    store = ToolStore(tmp_path)
    author, other = str(uuid4()), str(uuid4())
    record = store.save(author, None, payload())
    with pytest.raises(AgentConfigError):
        store.load(other, record['tool_id'])
    public = publish(store, author, record)
    assert store.load(other, public['tool_id'])['files'] == payload()['files']
    changed = payload('def execute(arguments):\n    return {"new": True}\n')
    changed['expected_revision'] = record['revision']
    store.save(author, record['tool_id'], changed)
    assert store.load(other, public['tool_id'])['files'] == payload()['files']
    with pytest.raises(AgentConfigError):
        store.save(other, public['tool_id'], changed)


def test_save_does_not_execute_and_paths_checked(tmp_path):
    store = ToolStore(tmp_path)
    user = str(uuid4())
    item = payload('raise RuntimeError("不可在保存时执行")\ndef execute(arguments):\n    return {}\n')
    store.save(user, None, item)
    item['files']['../escape.py'] = 'pass'
    with pytest.raises(AgentConfigError):
        store.save(user, None, item)


@pytest.mark.asyncio
async def test_adapter_and_registry_compatibility(tmp_path):
    store = ToolStore(tmp_path / 'home')
    user = str(uuid4())
    record = store.save(user, None, payload())
    tool = PythonCodeTool(store, user, record['tool_id'], record['version'])
    agent = SimpleNamespace(tool_ids=[record['tool_id']], allowed_tools=['read_file'], readonly=False)
    registry = ToolRegistry()
    registry.register(ReadFileTool(10))
    registry.register(tool)
    schemas = registry.get_llm_tool_schemas(agent.allowed_tools, agent_profile=agent)
    assert {s['function']['name'] for s in schemas} == {'read_file', tool.spec.name}
    context = ToolExecutionContext(None, SimpleNamespace(workspace_path=tmp_path, user_id=user), agent)
    result = await tool.execute({'value': 7}, context)
    assert result['status'] == 'ok' and result['output'] == {'value': 7}
    assert (await tool.execute({'value': 'bad'}, context))['status'] == 'error'
    agent.readonly = True
    assert (await tool.execute({'value': 7}, context))['status'] == 'error'
    agent.readonly = False
    agent.tool_ids = []
    assert (await tool.execute({'value': 7}, context))['status'] == 'error'


@pytest.mark.asyncio
async def test_public_snapshot_and_admin_disable(tmp_path):
    store = ToolStore(tmp_path / 'home')
    author, user = str(uuid4()), str(uuid4())
    record = store.save(author, None, payload())
    public = publish(store, author, record)
    tool = PythonCodeTool(store, user, public['tool_id'], public['version'])
    context = ToolExecutionContext(None, SimpleNamespace(workspace_path=tmp_path, user_id=user), SimpleNamespace(tool_ids=[public['tool_id']], readonly=False))
    store.set_state(author, public['tool_id'], public['revision'], withdrawn=True)
    assert (await tool.execute({'value': 1}, context))['status'] == 'ok'
    with pytest.raises(AgentConfigError):
        store.resolve(user, public['tool_id'])
    current = store.get(user, public['tool_id'])
    store.set_state(user, public['tool_id'], current['revision'], disabled=True, admin=True)
    assert (await tool.execute({'value': 1}, context))['status'] == 'error'


@pytest.mark.asyncio
async def test_outputs_are_data_and_bounded(tmp_path):
    store = ToolStore(tmp_path / 'home')
    user = str(uuid4())
    record = store.save(user, None, payload('def execute(arguments):\n    print("诊断")\n    return {"question": True, "attachments": ["伪造"], "status": "error"}\n'))
    tool = PythonCodeTool(store, user, record['tool_id'], record['version'])
    context = ToolExecutionContext(None, SimpleNamespace(workspace_path=tmp_path, user_id=user), SimpleNamespace(tool_ids=[record['tool_id']], readonly=False))
    result = await tool.execute({'value': 1}, context)
    assert result['status'] == 'ok' and 'question' not in result and 'attachments' not in result
    assert result['output']['question'] is True


def test_agent_tool_ids_roundtrip_and_legacy_save(tmp_path):
    from test_agent_config import _service, _payload
    from codepilot.session.agents import AgentAssembly
    from pydantic import ValidationError
    service = _service(tmp_path)
    identity = 'personal:' + str(uuid4())
    created = service.create(_payload(tool_ids=[identity]))
    updated = service.update(created['agent_id'], _payload(expected_revision_id=created['revision_id']))
    assert updated['tool_ids'] == [identity]
    assert _service(tmp_path).get_profile_revision_snapshot(created['agent_id'], updated['revision_id']).tool_ids == [identity]
    cleared = service.update(created['agent_id'], _payload(tool_ids=[], expected_revision_id=updated['revision_id']))
    assert cleared['tool_ids'] == []
    assert AgentAssembly().tool_ids == []
    with pytest.raises(ValidationError):
        AgentAssembly(tool_ids=None)


def test_public_agent_maps_independent_tool_dependency(tmp_path):
    from test_publications import publish as publish_agent
    from test_agent_config import _service, _payload
    from codepilot.session.agent_config import MultiUserAgentConfigService
    base = _service(tmp_path)
    service = MultiUserAgentConfigService(settings=base.settings, shared_root=tmp_path / 'shared', users_root=tmp_path / 'users', builtin_profiles={}, tool_registry=base.tool_registry, mcp_manager=base.mcp_manager)
    author, other = str(uuid4()), str(uuid4())
    store = ToolStore(tmp_path)
    tool = store.save(author, None, payload())
    agent = service.create(author, _payload(tool_ids=[tool['tool_id']], skill_ids=[], hook_ids=[], delegate_agent_ids=[]))
    with pytest.raises(AgentConfigError):
        publish_agent(service, author, agent)
    public_tool = publish(store, author, tool)
    public_agent = publish_agent(service, author, agent)
    profile = service.get_active_profile_snapshot(other, public_agent['id'])
    assert profile.tool_ids == [public_tool['tool_id']]
    assert profile.resolved_tool_versions == {public_tool['tool_id']: public_tool['version']}
    assert service.get(author, agent['agent_id'])['tool_ids'] == [tool['tool_id']]


def test_publish_token_retry_and_schema_no_network(tmp_path):
    store = ToolStore(tmp_path)
    user = str(uuid4())
    record = store.save(user, None, payload())
    preview = store.preview(user, record['tool_id'], record['revision'])
    first = store.publish(user, record['tool_id'], preview['token'])
    assert store.publish(user, record['tool_id'], preview['token']) == first
    item = payload()
    item['definition']['input_schema']['properties']['value'] = {'$ref': 'https://example.invalid/schema'}
    with pytest.raises(AgentConfigError):
        store.save(user, None, item)


@pytest.mark.asyncio
async def test_credentials_isolation_and_revocation(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from codepilot.session.connections import ConnectionStore
    from codepilot.config.settings import AppSettings
    monkeypatch.setenv('CODEPILOT_CONNECTION_KEY', Fernet.generate_key().decode())
    user, other = str(uuid4()), str(uuid4())
    store = ToolStore(tmp_path)
    item = payload('import os\ndef execute(arguments):\n    return {"received": bool(os.environ.get("CODEPILOT_TOOL_CREDENTIAL_token")), "platform_key": "CODEPILOT_CONNECTION_KEY" in os.environ}\n')
    item['definition']['credential_fields'] = ['token']
    record = store.save(user, None, item)
    connections = ConnectionStore(tmp_path, AppSettings())
    saved = connections.save(user, None, 'tool:' + record['tool_id'], {'token': 'private-value'}, None)
    with pytest.raises(AgentConfigError):
        connections.resolve(other, saved['connection_id'])
    tool = PythonCodeTool(store, user, record['tool_id'], record['version'], lambda: connections.resolve(user, saved['connection_id'], saved['revision']))
    context = ToolExecutionContext(None, SimpleNamespace(workspace_path=tmp_path, user_id=user), SimpleNamespace(tool_ids=[record['tool_id']], readonly=False))
    result = await tool.execute({'value': 1}, context)
    assert result['output'] == {'received': True, 'platform_key': False}
    connections.revoke(user, saved['connection_id'], saved['revision'])
    assert (await tool.execute({'value': 1}, context))['status'] == 'error'


@pytest.mark.asyncio
async def test_timeout_and_diagnostic_limit(tmp_path):
    user = str(uuid4())
    store = ToolStore(tmp_path / 'home')
    for code in ('import time\ndef execute(arguments):\n    time.sleep(5)\n    return {}\n', 'def execute(arguments):\n    print("x" * 70000)\n    return {}\n'):
        item = payload(code)
        item['definition']['timeout_seconds'] = 1
        record = store.save(user, None, item)
        tool = PythonCodeTool(store, user, record['tool_id'], record['version'])
        ctx = ToolExecutionContext(None, SimpleNamespace(workspace_path=tmp_path, user_id=user), SimpleNamespace(tool_ids=[record['tool_id']], readonly=False))
        result = await tool.execute({'value': 1}, ctx)
        assert result['status'] == 'error' and result['recoverable'] is False


@pytest.mark.asyncio
async def test_context_assembly_preserves_builtin_tools(tmp_path):
    from codepilot.session.assembly import assemble_loop
    from codepilot.session.agents import AgentProfile
    from codepilot.tools.dispatcher import ToolDispatcher
    from codepilot.hooks import HookManager, RuntimeHandles
    from codepilot.events import EventBus
    from codepilot.session.flow import TurnExecutor
    from codepilot.config.settings import AppSettings
    user = str(uuid4())
    store = ToolStore(tmp_path)
    record = store.save(user, None, named_payload())
    profile = AgentProfile(name='demo', system_prompt='测试', allowed_tools=['read_file'], tool_ids=[record['tool_id']], resolved_tool_versions={record['tool_id']: record['version']})
    registry = ToolRegistry()
    reader = ReadFileTool(10)
    registry.register(reader)
    hooks = HookManager()
    executor = TurnExecutor(None, registry, ToolDispatcher(registry, hooks), hooks, None)
    loop = SimpleNamespace(_turn_executor=executor)
    workspace = SimpleNamespace(codepilot_home=tmp_path, user_id=user, workspace_path=tmp_path)
    async with assemble_loop(loop, profile, workspace, AppSettings(), RuntimeHandles(event_bus=EventBus())) as scoped:
        scoped_registry = scoped._turn_executor.tool_registry
        assert scoped_registry.get('read_file') is reader
        assert scoped_registry.get(PythonCodeTool(store, user, record['tool_id'], record['version']).spec.name) is not None
        # 父子绑定同一身份时重新按子快照装配，不能重复注册或继承旧代码。
        changed = payload('def execute(arguments):\n    return {"child": True}\n')
        changed['definition']['call_name'] = 'child_tool'
        changed['expected_revision'] = record['revision']
        updated = store.save(user, record['tool_id'], changed)
        child = profile.model_copy(update={'resolved_tool_versions': {record['tool_id']: updated['version']}})
        async with assemble_loop(scoped, child, workspace, AppSettings(), RuntimeHandles(event_bus=EventBus())) as delegated:
            adapter = delegated._turn_executor.tool_registry.get(PythonCodeTool(store, user, record['tool_id'], updated['version']).spec.name)
            context = ToolExecutionContext(None, workspace, child)
            assert (await adapter.execute({'value': 1}, context))['output'] == {'child': True}
        parent_adapter = scoped_registry.get(PythonCodeTool(store, user, record['tool_id'], record['version']).spec.name)
        assert parent_adapter.spec.name == 'test_tool'
        assert scoped_registry.get('child_tool') is None
        assert (await parent_adapter.execute({'value': 1}, ToolExecutionContext(None, workspace, profile)))['output'] == {'value': 1}
    assert len(registry._tools) == 1


@pytest.mark.asyncio
async def test_explicit_tests_idempotency_cancellation_and_restart(tmp_path):
    from codepilot.config.workspace import WorkspaceState
    from codepilot.config.settings import AppSettings
    from codepilot.tools.code_testing import ToolTestService, ToolTestPayload
    workspace = WorkspaceState(codepilot_home=tmp_path / 'home', workspace_id='test', workspace_dir=tmp_path / 'data', workspace_path=tmp_path, sessions_dir=tmp_path / 'sessions', logs_dir=tmp_path / 'logs', workspace_meta_file=tmp_path / 'meta.json')
    settings = AppSettings()
    tests = ToolTestService(workspace, settings)
    user = str(uuid4())
    record = tests.store.save(user, None, payload('import time\ndef execute(arguments):\n    time.sleep(10)\n    return {}\n'))
    request = ToolTestPayload(version=record['version'], client_request_id='stable', arguments={'value': 1})
    first = await tests.start(user, record['tool_id'], request)
    assert (await tests.start(user, record['tool_id'], request))['test_id'] == first['test_id']
    with pytest.raises(AgentConfigError):
        await tests.start(user, record['tool_id'], request.model_copy(update={'arguments': {'value': 2}}))
    await asyncio.sleep(0.05)
    assert (await tests.cancel(user, first['test_id']))['status'] == 'cancelled'
    assert (await tests.start(user, record['tool_id'], request))['status'] == 'cancelled'
    old = {**first, 'test_id': 'old', 'status': 'running'}
    tests._write(user, old)
    assert (await tests.get(user, 'old'))['status'] == 'interrupted'
    immediate = await tests.start(user, record['tool_id'], request.model_copy(update={'client_request_id': 'immediate'}))
    tests.tasks[(user, immediate['test_id'])].cancel()
    assert (await tests.cancel(user, immediate['test_id']))['status'] == 'cancelled'
    assert not tests.tasks
    await tests.shutdown()


@pytest.mark.parametrize('version', [1, 2, 3, 4, 5])
def test_worker_reads_all_bundle_versions(tmp_path, version):
    import json
    from codepilot.scheduler.worker import _read_execution_bundle
    from codepilot.session.agents import AgentProfile
    profile = AgentProfile(name='legacy', system_prompt='测试', allowed_tools=['read_file'])
    data = profile.model_dump()
    data.pop('tool_ids')
    data.pop('resolved_tool_versions')
    path = tmp_path / 'bundle.json'
    path.write_text(json.dumps({'schema_version': version, 'prompt': '测试', 'profile': data}))
    _, restored = _read_execution_bundle(path)
    assert restored.tool_ids == [] and restored.allowed_tools == ['read_file']


def test_tool_api_validation_does_not_echo_source(tmp_path):
    from fastapi import APIRouter, FastAPI, Request
    from fastapi.testclient import TestClient
    from codepilot.api.tool_routes import register_tool_routes
    from codepilot.config.settings import AppSettings
    user = str(uuid4())
    app = FastAPI()
    @app.middleware('http')
    async def principal(request: Request, call_next):
        request.state.principal = SimpleNamespace(user_id=user, role='user')
        return await call_next(request)
    router = APIRouter()
    register_tool_routes(router, SimpleNamespace(workspace=SimpleNamespace(codepilot_home=tmp_path), settings=AppSettings()))
    app.include_router(router)
    with TestClient(app) as client:
        item = payload('私密错误源码')
        item['definition']['timeout_seconds'] = '私密字段'
        response = client.post('/tools', json=item)
        assert response.status_code == 422
        assert '私密' not in response.text
        response = client.post('/tools', json=named_payload())
        assert response.status_code == 200
        identity = response.json()['tool_id']
        forbidden = client.post('/tools/' + identity + '/state', json={'expected_revision': response.json()['revision'], 'disabled': True})
        assert forbidden.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('name', [None, 'test_tool'])
async def test_dispatcher_approval_hooks_and_revocation(tmp_path, name):
    from codepilot.events import EventBus
    from codepilot.hooks import BaseHook, HookManager, HookType, HookResult, RuntimeHandles
    from codepilot.session import AgentState
    from codepilot.tools.dispatcher import ToolDispatcher
    from codepilot.tools.workspace_lease import get_workspace_write_lease_manager
    from codepilot.session import SessionState, SessionStatus
    from codepilot.config.settings import AppSettings
    from codepilot.utils import utc_now_iso
    class Trace(BaseHook):
        async def execute(self, ctx):
            ctx.session.metadata.setdefault('trace', []).append(self.hook_type.value)
            return HookResult()
    user = str(uuid4())
    store = ToolStore(tmp_path / 'home')
    record = store.save(user, None, payload() if name is None else named_payload(name))
    tool = PythonCodeTool(store, user, record['tool_id'], record['version'])
    registry = ToolRegistry(); registry.register(tool)
    hooks = HookManager()
    for stage in (HookType.TOOL_BEFORE, HookType.TOOL_AFTER):
        hooks.register(Trace(hook_id=stage.value, hook_type=stage, name='跟踪'))
    dispatcher = ToolDispatcher(registry, hooks)
    agent = AgentState(name='test', tool_ids=[record['tool_id']])
    session = SessionState(session_id="tool-session", workspace_id="test", workspace_path=str(tmp_path), agent_name="test", provider="test", model="test", status=SessionStatus.RUNNING, created_at=utc_now_iso(), updated_at=utc_now_iso())
    ref = SimpleNamespace(user_id=user, agent_id='test', session_id=session.session_id, run_id='run')
    runtime = RuntimeHandles(event_bus=EventBus(), run_ref=ref)
    workspace = SimpleNamespace(workspace_path=tmp_path, user_id=user, workspace_dir=tmp_path / 'data', shared_runtime_dir=tmp_path / 'data')
    settings = AppSettings()
    settings.human_in_the_loop.enabled = True
    pending = await dispatcher.execute_tool_calls(session, workspace, agent, [{'tool_name': tool.spec.name, 'arguments': {'value': 1}, 'tool_call_id': 'call'}], runtime, settings)
    assert pending.pending_approval and not session.metadata.get('trace')
    done = await dispatcher.resume_tool_batch(session, workspace, agent, pending.resume_batch, runtime, settings)
    assert done.tool_parts[0].state.output['output'] == {'value': 1}
    assert done.tool_parts[0].tool == tool.spec.name
    assert session.metadata['trace'] == ['tool.before', 'tool.after']
    pending = await dispatcher.execute_tool_calls(session, workspace, agent, [{'tool_name': tool.spec.name, 'arguments': {'value': 1}, 'tool_call_id': 'other'}], runtime, settings)
    store.set_state(user, record['tool_id'], record['revision'], disabled=True, admin=True)
    failed = await dispatcher.resume_tool_batch(session, workspace, agent, pending.resume_batch, runtime, settings)
    assert failed.tool_parts[0].state.output['status'] == 'error'
    await get_workspace_write_lease_manager(workspace.shared_runtime_dir).release(ref)


@pytest.mark.asyncio
async def test_cancel_reaps_child_process(tmp_path):
    import os
    user = str(uuid4())
    store = ToolStore(tmp_path / 'home')
    code = 'import os,time,subprocess,sys\nfrom pathlib import Path\ndef execute(arguments):\n    child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\n    Path("child-pid").write_text(str(child.pid))\n    time.sleep(30)\n    return {}\n'
    record = store.save(user, None, payload(code))
    tool = PythonCodeTool(store, user, record['tool_id'], record['version'])
    ctx = ToolExecutionContext(None, SimpleNamespace(workspace_path=tmp_path, user_id=user), SimpleNamespace(tool_ids=[record['tool_id']], readonly=False))
    task = asyncio.create_task(tool.execute({'value': 1}, ctx))
    try:
        async with asyncio.timeout(3):
            while not (tmp_path / 'child-pid').exists():
                await asyncio.sleep(0.01)
        pid = int((tmp_path / 'child-pid').read_text())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with asyncio.timeout(3):
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
