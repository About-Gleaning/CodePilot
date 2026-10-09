"""统一发布的真实 HTTP、安全边界与中断重试回归。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from test_agent_config import _payload, _Tool
from test_publications import setup, publish, skill_payload
from test_python_tools import named_payload, publish as publish_tool
from codepilot.api.publication_routes import register_publication_routes
from codepilot.session.agent_config import AgentConfigError
from codepilot.tools.code_store import ToolStore
from codepilot.skills.store import SkillStore


@pytest.fixture
def api(setup):
    service, user, other = setup
    app = FastAPI()

    @app.middleware('http')
    async def principal(request, call_next):
        request.state.principal = SimpleNamespace(user_id=request.headers.get('test-user', user), username='测试用户', role='user')
        return await call_next(request)

    router = APIRouter(prefix='/api')
    register_publication_routes(router, SimpleNamespace(agent_config_service=service, settings=service.settings))
    app.include_router(router)
    with TestClient(app) as client:
        yield client, service, user, other, ToolStore(service.users_root.parent)


def agent(service, user, tools, **extra):
    return service.create(user, _payload(tool_ids=tools, skill_ids=[], hook_ids=[], delegate_agent_ids=[], **extra))


def preview(client, record, include=True):
    return client.post('/api/agent-publications/preview', json={'source_agent_id': record['agent_id'], 'include_dependencies': include})


def confirm(client, candidate, request='稳定请求'):
    return client.post('/api/agent-publications/confirm', json={k: candidate[k] for k in ('candidate_id', 'digest', 'expected_revision')} | {'client_request_id': request})


def test_http_aggregate_and_single_confirmation_more_than_five_tools(api):
    client, service, user, other, tools = api
    records = [tools.save(user, None, named_payload(f'tool_{i}')) for i in range(6)]
    root = agent(service, user, [r['tool_id'] for r in records])
    error = preview(client, root, False)
    assert error.status_code == 409
    issues = error.json()['detail']['issues']
    assert len(issues) == 6
    assert all(i['action'] == 'publish' and i['resource_name'] == '示例工具' and i['referenced_by'][0]['agent_id'] == root['agent_id'] for i in issues)
    candidate = preview(client, root).json()
    assert len(candidate['dependencies']) == 6
    assert tools.list(user, 'public')['total'] == 0
    assert client.post('/api/agent-publications/preview', json={'source_agent_id': root['agent_id'], 'include_dependencies': 'true'}).status_code == 422
    result = confirm(client, candidate)
    assert result.status_code == 200, result.text
    assert len(result.json()['progress']) == 7
    assert confirm(client, candidate).json() == result.json()
    profile = service.get_active_profile_snapshot(other, result.json()['id'])
    assert len(profile.tool_ids) == 6
    assert service.get(user, root['agent_id'])['tool_ids'] == [r['tool_id'] for r in records]


def test_shared_dependency_lists_all_referrers_and_publishes_once(api):
    client, service, user, other, tools = api
    service.tool_registry.register(_Tool('task'))
    tool = tools.save(user, None, named_payload())
    child = agent(service, user, [tool['tool_id']], name='child', launch_modes=['delegated'])
    root = service.create(user, _payload(tool_ids=[tool['tool_id']], tool_names=['task'], can_delegate=True,
        delegate_agent_ids=[child['agent_id']], skill_ids=[], hook_ids=[]))
    issues = preview(client, root, False).json()['detail']['issues']
    assert len(issues) == 1
    assert {r['agent_id'] for r in issues[0]['referenced_by']} == {root['agent_id'], child['agent_id']}
    candidate = preview(client, root).json()
    assert len(candidate['dependencies']) == 1
    assert confirm(client, candidate).status_code == 200
    assert tools.list(user, 'public')['total'] == 1


@pytest.mark.parametrize('direct_public', [False, True])
def test_restore_old_public_version_and_ignore_private_edits(api, direct_public):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload('old_tool'))
    public = publish_tool(tools, user, source)
    tools.set_state(user, public['tool_id'], public['revision'], withdrawn=True)
    changed = named_payload('new_tool')
    changed['expected_revision'] = source['revision']
    changed['files']['main.py'] = 'def execute(arguments):\n    return {"new": True}\n'
    tools.save(user, source['tool_id'], changed)
    root = agent(service, user, [public['tool_id'] if direct_public else source['tool_id']])
    candidate = preview(client, root).json()
    dependency = candidate['dependencies'][0]
    assert dependency['action'] == 'restore' and dependency['call_name'] == 'old_tool'
    assert dependency['files'] == tools.load(user, public['tool_id'])['files']
    result = confirm(client, candidate)
    assert result.status_code == 200, result.text
    restored = tools.resolve(other, public['tool_id'])
    assert restored['version'] == public['version'] and restored['call_name'] == 'old_tool'


@pytest.mark.parametrize('state', ['archived', 'disabled', 'other_withdrawn', 'corrupt'])
def test_blocked_states_do_not_mutate_any_dependency(api, state):
    client, service, user, other, tools = api
    first = tools.save(user, None, named_payload('good_tool'))
    source = tools.save(other if state == 'other_withdrawn' else user, None, named_payload('bad_tool'))
    identity = source['tool_id']
    if state == 'archived':
        tools.set_state(user, identity, source['revision'], archived=True)
    else:
        public = publish_tool(tools, source['owner_user_id'], source)
        identity = public['tool_id']
        if state == 'disabled':
            tools.set_state(user, identity, public['revision'], disabled=True, admin=True)
        elif state == 'other_withdrawn':
            tools.set_state(other, identity, public['revision'], withdrawn=True)
        else:
            tools._version_path(identity, public['version']).write_text('{}')
    root = agent(service, user, [first['tool_id'], identity])
    response = preview(client, root)
    assert response.status_code == 409
    assert response.json()['detail']['issues']
    assert tools.list(user, 'public')['total'] == (0 if state == 'archived' else 1)


def test_archived_source_with_usable_public_and_missing_credentials_can_publish(api):
    client, service, user, other, tools = api
    body = named_payload()
    body['definition']['credential_fields'] = ['token']
    source = tools.save(user, None, body)
    public = publish_tool(tools, user, source)
    tools.set_state(user, source['tool_id'], source['revision'], archived=True)
    root = agent(service, user, [source['tool_id']])
    candidate = preview(client, root).json()
    assert candidate['dependencies'] == []
    result = confirm(client, candidate)
    assert result.status_code == 200
    assert service.publications.profile(other, result.json()['id']).tool_ids == [public['tool_id']]


def test_public_version_collision_and_readonly_reported(api):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload('read_file'))
    publish_tool(tools, user, source)
    changed = named_payload('safe_name') | {'expected_revision': source['revision']}
    tools.save(user, source['tool_id'], changed)
    root = agent(service, user, [source['tool_id']])
    assert any(i['code'] == 'tool_name_conflict' for i in preview(client, root).json()['detail']['issues'])
    other_tool = tools.save(user, None, named_payload('other_tool'))
    readonly = agent(service, user, [other_tool['tool_id']], name='readonly', readonly=True)
    assert any(i['code'] == 'tool_readonly_forbidden' for i in preview(client, readonly).json()['detail']['issues'])


def test_sensitive_and_unknown_dependency_errors_do_not_leak(api):
    client, service, user, other, tools = api
    body = named_payload()
    secret = '/Users/private/customer-secret'
    body['files']['main.py'] = f'# {secret}\ndef execute(arguments):\n    return {{}}\n'
    source = tools.save(user, None, body)
    foreign = tools.save(other, None, named_payload('hidden_name'))
    root = agent(service, user, [source['tool_id'], foreign['tool_id']])
    response = preview(client, root)
    assert response.status_code == 409
    assert secret not in response.text and 'hidden_name' not in response.text
    hidden = next(i for i in response.json()['detail']['issues'] if i['resource_id'] == foreign['tool_id'])
    assert hidden['resource_name'] is None


def test_receipt_survives_failure_after_tool_commit_and_retry(api, monkeypatch):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    root = agent(service, user, [source['tool_id']])
    candidate = preview(client, root).json()
    original = ToolStore.apply_dependency

    def interrupted(self, *args):
        original(self, *args)
        raise OSError('模拟工具提交后进程中断')

    monkeypatch.setattr(ToolStore, 'apply_dependency', interrupted)
    failure = confirm(client, candidate)
    assert failure.status_code == 503
    assert failure.json()['detail']['progress'][0]['status'] == 'completed'
    assert failure.json()['detail']['retryable'] is True
    assert service.publications.list(other) == []
    assert confirm(client, candidate, '新请求').status_code == 409
    public = tools.get(user, candidate['dependencies'][0]['tool_id'])
    monkeypatch.setattr(ToolStore, 'apply_dependency', original)
    result = confirm(client, candidate)
    assert result.status_code == 200, result.text
    assert tools.get(user, public['tool_id'])['revision'] == public['revision']


@pytest.mark.parametrize('change', ['withdrawn', 'disabled', 'revision'])
def test_completed_step_later_changed_cannot_be_replayed(api, monkeypatch, change):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    root = agent(service, user, [source['tool_id']])
    candidate = preview(client, root).json()
    original = service.publications._publish_manifest
    monkeypatch.setattr(service.publications, '_publish_manifest', lambda *args: (_ for _ in ()).throw(OSError('模拟最终保存失败')))
    assert confirm(client, candidate).status_code == 503
    public = tools.get(user, candidate['dependencies'][0]['tool_id'])
    tools.set_state(user, public['tool_id'], public['revision'], admin=True, **({'withdrawn': True} if change == 'withdrawn' else {'disabled': True} if change == 'disabled' else {'withdrawn': False}))
    monkeypatch.setattr(service.publications, '_publish_manifest', original)
    result = confirm(client, candidate)
    assert result.status_code == 409
    assert result.json()['detail']['retryable'] is False
    assert service.publications.list(other) == []
    if change == 'withdrawn':
        assert tools.get(user, public['tool_id'])['withdrawn'] is True


def test_changed_source_and_expired_candidate_do_not_publish(api):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    root = agent(service, user, [source['tool_id']])
    candidate = preview(client, root).json()
    tools.save(user, source['tool_id'], named_payload('updated') | {'expected_revision': source['revision']})
    assert confirm(client, candidate).status_code == 409
    assert tools.list(user, 'public')['total'] == 0
    fresh = preview(client, root).json()
    with service.publications.db() as db:
        db.execute('UPDATE candidates SET expires=? WHERE id=?', (time.time() - 1, fresh['candidate_id']))
    assert confirm(client, fresh).json()['detail']['code'] == 'publication_expired'
    assert tools.list(user, 'public')['total'] == 0


def test_republish_failure_keeps_previous_agent_and_public_resource(api, monkeypatch):
    client, service, user, other, tools = api
    skills = SkillStore(service.users_root.parent)
    skill = skills.save(user, None, skill_payload())
    root = service.create(user, _payload(skill_ids=[skill['skill_id']], hook_ids=[], delegate_agent_ids=[]))
    first = publish(service, user, root)
    public_resource = service.publications.get(first['id'])['manifest']['resources'][0]
    public_skills = SkillStore(service.users_root.parent, publication_id=first['id'])
    current = public_skills.get(user, public_resource['id'])
    public_skills.save(user, public_resource['id'], skill_payload('公共编辑') | {'expected_revision': current['revision']})
    skills.archive(user, skill['skill_id'], skill['revision'])
    source = tools.save(user, None, named_payload())
    updated = service.update(user, root['agent_id'], _payload(skill_ids=[skill['skill_id']], hook_ids=[], tool_ids=[source['tool_id']], delegate_agent_ids=[], expected_revision_id=root['revision_id']))
    candidate = preview(client, updated).json()
    assert candidate['resources'][0]['payload'] is None
    original = service.publications._publish_manifest
    monkeypatch.setattr(service.publications, '_publish_manifest', lambda *args: (_ for _ in ()).throw(OSError('模拟保存失败')))
    assert confirm(client, candidate).status_code == 503
    assert service.publications.get(first['id'])['revision'] == first['revision']
    monkeypatch.setattr(service.publications, '_publish_manifest', original)
    assert confirm(client, candidate).status_code == 200
    assert '公共编辑' in public_skills.read(other, public_resource['id'], 'SKILL.md').decode()


def test_concurrent_confirm_is_idempotent(api):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    root = agent(service, user, [source['tool_id']])
    candidate = preview(client, root).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(client, candidate), range(2)))
    assert all(r.status_code == 200 for r in results)
    assert results[0].json() == results[1].json()
    assert tools.list(user, 'public')['total'] == 1


def test_aliases_of_same_public_tool_restore_once(api):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    public = publish_tool(tools, user, source)
    tools.set_state(user, public['tool_id'], public['revision'], withdrawn=True)
    root = agent(service, user, [source['tool_id'], public['tool_id']])
    response = preview(client, root)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert len(candidate['dependencies']) == 1
    assert list(candidate['profiles'].values())[0]['tool_ids'] == [public['tool_id']]
    result = confirm(client, candidate)
    assert result.status_code == 200, result.text
    assert len(result.json()['progress']) == 2


def test_restart_reuses_persistent_receipt_without_background_execution(api, monkeypatch):
    from codepilot.session.publications import PublicationStore
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    root = agent(service, user, [source['tool_id']])
    candidate = preview(client, root).json()
    original = ToolStore.apply_dependency

    def interrupted(self, *args):
        original(self, *args)
        raise OSError('模拟保存回执后中断')

    monkeypatch.setattr(ToolStore, 'apply_dependency', interrupted)
    assert confirm(client, candidate).status_code == 503
    public = tools.get(user, candidate['dependencies'][0]['tool_id'])
    monkeypatch.setattr(ToolStore, 'apply_dependency', original)
    service.publications = PublicationStore(service.users_root.parent)
    assert service.publications.list(other) == []
    assert tools.get(user, public['tool_id'])['revision'] == public['revision']
    assert confirm(client, candidate).status_code == 200
    assert tools.get(user, public['tool_id'])['revision'] == public['revision']


def test_failure_after_agent_index_write_rolls_back_agent_only(api, monkeypatch):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    root = agent(service, user, [source['tool_id']])
    candidate = preview(client, root).json()
    original = service.publications._publish_manifest

    def fail_after_index(*args):
        original(*args)
        raise OSError('模拟写入索引后提交前失败')

    monkeypatch.setattr(service.publications, '_publish_manifest', fail_after_index)
    result = confirm(client, candidate)
    assert result.status_code == 503
    assert service.publications.list(other) == []
    assert tools.list(user, 'public')['total'] == 1
    monkeypatch.setattr(service.publications, '_publish_manifest', original)
    assert confirm(client, candidate).status_code == 200


def test_delegation_members_changed_requires_new_preview(api):
    client, service, user, other, tools = api
    service.tool_registry.register(_Tool('task'))
    source = tools.save(user, None, named_payload())
    root = service.create(user, _payload(tool_names=['task'], can_delegate=True, delegate_agent_ids=None,
        tool_ids=[source['tool_id']], hook_ids=[], skill_ids=[]))
    candidate = preview(client, root).json()
    agent(service, user, [], name='new-child', launch_modes=['delegated'])
    assert confirm(client, candidate).status_code == 409
    assert tools.list(user, 'public')['total'] == 0


def test_tool_action_count_and_combined_size_limits(api):
    client, service, user, other, tools = api
    service.tool_registry.register(_Tool('task'))
    identities = [tools.save(user, None, named_payload(f'tool_{i}'))['tool_id'] for i in range(101)]
    child = agent(service, user, identities[100:], name='child', launch_modes=['delegated'])
    root = service.create(user, _payload(tool_names=['task'], can_delegate=True, delegate_agent_ids=[child['agent_id']],
        tool_ids=identities[:100], hook_ids=[], skill_ids=[]))
    response = preview(client, root)
    assert response.status_code == 409 and '资源数量' in response.text
    large = named_payload('large')
    large['files'] = {'main.py': 'def execute(arguments):\n    return {}\n', **{f'part_{i}.py': '#' + 'x' * 900_000 for i in range(4)}}
    big_tools = [tools.save(user, None, large | {'definition': large['definition'] | {'call_name': f'large_{i}'}})['tool_id'] for i in range(10)]
    big_root = agent(service, user, big_tools, name='large-root')
    response = preview(client, big_root)
    assert response.status_code == 409 and '32 MiB' in response.text
    assert tools.list(user, 'public')['total'] == 0


def test_withdraw_is_serialized_after_final_check_until_agent_commit(api, monkeypatch):
    from threading import Event
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    public = publish_tool(tools, user, source)
    root = agent(service, user, [source['tool_id']])
    candidate = preview(client, root).json()
    entered, start_withdraw = Event(), Event()
    original = service.publications._publish_manifest

    def observe_lock(*args):
        entered.set()
        assert start_withdraw.wait(5)
        return original(*args)

    def withdraw():
        assert entered.wait(5)
        start_withdraw.set()
        return tools.set_state(user, public['tool_id'], public['revision'], withdrawn=True)

    monkeypatch.setattr(service.publications, '_publish_manifest', observe_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(withdraw)
        result = confirm(client, candidate)
        withdrawn = update.result(timeout=5)
    assert result.status_code == 200, result.text
    assert withdrawn['withdrawn']
    with pytest.raises(AgentConfigError):
        service.get_active_profile_snapshot(other, result.json()['id'])


def test_failed_attempt_request_id_cannot_start_another_candidate(api, monkeypatch):
    client, service, user, other, tools = api
    source = tools.save(user, None, named_payload())
    root = agent(service, user, [source['tool_id']])
    one = preview(client, root).json()
    two = preview(client, root).json()
    original = ToolStore.apply_dependency
    monkeypatch.setattr(ToolStore, 'apply_dependency', lambda *args: (_ for _ in ()).throw(OSError('模拟保存失败')))
    assert confirm(client, one).status_code == 503
    monkeypatch.setattr(ToolStore, 'apply_dependency', original)
    assert confirm(client, two).status_code == 409
    assert tools.list(user, 'public')['total'] == 0
    assert confirm(client, one).status_code == 200


def test_corrupt_live_skill_is_reported_before_any_tool_publish(api):
    client, service, user, other, tools = api
    skills = SkillStore(service.users_root.parent)
    skill = skills.save(user, None, skill_payload())
    source = tools.save(user, None, named_payload())
    root = service.create(user, _payload(tool_ids=[source['tool_id']], skill_ids=[skill['skill_id']], hook_ids=[], delegate_agent_ids=[]))
    record = skills._current(user, skill['skill_id'])
    (skills._directory(user, skill['skill_id']) / record['content_dir'] / 'SKILL.md').write_text('损坏正文')
    result = preview(client, root)
    assert result.status_code == 409
    assert any(i['resource_kind'] == 'skill' for i in result.json()['detail']['issues'])
    assert tools.list(user, 'public')['total'] == 0
