"""显式代码工具测试：有界容量、持久回执、重启不重放。"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from codepilot.session.agent_config import AgentConfigError
from codepilot.session.connections import ConnectionStore
from codepilot.session.working_directory import resolve_working_directory
from codepilot.tools.base import ToolExecutionContext
from codepilot.tools.code_store import ToolStore, digest, encoded
from codepilot.tools.python_code_tool import PythonCodeTool
from codepilot.tools.workspace_lease import get_workspace_write_lease_manager


class ToolTestPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(pattern=r'^[0-9a-f]{64}$')
    client_request_id: str = Field(min_length=1, max_length=100)
    arguments: dict = Field(default_factory=dict)
    working_directory: str | None = Field(default=None, max_length=4096)
    connection_id: str | None = Field(default=None, max_length=100)


class ToolTestService:
    def __init__(self, workspace, settings):
        self.workspace, self.settings = workspace, settings
        self.store = ToolStore(workspace.codepilot_home)
        self.tasks = {}
        self.lock = asyncio.Lock()

    def _read(self, user, identity):
        with self.store.db() as db:
            row = db.execute('SELECT record FROM tests WHERE owner=? AND id=?', (user, identity)).fetchone()
        if not row:
            raise AgentConfigError('测试不存在', status=404)
        return json.loads(row[0])

    def _write(self, user, record):
        with self.store.db() as db:
            db.execute('INSERT INTO tests VALUES (?,?,?) ON CONFLICT(owner,id) DO UPDATE SET record=excluded.record',
                       (record['test_id'], user, encoded(record).decode()))

    async def start(self, user, identity, payload):
        if len(encoded(payload.arguments)) > 65536:
            raise AgentConfigError('测试参数超过限制')
        test_id = digest(payload.client_request_id)
        fingerprint = digest([identity, payload.model_dump()])
        async with self.lock:
            try:
                saved = await asyncio.to_thread(self._read, user, test_id)
            except AgentConfigError as exc:
                if exc.status != 404:
                    raise
            else:
                if saved['fingerprint'] != fingerprint:
                    raise AgentConfigError('请求 ID 已用于其他测试', status=409)
                return await self.get(user, test_id)
            if len(self.tasks) >= 2 or any(key[0] == user for key in self.tasks):
                raise AgentConfigError('测试容量已满', status=409)
            # 先检查当前可用性，再固定已保存版本；测试不得借历史版本绕过撤回。
            await asyncio.to_thread(self.store.resolve, user, identity)
            bundle = await asyncio.to_thread(self.store.load, user, identity, payload.version)
            directory = resolve_working_directory(self.settings, self.workspace.workspace_path, payload.working_directory)
            record = {'test_id': test_id, 'tool_id': identity, 'version': bundle['version'], 'fingerprint': fingerprint,
                      'status': 'running', 'result': None}
            await asyncio.to_thread(self._write, user, record)
            self.tasks[(user, test_id)] = asyncio.create_task(self._execute(user, record, payload, directory))
            return record

    async def _execute(self, user, record, payload, directory):
        ref = SimpleNamespace(user_id=user, agent_id='tool-test', session_id=record['test_id'], run_id=str(uuid4()))
        workspace = replace(self.workspace.for_user(user), workspace_path=directory)
        lease = get_workspace_write_lease_manager(workspace.shared_runtime_dir)
        try:
            resolver = None
            if payload.connection_id:
                connections = ConnectionStore(self.store.home, self.settings)
                connection, _ = await asyncio.to_thread(connections.resolve, user, payload.connection_id)
                resolver = lambda: connections.resolve(user, payload.connection_id, connection['revision'])
            tool = PythonCodeTool(self.store, user, record['tool_id'], record['version'], resolver)
            agent = SimpleNamespace(tool_ids=[record['tool_id']], readonly=False)
            await lease.acquire(ref)
            result = await tool.execute(payload.arguments, ToolExecutionContext(None, workspace, agent, run_ref=ref))
            record.update(status='failed' if result['status'] == 'error' else 'completed', result=result)
        except asyncio.CancelledError:
            record.update(status='cancelled', result=None)
        except Exception:
            record.update(status='failed', result={'status': 'error', 'error_message': '工具测试失败'})
        finally:
            await lease.release(ref)
            await asyncio.to_thread(self._write, user, record)
            self.tasks.pop((user, record['test_id']), None)

    async def get(self, user, identity):
        record = await asyncio.to_thread(self._read, user, identity)
        if record['status'] == 'running' and (user, identity) not in self.tasks:
            record.update(status='interrupted', result=None)
            await asyncio.to_thread(self._write, user, record)
        return record

    async def cancel(self, user, identity):
        await self.get(user, identity)
        task = self.tasks.get((user, identity))
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # 首次调度前取消不会进入协程的 finally，需补齐回执并释放容量。
            if self.tasks.pop((user, identity), None) is not None:
                record = await asyncio.to_thread(self._read, user, identity)
                record.update(status='cancelled', result=None)
                await asyncio.to_thread(self._write, user, record)
        return await self.get(user, identity)

    async def shutdown(self):
        await asyncio.gather(*(self.cancel(user, identity) for user, identity in list(self.tasks)))
