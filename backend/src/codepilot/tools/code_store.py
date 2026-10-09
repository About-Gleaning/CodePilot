"""代码工具的独立作用域、不可变文件版本与分页元数据索引。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from codepilot.session.agent_config import AgentConfigError
from codepilot.skills.store import SkillStore
from codepilot.utils import utc_now_iso


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def call_name(identity: str) -> str:
    scope, raw = identity.split(':', 1)
    if scope not in {'personal', 'public'}:
        raise AgentConfigError('工具身份无效')
    return 'code_tool_' + ('p_' if scope == 'personal' else 'u_') + UUID(raw).hex


def resolved_call_name(identity: str, definition: dict) -> str:
    # 旧版本只在读取时回退；不重写版本正文或为新名称注册历史执行别名。
    legacy = call_name(identity)
    return definition.get('call_name') or legacy


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=100)
    call_name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    description: str = Field(min_length=1, max_length=2000)
    input_schema: dict = Field(default_factory=lambda: {'type': 'object', 'properties': {}, 'additionalProperties': False})
    entrypoint: str = Field(default='main.py', max_length=500)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    credential_fields: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def validate_definition(self):
        if 'call_name' in self.model_fields_set and (self.call_name is None or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', self.call_name) or self.call_name.startswith(('mcp__', 'code_tool_'))):
            raise ValueError('调用名称无效或使用了保留前缀')
        if len(encoded(self.input_schema)) > 32768 or self.input_schema.get('type') != 'object':
            raise ValueError('参数 Schema 必须为不超过 32 KiB 的对象')
        # 不允许外部引用，避免校验参数时隐式访问网络或文件。
        def check(value, depth=0):
            if depth > 30:
                raise ValueError('参数 Schema 嵌套过深')
            if isinstance(value, dict):
                if '$ref' in value or '$dynamicRef' in value:
                    raise ValueError('首版不支持 Schema 引用')
                for child in value.values():
                    check(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    check(child, depth + 1)
        check(self.input_schema)
        Draft202012Validator.check_schema(self.input_schema)
        if not self.entrypoint.endswith('.py') or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', name) for name in self.credential_fields):
            raise ValueError('入口或凭证字段无效')
        if len(set(self.credential_fields)) != len(self.credential_fields):
            raise ValueError('凭证字段重复')
        return self


class ToolPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    definition: ToolDefinition
    files: dict[str, str] = Field(max_length=100)
    expected_revision: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class ToolStore:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.root = SkillStore.resource_path(self.home, 'tool-resources')
        self.files = SkillStore(home)

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = SkillStore.resource_path(self.root, 'catalog.sqlite3')
        # 首次创建也使用私有权限，避免创建到 chmod 之间的可读窗口。
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript('''CREATE TABLE IF NOT EXISTS tools (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, scope TEXT NOT NULL,
                source TEXT, record TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS tool_owner_scope ON tools(owner, scope);
                CREATE TABLE IF NOT EXISTS previews (token TEXT PRIMARY KEY, owner TEXT NOT NULL, source TEXT NOT NULL,
                version TEXT NOT NULL, revision TEXT NOT NULL, public_revision TEXT, expires REAL NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS tests (id TEXT NOT NULL, owner TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(owner, id));
                CREATE TABLE IF NOT EXISTS publication_steps (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL);''')
            yield db
            db.commit()
        finally:
            db.close()

    def _record(self, db, user, identity, *, admin=False):
        try:
            call_name(identity)
            UUID(user)
        except (ValueError, AttributeError) as exc:
            raise AgentConfigError('工具不存在', code='tool_not_found', status=404) from exc
        row = db.execute('SELECT * FROM tools WHERE id=?', (identity,)).fetchone()
        if row is None or row['scope'] == 'private' and row['owner'] != user and not admin:
            raise AgentConfigError('工具不存在', code='tool_not_found', status=404)
        record = json.loads(row['record'])
        return {**record, 'call_name': record.get('call_name') or call_name(identity)}

    def get(self, user, identity):
        with self.db() as db:
            return self._record(db, user, identity)

    def _put(self, db, record):
        db.execute('INSERT INTO tools VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
                   (record['tool_id'], record['owner_user_id'], record['visibility'], record.get('source_tool_id'), encoded(record).decode()))

    def _version_path(self, identity, version):
        if not re.fullmatch(r'[0-9a-f]{64}', version):
            raise AgentConfigError('工具版本无效', code='tool_version_invalid')
        call_name(identity)
        return SkillStore.resource_path(self.root, f"versions/{identity.replace(':', '-')}/{version}.json")

    def _write_version(self, identity, bundle):
        version = digest(bundle)
        path = self._version_path(identity, version)
        if not path.exists():
            temporary = path.parent / ('.tmp-' + uuid4().hex)
            try:
                self.files._write(temporary, encoded(bundle))
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return version

    def load(self, user, identity, version=None, *, record=None):
        record = record if record is not None else self.get(user, identity)
        version = version or record['version']
        path = self._version_path(identity, version)
        try:
            if path.stat().st_size > 26 * 1024 * 1024:
                raise ValueError
            with path.open('rb') as stream:
                bundle = json.loads(stream.read(26 * 1024 * 1024 + 1))
            if digest(bundle) != version:
                raise ValueError
            ToolDefinition.model_validate(bundle['definition'])
        except (OSError, ValueError, KeyError) as exc:
            raise AgentConfigError('工具版本损坏或不存在', code='tool_version_invalid', status=409) from exc
        return {**record, **bundle, 'version': version, 'call_name': resolved_call_name(identity, bundle['definition'])}

    def resolve(self, user, identity, version=None):
        record = self.get(user, identity)
        if record['disabled'] or version is None and (record['withdrawn'] or record['archived']):
            raise AgentConfigError('工具已禁用、撤回或归档', code='tool_unavailable', status=409)
        return self.load(user, identity, version)

    def save(self, user, identity, payload):
        try:
            payload = ToolPayload.model_validate(payload)
            UUID(user)
        except Exception as exc:
            raise AgentConfigError('工具配置无效', code='tool_config_invalid') from exc
        creating = identity is None
        identity = identity or 'personal:' + str(uuid4())
        if not identity.startswith('personal:'):
            raise AgentConfigError('公共工具只能通过重新发布更新', status=403)
        data = payload.model_dump()
        # 兼容旧定义，不能把模型默认 None 写入不可变版本。
        if 'call_name' not in payload.definition.model_fields_set:
            data['definition'].pop('call_name')
        if not data['files'] or data['definition']['entrypoint'] not in data['files']:
            raise AgentConfigError('必须提供 Python 入口文件')
        total = 0
        for name, content in data['files'].items():
            SkillStore.resource_path(self.root, name)
            if not name.endswith('.py') or len(name) > 500:
                raise AgentConfigError('首版只支持 Python 文件')
            size = len(content.encode())
            total += size
            if size > 1024 * 1024 or total > 4 * 1024 * 1024:
                raise AgentConfigError('文件超过单个 1 MiB 或总量 4 MiB 限制')
        bundle = {'definition': data['definition'], 'files': data['files']}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            exists = db.execute('SELECT 1 FROM tools WHERE id=?', (identity,)).fetchone()
            current = self._record(db, user, identity) if exists else None
            if current and (current['archived'] or current['disabled']):
                raise AgentConfigError('工具已归档或禁用', status=409)
            if current and current['revision'] != payload.expected_revision:
                raise AgentConfigError('工具已更新，请重新加载', code='revision_conflict', status=409)
            if current is None and (not creating or payload.expected_revision):
                raise AgentConfigError('工具不存在', status=404)
            if current and 'call_name' not in data['definition']:
                # 旧客户端省略新字段时从当前固定版本保留，不能回退到 ID 调用名。
                previous = self.load(user, identity, current['version'])['definition']
                if 'call_name' in previous:
                    data['definition']['call_name'] = previous['call_name']
            version = self._write_version(identity, bundle)
            record = {**(current or {}), 'tool_id': identity, 'owner_user_id': user, 'visibility': 'private',
                      'name': payload.definition.name, 'description': payload.definition.description, 'version': version,
                      'call_name': resolved_call_name(identity, data['definition']),
                      'revision': uuid4().hex + uuid4().hex, 'archived': False, 'withdrawn': False, 'disabled': False,
                      'updated_at': utc_now_iso()}
            self._put(db, record)
            return record

    def list(self, user, scope='private', offset=0, limit=50, q=''):
        with self.db() as db:
            condition = "scope=? AND (scope='public' OR owner=?) AND (json_extract(record,'$.name') LIKE ? OR json_extract(record,'$.description') LIKE ?)"
            args = (scope, user, '%' + q + '%', '%' + q + '%')
            total = db.execute('SELECT COUNT(*) FROM tools WHERE ' + condition, args).fetchone()[0]
            rows = db.execute('SELECT record FROM tools WHERE ' + condition + ' ORDER BY id LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
            records = [json.loads(row[0]) for row in rows]
            return {'tools': [{**record, 'call_name': record.get('call_name') or call_name(record['tool_id'])} for record in records], 'total': total}

    def preview(self, user, identity, revision):
        record = self.get(user, identity)
        if record['owner_user_id'] != user or record['visibility'] != 'private' or record['revision'] != revision:
            raise AgentConfigError('源工具已变化或无权发布', status=409)
        bundle = self.resolve(user, identity)
        from codepilot.session.publications import check_public_content
        check_public_content(bundle['definition'], record['name'])
        for name, content in bundle['files'].items():
            check_public_content(content, name)
        public_id = 'public:' + str(uuid5(NAMESPACE_URL, user + ':' + identity))
        with self.db() as db:
            db.execute('DELETE FROM previews WHERE expires<?', (time.time(),))
            if db.execute('SELECT COUNT(*) FROM previews WHERE owner=?', (user,)).fetchone()[0] >= 5:
                raise AgentConfigError('发布预览过多，请稍后重试', status=409)
            row = db.execute('SELECT record FROM tools WHERE id=?', (public_id,)).fetchone()
            public_revision = json.loads(row[0])['revision'] if row else None
            token = uuid4().hex
            db.execute('INSERT INTO previews VALUES (?,?,?,?,?,?,?,NULL)',
                       (token, user, identity, record['version'], revision, public_revision, time.time() + 900))
        return {'token': token, 'tool_id': public_id, 'call_name': resolved_call_name(public_id, bundle['definition']), 'definition': bundle['definition'], 'files': bundle['files']}

    def publish(self, user, identity, token):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            candidate = db.execute('SELECT * FROM previews WHERE token=? AND owner=? AND source=?', (token, user, identity)).fetchone()
            if candidate is None or candidate['expires'] < time.time():
                raise AgentConfigError('发布预览已失效', status=409)
            if candidate['result']:
                return json.loads(candidate['result'])
            source = self._record(db, user, identity)
            if source['revision'] != candidate['revision'] or source['archived'] or source['disabled']:
                raise AgentConfigError('源工具已变化，请重新预览', status=409)
            public_id = 'public:' + str(uuid5(NAMESPACE_URL, user + ':' + identity))
            old_row = db.execute('SELECT record FROM tools WHERE id=?', (public_id,)).fetchone()
            old = json.loads(old_row[0]) if old_row else None
            if (old['revision'] if old else None) != candidate['public_revision'] or old and old['disabled']:
                raise AgentConfigError('公共工具状态已变化', status=409)
            bundle = self.load(user, identity, candidate['version'])
            version = self._write_version(public_id, {key: bundle[key] for key in ('definition', 'files')})
            record = {**source, 'tool_id': public_id, 'visibility': 'public', 'source_tool_id': identity,
                      'call_name': resolved_call_name(public_id, bundle['definition']),
                      'version': version, 'revision': uuid4().hex + uuid4().hex, 'withdrawn': False,
                      'updated_at': utc_now_iso()}
            self._put(db, record)
            # 保留候选回执；同一 token 重试只返回本次已发布身份，不再执行发布。
            db.execute('UPDATE previews SET result=? WHERE token=?', (encoded(record).decode(), token))
            return record

    def public_dependency(self, user, identity):
        if identity.startswith('public:'):
            return self.resolve(user, identity)['tool_id']
        source = self.get(user, identity)
        public_id = 'public:' + str(uuid5(NAMESPACE_URL, source['owner_user_id'] + ':' + identity))
        try:
            self.resolve(user, public_id)
        except AgentConfigError as exc:
            raise AgentConfigError("请先发布或恢复代码工具，再发布 Agent", code="tool_publication_required", status=409) from exc
        return public_id

    def prepare_dependency(self, user, identity):
        """只读取内容；批量候选不占用独立工具的预览名额。"""
        source = self.get(user, identity)
        public_id = identity if identity.startswith('public:') else 'public:' + str(uuid5(NAMESPACE_URL, source['owner_user_id'] + ':' + identity))
        try:
            public = self.get(user, public_id)
        except AgentConfigError as exc:
            if exc.code != 'tool_not_found':
                raise
            public = None
        if public and public['disabled']:
            raise AgentConfigError('公共工具已被管理员禁用，请联系管理员', code='tool_disabled', status=409)
        action = 'use'
        if public is None:
            if source['visibility'] != 'private' or source['owner_user_id'] != user:
                raise AgentConfigError('工具不存在或无权发布', code='tool_not_found', status=404)
            bundle = self.resolve(user, identity)
            action = 'publish'
        else:
            bundle = self.load(user, public_id)
            if public['withdrawn']:
                if public['owner_user_id'] != user:
                    raise AgentConfigError('公共工具已撤回，只有作者可以恢复', code='tool_withdrawn', status=409)
                action = 'restore'
        from codepilot.session.publications import check_public_content
        check_public_content(bundle['definition'], '工具定义')
        for content in bundle['files'].values():
            check_public_content(content, '工具文件')
        return {'source_id': identity if action == 'publish' else public_id, 'tool_id': public_id, 'name': bundle['definition']['name'],
                'action': action, 'call_name': resolved_call_name(public_id, bundle['definition']),
                'source_revision': source['revision'] if action == 'publish' else None,
                'public_revision': public['revision'] if public else None,
                'version': bundle['version'], 'definition': bundle['definition'],
                'files': bundle['files'] if action != 'use' else None}

    def dependency_receipt(self, db, user, step_id, plan):
        row = db.execute('SELECT * FROM publication_steps WHERE id=? AND owner=?', (step_id, user)).fetchone()
        if row and row['fingerprint'] != digest(plan):
            raise AgentConfigError('依赖发布候选不匹配，请重新预览', code='publication_changed', status=409)
        return json.loads(row['result']) if row else None

    def check_dependency(self, db, user, plan, receipt=None):
        """使用调用方事务读取，避免最终提交期间再次打开工具数据库。"""
        if plan['source_id'].startswith('personal:'):
            source = self._record(db, user, plan['source_id'])
            if plan['action'] == 'publish' and (source['revision'] != plan['source_revision'] or source['archived'] or source['disabled']):
                raise AgentConfigError('源工具已变化或不可用，请重新预览', code='publication_changed', status=409)
        try:
            public = self._record(db, user, plan['tool_id'])
        except AgentConfigError as exc:
            if exc.code != 'tool_not_found':
                raise
            public = None
        expected = receipt['revision'] if receipt else plan['public_revision']
        if (public['revision'] if public else None) != expected:
            raise AgentConfigError('公共工具已变化，请重新预览', code='publication_changed', status=409)
        if public and (public['disabled'] or public['archived'] or public['withdrawn'] != (plan['action'] == 'restore' and receipt is None)):
            raise AgentConfigError('公共工具已撤回、禁用或状态变化，请重新预览', code='publication_changed', status=409)
        if plan['action'] != 'use' and public and public['owner_user_id'] != user:
            raise AgentConfigError('无权处理公共工具', code='forbidden', status=403)
        # 不可变版本在确认时再校验完整性，不能依靠过往预览掩盖存储损坏。
        identity = plan['source_id'] if plan['action'] == 'publish' and receipt is None else plan['tool_id']
        bundle = self.load(user, identity, plan['version'], record=source if identity == plan['source_id'] and plan['action'] == 'publish' and receipt is None else public)
        if bundle['definition'] != plan['definition'] or plan['files'] is not None and bundle['files'] != plan['files']:
            raise AgentConfigError('工具内容已变化，请重新预览', code='publication_changed', status=409)
        return public

    def apply_dependency(self, user, plan, step_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            receipt = self.dependency_receipt(db, user, step_id, plan)
            public = self.check_dependency(db, user, plan, receipt)
            if receipt:
                return receipt
            if plan['action'] == 'publish':
                source = self._record(db, user, plan['source_id'])
                version = self._write_version(plan['tool_id'], {'definition': plan['definition'], 'files': plan['files']})
                public = {**source, 'tool_id': plan['tool_id'], 'visibility': 'public', 'source_tool_id': plan['source_id'],
                          'call_name': plan['call_name'], 'version': version, 'withdrawn': False,
                          'revision': uuid4().hex + uuid4().hex, 'updated_at': utc_now_iso()}
            elif plan['action'] == 'restore':
                public = {**public, 'withdrawn': False, 'revision': uuid4().hex + uuid4().hex, 'updated_at': utc_now_iso()}
            else:
                return public
            self._put(db, public)
            # 状态与回执一起提交；进程中断后不会重复公开已成功的步骤。
            db.execute('INSERT INTO publication_steps VALUES (?,?,?,?)', (step_id, user, digest(plan), encoded(public).decode()))
            return public

    def set_state(self, user, identity, revision, *, archived=None, withdrawn=None, disabled=None, admin=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            record = self._record(db, user, identity, admin=admin)
            if record['revision'] != revision:
                raise AgentConfigError('工具已变化', code='revision_conflict', status=409)
            if disabled is not None and not admin or (archived is not None or withdrawn is not None) and record['owner_user_id'] != user:
                raise AgentConfigError('无权修改工具状态', status=403)
            for key, value in (('archived', archived), ('withdrawn', withdrawn), ('disabled', disabled)):
                if value is not None:
                    record[key] = value
            record['revision'] = uuid4().hex + uuid4().hex
            self._put(db, record)
            return record

    def connection_target(self, user, identity):
        bundle = self.resolve(user, identity)
        fields = bundle['definition']['credential_fields']
        return {'target': 'tool:' + identity, 'kind': 'tool', 'fields': fields, 'personal': True,
                'signature': digest([identity, fields])}
