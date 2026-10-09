"""有容量限制的显式 Hook 测试；持久回执防止断线重试重放副作用。"""

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from codepilot.events import EventBus
from codepilot.hooks import HookContext, HookManager, HookType, RuntimeHandles
from codepilot.hooks.manager import validate_parameters
from codepilot.hooks.store import HookStore
from codepilot.session import AgentState, SessionState, SessionStatus
from codepilot.session.agent_config import AgentConfigError
from codepilot.session.connections import ConnectionStore
from codepilot.session.working_directory import resolve_working_directory
from codepilot.tools.workspace_lease import get_workspace_write_lease_manager
from codepilot.utils import utc_now_iso


class TestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_request_id: str = Field(min_length=1, max_length=100)
    parameters: dict[str, str | int | bool] = Field(default_factory=dict, max_length=50)
    working_directory: str | None = Field(default=None, max_length=4096)
    connection_id: str | None = Field(default=None, max_length=100)
    tool_name: str = Field(default="test_tool", max_length=100)


class HookTestService:
    def __init__(self, workspace, settings):
        self.workspace, self.settings = workspace, settings
        self.store = HookStore(workspace.codepilot_home)
        self.tasks = {}
        self.results = OrderedDict()
        self.lock = asyncio.Lock()

    def _root(self, user):
        return self.store.files.resource_path(self.store.root(user), ".tests")

    def _read(self, user, identity):
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise AgentConfigError("测试不存在", status=404)
        try:
            return json.loads(self.store.files.resource_path(self._root(user), identity + ".json").read_text())
        except OSError as exc:
            raise AgentConfigError("测试不存在", status=404) from exc

    def _write(self, user, record):
        import os
        root = self._root(user)
        temporary = root / f".tmp-{uuid4().hex}"
        try:
            self.store.files._write(temporary, json.dumps(record).encode())
            os.replace(temporary, root / (record["test_id"] + ".json"))
        finally:
            temporary.unlink(missing_ok=True)

    async def start(self, user, hook_id, payload):
        identity = hashlib.sha256(payload.client_request_id.encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([hook_id, payload.model_dump()], sort_keys=True).encode()).hexdigest()
        async with self.lock:
            path = self.store.files.resource_path(self._root(user), identity + ".json")
            if path.exists():
                record = self._read(user, identity)
                if record["fingerprint"] != fingerprint:
                    raise AgentConfigError("请求 ID 已用于其他测试", code="request_conflict", status=409)
                return await self.get(user, identity)
            active = [key for key, task in self.tasks.items() if not task.done()]
            if len(active) >= 2 or any(key[0] == user for key in active):
                raise AgentConfigError("Hook 测试容量已满", code="test_capacity", status=409)
            bundle = await asyncio.to_thread(self.store.load, user, hook_id, payload.version)
            if bundle["archived"]:
                raise AgentConfigError("Hook 已归档", status=409)
            validate_parameters(payload.parameters, bundle["definition"]["parameters"])
            directory = resolve_working_directory(self.settings, self.workspace.workspace_path, payload.working_directory)
            await asyncio.to_thread(self.store.check_command, bundle["definition"], directory)
            record = {"test_id": identity, "hook_id": hook_id, "version": payload.version, "fingerprint": fingerprint,
                      "status": "running", "created_at": utc_now_iso(), "error_code": None}
            await asyncio.to_thread(self._write, user, record)
            self.tasks[(user, identity)] = asyncio.create_task(self._execute(user, record, payload, directory))
            return record

    async def _execute(self, user, record, payload, directory):
        identity = record["test_id"]
        ref = SimpleNamespace(agent_id="hook-test", session_id=identity, run_id=identity)
        workspace = replace(self.workspace.for_user(user), workspace_path=directory)
        runtime = RuntimeHandles(event_bus=EventBus(), run_ref=ref, stop_event=asyncio.Event(), explicit_connections=True)
        try:
            hook = await asyncio.to_thread(self.store.instantiate, user, record["hook_id"], payload.version)
            if payload.connection_id:
                store = ConnectionStore(self.workspace.codepilot_home, self.settings)
                connection, _ = await asyncio.to_thread(store.resolve, user, payload.connection_id)
                if connection["target"] != f"hook:{record['hook_id']}":
                    raise AgentConfigError("连接不属于当前 Hook")
                runtime.hook_connections[record["hook_id"]] = lambda: store.resolve(user, payload.connection_id, connection["revision"])
            session = SessionState(session_id=identity, user_id=user, workspace_id=self.workspace.workspace_id,
                                   workspace_path=str(directory), agent_name="hook-test", provider="test", model="test",
                                   status=SessionStatus.RUNNING, created_at=utc_now_iso(), updated_at=utc_now_iso())
            agent = AgentState(name="hook-test", hook_ids=[hook.hook_id], hook_parameters={hook.hook_id: payload.parameters})
            ctx = HookContext(hook_type=hook.hook_type.value, session=session, workspace=workspace, agent=agent,
                              messages=[], runtime=runtime, tool_call={"tool_name": payload.tool_name, "arguments": {}},
                              tool_result={"status": "ok", "output": "测试结果"})
            manager = HookManager()
            manager.register(hook)
            result = await manager.run(HookType(hook.hook_type), ctx)
            record["status"] = "failed" if result.fail_session or (result.error and result.error.code != "hook_stopped") else "completed"
            record["error_code"] = result.error.code if result.error else None
            # 测试只展示人工请求，不创建真实会话或等待审批。
            self.results[(user, identity)] = {"stop": result.stop_loop, "fail": result.fail_session,
                                               "human": result.requires_human_input,
                                               "context": [part.text for message in result.messages_to_append for part in message.parts if hasattr(part, "text")]}
            while len(self.results) > 20:
                self.results.popitem(last=False)
            asyncio.get_running_loop().call_later(300, self.results.pop, (user, identity), None)
        except asyncio.CancelledError:
            record["status"] = "cancelled"
        except Exception:
            record.update(status="failed", error_code="test_failed")
        finally:
            await get_workspace_write_lease_manager(workspace.shared_runtime_dir).release(ref)
            record["finished_at"] = utc_now_iso()
            await asyncio.to_thread(self._write, user, record)
            self.tasks.pop((user, identity), None)

    async def get(self, user, identity):
        record = await asyncio.to_thread(self._read, user, identity)
        if record["status"] == "running" and (user, identity) not in self.tasks:
            record.update(status="interrupted", error_code="service_restarted")
            await asyncio.to_thread(self._write, user, record)
        return {**record, "result": self.results.get((user, identity))}

    async def cancel(self, user, identity):
        await self.get(user, identity)
        task = self.tasks.get((user, identity))
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return await self.get(user, identity)

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.results.clear()
