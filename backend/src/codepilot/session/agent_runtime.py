from __future__ import annotations

"""多 Agent 控制面与同进程执行后端。

Manager 只保存资源索引与持久化状态；SessionRunner、Task 和等待对象均封装在
Backend 句柄内。这样提升并发容量或替换为 worker 时不需要改动 HTTP 契约。
"""

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from codepilot.config.settings import resolve_llm_selection, resolve_thinking_value
from codepilot.events import DomainEvent, EventBus, HumanInteractionEvent, RunEventScope, StreamEvent
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.memory import UserPartitionedSessionMemory
from codepilot.session.agents import AgentProfile
from codepilot.session.attachments import decode_image_attachment, sanitize_attachment_filename
from codepilot.session.inbox import public_submission
from codepilot.session.runtime_store import (
    RuntimeStateStore,
    RuntimeStoreCorrupt,
    UserRuntimeStoreRegistry,
    encode_runtime_cursor,
)
from codepilot.session.session_runner import SessionRunner
from codepilot.session.state import (
    AgentLifecycleState,
    AgentRuntimeState,
    HumanInteractionRef,
    HumanInteractionState,
    InteractionStatus,
    RunRef,
    RunState,
    RunStatus,
    SessionStatus,
)
from codepilot.utils import utc_now_iso


class RuntimeConflict(ValueError):
    """资源归属、幂等、恢复或容量冲突。"""

    def __init__(self, code: str, message: str, *, status: int = 409, retry_after: int | None = None, issues: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.issues = issues or []


UserAgentKey = tuple[str, str]
SessionKey = tuple[str, str, str]
RunKey = tuple[str, str, str, str]


class AgentProfileProvider(Protocol):
    def get_active_profile_snapshot(self, user_id: str, agent_id: str) -> AgentProfile: ...

    def get_record_snapshot(self, user_id: str, agent_id: str) -> dict[str, Any]: ...

    def list_active_profile_snapshots(self, user_id: str) -> list[AgentProfile]: ...


class SessionRunnerFactory:
    """为每个 Session 创建不共享可变状态的 Runner。"""

    def __init__(self, create: Callable[..., SessionRunner]) -> None:
        self._create = create

    def create_runner(self, user_id: str) -> SessionRunner:
        try:
            return self._create(user_id)
        except TypeError:
            # 兼容只接收零参数的测试 Factory；正式运行时始终传入用户视图。
            return self._create()


@dataclass(slots=True)
class SessionExecutionHandle:
    agent_id: str
    session_id: str
    runner: SessionRunner
    user_id: str = ""


@dataclass(slots=True)
class RunExecutionHandle:
    run_ref: RunRef
    runner: SessionRunner
    event_scope: RunEventScope



@dataclass(frozen=True, slots=True)
class RunExecutionResult:
    status: RunStatus
    error_code: str | None = None
    error_summary: str | None = None
    external_effect_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class CancellationResult:
    confirmed: bool
    external_effect_uncertain: bool = False
    error_code: str | None = None


class AgentRuntimeBackend(Protocol):
    async def start_agent(self, user_id: str, agent_id: str) -> None: ...

    async def stop_agent(self, user_id: str, agent_id: str) -> None: ...

    async def load_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        replay: dict[str, Any] | None,
        profile_snapshot: AgentProfile,
    ) -> SessionExecutionHandle: ...

    async def start_run(
        self,
        session_handle: SessionExecutionHandle,
        run_ref: RunRef,
        request: GatewayInput,
        profile_snapshot: AgentProfile,
        event_scope: RunEventScope,
    ) -> RunExecutionHandle: ...

    def get_session_snapshot(self, session_handle: SessionExecutionHandle) -> dict[str, Any]: ...

    async def wait_run(self, run_handle: RunExecutionHandle) -> RunExecutionResult: ...

    async def cancel_run(
        self, session_handle: SessionExecutionHandle, run_ref: RunRef
    ) -> CancellationResult: ...

    async def reply_interaction(
        self,
        session_handle: SessionExecutionHandle,
        ref: HumanInteractionRef,
        payload: GatewayInput,
    ) -> None: ...

    async def append_message(self, handle: SessionExecutionHandle, request: GatewayInput, receipt: dict[str, Any]) -> dict[str, Any]: ...

    async def close_session(self, session_handle: SessionExecutionHandle) -> None: ...

    async def shutdown(self) -> None: ...


class InProcessAgentRuntimeBackend:
    """同进程执行后端；不维护全局 Agent、Run 或幂等索引。"""

    def __init__(self, runner_factory: SessionRunnerFactory) -> None:
        self._runner_factory = runner_factory
        self._sessions: dict[tuple[str, str, str], SessionExecutionHandle] = {}

    async def start_agent(self, user_id: str, agent_id: str) -> None:
        return None

    async def stop_agent(self, user_id: str, agent_id: str) -> None:
        handles = [handle for key, handle in self._sessions.items() if key[:2] == (user_id, agent_id)]
        await asyncio.gather(*(self.close_session(handle) for handle in handles), return_exceptions=True)

    async def load_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        replay: dict[str, Any] | None,
        profile_snapshot: AgentProfile,
    ) -> SessionExecutionHandle:
        key = (user_id, agent_id, session_id)
        if key in self._sessions:
            return self._sessions[key]
        runner = self._runner_factory.create_runner(user_id)
        if replay is not None:
            runner.load_session(session_id, replay)
        handle = SessionExecutionHandle(user_id=user_id, agent_id=agent_id, session_id=session_id, runner=runner)
        self._sessions[key] = handle
        return handle

    async def start_run(
        self,
        session_handle: SessionExecutionHandle,
        run_ref: RunRef,
        request: GatewayInput,
        profile_snapshot: AgentProfile,
        event_scope: RunEventScope,
    ) -> RunExecutionHandle:
        await session_handle.runner.start_resource_run(
            request,
            run_ref=run_ref,
            profile=profile_snapshot,
            event_scope=event_scope,
        )
        return RunExecutionHandle(run_ref=run_ref, runner=session_handle.runner, event_scope=event_scope)

    def get_session_snapshot(self, session_handle: SessionExecutionHandle) -> dict[str, Any]:
        return session_handle.runner.get_status_snapshot()

    async def append_message(self, handle: SessionExecutionHandle, request: GatewayInput, receipt: dict[str, Any]) -> dict[str, Any]:
        return await handle.runner.append_message(request, receipt)

    async def wait_run(self, run_handle: RunExecutionHandle) -> RunExecutionResult:
        session = await run_handle.runner.wait_current_run()
        status = RunStatus.COMPLETED
        if session and session.status == SessionStatus.FAILED:
            status = RunStatus.FAILED
        elif session and session.status == SessionStatus.CANCELLED:
            status = RunStatus.CANCELLED
        uncertain = bool(session and session.metadata.get("external_effect_uncertain"))
        return RunExecutionResult(
            status=status,
            error_code="cancellation_uncertain" if uncertain else session.stop_reason if session else None,
            external_effect_uncertain=uncertain,
        )

    async def cancel_run(
        self, session_handle: SessionExecutionHandle, run_ref: RunRef
    ) -> CancellationResult:
        await session_handle.runner.handle_input(GatewayInput(type=GatewayInputType.STOP))
        try:
            await asyncio.wait_for(session_handle.runner.wait_current_run(), timeout=1)
        except TimeoutError:
            try:
                await asyncio.wait_for(session_handle.runner.force_cancel_current_run(), timeout=9)
            except Exception:
                return CancellationResult(
                    confirmed=False,
                    external_effect_uncertain=True,
                    error_code="cancellation_uncertain",
                )
        return CancellationResult(confirmed=True)

    async def reply_interaction(
        self,
        session_handle: SessionExecutionHandle,
        ref: HumanInteractionRef,
        payload: GatewayInput,
    ) -> None:
        await session_handle.runner.handle_input(payload)

    async def close_session(self, session_handle: SessionExecutionHandle) -> None:
        await session_handle.runner.shutdown()
        self._sessions.pop((session_handle.user_id, session_handle.agent_id, session_handle.session_id), None)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close_session(handle) for handle in list(self._sessions.values())), return_exceptions=True)


@dataclass(slots=True)
class SessionRuntimeHandle:
    execution: SessionExecutionHandle
    profile_revision_id: str
    active_run_id: str | None = None
    last_accessed_at: str = ""


@dataclass(slots=True)
class _RequestReservation:
    fingerprint: str
    future: asyncio.Future[tuple[RunState | None, BaseException | None]]


class AgentRuntimeManager:
    """HTTP 主进程唯一使用的多 Agent 控制面。"""

    def __init__(
        self,
        *,
        workspace: Any,
        config: Any,
        event_bus: EventBus,
        session_memory: UserPartitionedSessionMemory,
        profile_provider: AgentProfileProvider,
        backend: AgentRuntimeBackend,
        max_active_runs: int = 1,
        max_started_agents: int = 5,
        max_active_runs_per_user: int = 2,
        max_started_agents_per_user: int = 3,
        schedule_coordinator: Any = None,
    ) -> None:
        self._config = config
        self._schedule_coordinator = schedule_coordinator
        self._workspace = workspace
        self._event_bus = event_bus
        self._session_memory = session_memory
        self._legacy_mode = not isinstance(session_memory, UserPartitionedSessionMemory)
        self._profile_provider = profile_provider
        self._backend = backend
        self._max_active_runs = max_active_runs
        self._max_started_agents = max_started_agents
        self._max_active_runs_per_user = max_active_runs_per_user
        self._max_started_agents_per_user = max_started_agents_per_user
        self._runtimes: dict[UserAgentKey, AgentRuntimeState] = {}
        self._sessions: dict[SessionKey, SessionRuntimeHandle] = {}
        self._runs: dict[RunKey, RunState] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, RunKey]] = {}
        self._request_reservations: dict[tuple[str, str], _RequestReservation] = {}
        self._active_sessions: dict[SessionKey, str] = {}
        self._active_run_total = 0
        self._executions: dict[RunKey, RunExecutionHandle] = {}
        self._run_locks: dict[RunKey, asyncio.Lock] = {}
        self._terminal_events: dict[RunKey, asyncio.Event] = {}
        self._interactions: dict[tuple[str, str, str, str, str], HumanInteractionState] = {}
        self._watchers: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._state_persist_lock = asyncio.Lock()
        self._state_generation: dict[str, int] = {}
        self._persisted_state_generation: dict[str, int] = {}
        self._stores = UserRuntimeStoreRegistry(workspace.workspace_dir)
        self._control_buses: dict[str, EventBus] = {}
        self._recovery_error: str | None = None
        self._recovered = False
        self._submission_locks: dict[str, asyncio.Lock] = {}
        self._submissions: dict[tuple[str, str], dict[str, Any]] = {}
        self._submission_users_loaded: set[str] = set()

    async def submit_message(
        self, user_id: str, agent_id: str, request: GatewayInput,
        session_id: str | None, client_request_id: str, expected_run_id: str | None = None,
    ) -> dict[str, Any]:
        """发送入口统一分流；幂等判定先于活动 Run 判定，防止重试意外重启。"""
        self._ensure_recovered()
        _validate_resource_id(client_request_id, "client_request_id", max_length=128)
        self._record(user_id, agent_id)
        if session_id:
            _validate_resource_id(session_id, "session_id")
        if expected_run_id:
            _validate_resource_id(expected_run_id, "expected_run_id")
            if not session_id:
                raise ValueError("expected_run_id 必须同时指定 session_id")
        request = request.model_copy(update={"metadata": {**request.metadata, "expected_run_id": expected_run_id}})
        fingerprint = _request_fingerprint(user_id, agent_id, session_id, request)
        key = (user_id, client_request_id)
        # 只串行同一用户的短提交阶段，模型和工具始终在独立后台任务中执行。
        async with self._submission_locks.setdefault(user_id, asyncio.Lock()):
            if user_id not in self._submission_users_loaded:
                for summary in self._memory_list_sessions(user_id):
                    replay = await self._memory_replay(user_id, summary["session_id"])
                    for item in replay.get("submissions", []):
                        self._submissions[(user_id, item["client_request_id"])] = {key: value for key, value in item.items() if key != "message"}
                self._submission_users_loaded.add(user_id)
            existing = self._submissions.get(key)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise RuntimeConflict("client_request_conflict", "client_request_id 已用于不同请求")
                run = self._require_run(RunRef.model_validate(existing["ref"]))
                return {**run.model_dump(), "submission": self._public_submission(existing)}
            if key in self._idempotency:
                old_fingerprint, run_key = self._idempotency[key]
                if old_fingerprint != fingerprint:
                    raise RuntimeConflict("client_request_conflict", "client_request_id 已用于不同请求")
                return self._runs[run_key].model_dump()
            session_key = (user_id, agent_id, session_id)
            if self._schedule_coordinator is not None:
                self._schedule_coordinator.assert_session_idle(user_id, agent_id, session_id)
            async with self._lock:
                active_id = self._active_sessions.get(session_key)
                run = self._runs.get((*session_key, active_id)) if active_id else None
                handle = self._sessions.get(session_key)
            if expected_run_id and active_id != expected_run_id:
                raise RuntimeConflict("expected_run_conflict", "原执行已结束或发生变化，请刷新后再发送")
            if run is not None:
                if run.status != RunStatus.RUNNING or handle is None:
                    raise RuntimeConflict("session_not_accepting", "当前正在启动、停止或等待人工处理，请稍后再发送")
                if ((request.provider is not None and request.provider != run.provider)
                    or (request.model is not None and request.model != run.model)
                    or ("thinking_value" in request.metadata and request.metadata["thinking_value"] != run.thinking_value)):
                    raise RuntimeConflict("active_model_conflict", "追加消息不能修改当前执行的模型设置")
                state = self._runtimes.get((user_id, agent_id))
                if state is None or state.lifecycle_state != AgentLifecycleState.RUNNING:
                    raise RuntimeConflict("agent_not_running", "Agent 已停止或正在关闭")
                receipt = {"mode": "appended", "client_request_id": client_request_id,
                           "fingerprint": fingerprint, "ref": run.ref.model_dump()}
                execution = self._executions.get(_run_key(run.ref))
                if execution is None:
                    raise RuntimeConflict("session_not_accepting", "当前执行正在结束，请刷新后再发送")
                try:
                    item = await self._backend.append_message(handle.execution, request, receipt)
                except ValueError as exc:
                    raise RuntimeConflict("session_not_accepting", str(exc)) from exc
                self._submissions[key] = {field: value for field, value in item.items() if field != "message"}
                await execution.event_scope.publish_stream_event(StreamEvent(
                    event_type="message_submission", session_id=session_id,
                    created_at=utc_now_iso(), data={"submissions": [self._public_submission(item, with_message=True)]},
                ))
                return {**run.model_dump(), "submission": self._public_submission(item)}
            message_id = f"msg_{uuid4().hex}"
            request = request.model_copy(update={"metadata": {**request.metadata, "_message_id": message_id}})
            run = await self.start_run(user_id, agent_id, request, session_id, client_request_id)
            return run.model_dump()

    @staticmethod
    def _public_submission(item: dict[str, Any], *, with_message: bool = False) -> dict[str, Any]:
        result = public_submission(item)
        if not with_message:
            result.pop("message", None)
        return result

    async def recover(self, enabled_user_ids: set[str]) -> None:
        """恢复期望启动状态与幂等索引，不重放任何旧副作用。"""
        # lifespan 完成前不会接收请求；该标记表示恢复流程已经被执行过。
        self._recovered = True
        payloads: dict[str, dict[str, Any]] = {}
        try:
            for user_id in self._stores.known_user_ids():
                payload, recovered = await asyncio.gather(
                    self._stores.state(user_id).read(),
                    self._stores.runs(user_id).recover(),
                )
                control = self._stores.control(user_id)
                await control.recover()
                self._control_bus(user_id).set_initial_seq(control.current_seq)
                payloads[user_id] = payload
                runs, idempotency = recovered
                for old_key, run in runs.items():
                    run.ref.user_id = run.ref.user_id or user_id
                    key = _run_key(run.ref)
                    self._runs[key] = run
                for client_request_id, (fingerprint, old_key) in idempotency.items():
                    run = runs[old_key]
                    self._idempotency[(user_id, client_request_id)] = (fingerprint, _run_key(run.ref))
        except RuntimeStoreCorrupt:
            self._recovery_error = "runtime_recovery_incomplete"
            return

        self._run_locks = {key: asyncio.Lock() for key in self._runs}
        self._terminal_events = {key: asyncio.Event() for key in self._runs}
        for key, run in self._runs.items():
            if run.status in _TERMINAL_RUN_STATUSES:
                self._terminal_events[key].set()
        for run in list(self._runs.values()):
            if run.status in _ACTIVE_RUN_STATUSES:
                await self._transition_terminal(run, RunStatus.CANCELLED, error_code="service_restarted")

        for user_id, payload in payloads.items():
            agents = payload.get("agents") if isinstance(payload, dict) else {}
            if not isinstance(agents, dict):
                self._recovery_error = "runtime_recovery_incomplete"
                return
            for agent_id, item in agents.items():
                if not isinstance(item, dict):
                    continue
                recent_session_id = _string_or_none(item.get("recent_session_id"))
                if user_id not in enabled_user_ids:
                    self._runtimes[(user_id, agent_id)] = AgentRuntimeState(
                        user_id=user_id,
                        agent_id=agent_id,
                        desired_state=AgentLifecycleState.STOPPED,
                        lifecycle_state=AgentLifecycleState.STOPPED,
                        recent_session_id=recent_session_id,
                    )
                    continue
                if item.get("desired_state") != AgentLifecycleState.RUNNING.value:
                    continue
                capacity_error: str | None = None
                if self._started_count() >= self._max_started_agents:
                    capacity_error = (
                        "started_agent_capacity_exceeded"
                        if self._legacy_mode
                        else "service_started_agent_capacity_exceeded"
                    )
                elif user_id != "legacy" and self._started_count(user_id) >= self._max_started_agents_per_user:
                    capacity_error = "user_started_agent_capacity_exceeded"
                if capacity_error is not None:
                    self._runtimes[(user_id, agent_id)] = AgentRuntimeState(
                        user_id=user_id,
                        agent_id=agent_id,
                        desired_state=AgentLifecycleState.STOPPED,
                        lifecycle_state=AgentLifecycleState.STOPPED,
                        recent_session_id=recent_session_id,
                        error_code=capacity_error,
                    )
                    continue
                try:
                    self._profile_provider.get_record_snapshot(user_id, agent_id)
                    await self._backend_start_agent(user_id, agent_id)
                except Exception:  # 配置可能已删除或损坏，保留 ERROR 供用户处理。
                    lifecycle, error = AgentLifecycleState.ERROR, "agent_recovery_failed"
                else:
                    lifecycle, error = AgentLifecycleState.RUNNING, None
                self._runtimes[(user_id, agent_id)] = AgentRuntimeState(
                    user_id=user_id,
                    agent_id=agent_id,
                    desired_state=AgentLifecycleState.RUNNING,
                    lifecycle_state=lifecycle,
                    recent_session_id=recent_session_id,
                    error_code=error,
                )
            if agents:
                await self._persist_states(user_id)

    async def disable_user(self, user_id: str) -> None:
        """撤销用户的进程内活动；已经发生的外部副作用不做补偿回滚。"""
        self._event_bus.close_user_subscriptions(user_id)
        control_bus = self._control_buses.get(user_id)
        if control_bus is not None:
            control_bus.close_user_subscriptions(user_id)
        agent_ids = [agent_id for owner, agent_id in self._runtimes if owner == user_id]
        await asyncio.gather(
            *(self.stop_agent(user_id, agent_id, persist_desired=True) for agent_id in agent_ids),
            return_exceptions=True,
        )
        async with self._lock:
            for interaction in self._interactions.values():
                if interaction.ref.user_id == user_id and interaction.status in {InteractionStatus.PENDING, InteractionStatus.RESOLVING}:
                    interaction.status = InteractionStatus.CANCELLED
                    interaction.ended_at = utc_now_iso()

    async def shutdown(self) -> None:
        targets = [run.ref for run in self._runs.values() if run.status in _ACTIVE_RUN_STATUSES]
        await asyncio.gather(*(self.cancel_run(ref) for ref in targets), return_exceptions=True)
        if self._watchers:
            await asyncio.gather(*self._watchers, return_exceptions=True)
        try:
            await asyncio.wait_for(self._backend.shutdown(), timeout=10)
        except TimeoutError:
            self._recovery_error = "runtime_recovery_incomplete"

    async def start_agent(self, user_id: str, agent_id: str | None = None) -> AgentRuntimeState:
        user_id, agent_id = _normalize_user_agent(user_id, agent_id)
        profile = self._active_profile(user_id, agent_id)
        if profile.kind != "agent":
            raise RuntimeConflict("agent_not_runnable", "subagent 不能直接启动")
        if profile.publication_id:
            publications = self._profile_provider.publications
            settings = publications.settings(user_id, profile.publication_id)
            if settings["revision"] is None:
                publications.save_settings(user_id, profile.publication_id, {"working_directory": None, "connections": {}}, None)
        async with self._lock:
            key = (user_id, agent_id)
            current = self._runtimes.get(key)
            if current and current.desired_state == AgentLifecycleState.RUNNING:
                return current
            if current and current.lifecycle_state == AgentLifecycleState.STOPPING:
                raise RuntimeConflict("agent_stopping", "Agent 正在关闭，请等待终态")
            if current and current.error_code == "cancellation_uncertain":
                raise RuntimeConflict("cancellation_uncertain", "上次取消结果不确定，需重启服务后再启动")
            if self._started_count() >= self._max_started_agents:
                code = "started_agent_capacity_exceeded" if self._legacy_mode else "service_started_agent_capacity_exceeded"
                raise RuntimeConflict(code, "服务已启动 Agent 数量已达上限")
            if user_id != "legacy" and self._started_count(user_id) >= self._max_started_agents_per_user:
                raise RuntimeConflict("user_started_agent_capacity_exceeded", "当前用户已启动 Agent 数量已达上限")
            state = AgentRuntimeState(
                user_id=user_id,
                agent_id=agent_id,
                desired_state=AgentLifecycleState.RUNNING,
                lifecycle_state=AgentLifecycleState.STARTING,
            )
            self._runtimes[key] = state
        await self._persist_states(user_id)
        await self._publish_control(user_id, "agent_starting", agent_id=agent_id)
        try:
            await self._backend_start_agent(user_id, agent_id)
        except Exception as exc:
            async with self._lock:
                state.lifecycle_state = AgentLifecycleState.ERROR
                state.error_code = "agent_start_failed"
            await self._publish_control(
                user_id,
                "agent_error",
                agent_id=agent_id,
                data={"error_code": "agent_start_failed"},
            )
            raise RuntimeConflict("agent_start_failed", "Agent 启动失败") from exc
        async with self._lock:
            state.lifecycle_state = AgentLifecycleState.RUNNING
        await self._publish_control(user_id, "agent_running", agent_id=agent_id)
        return state

    async def stop_agent(self, user_id: str, agent_id: str | None = None, *, persist_desired: bool = True) -> AgentRuntimeState:
        user_id, agent_id = _normalize_user_agent(user_id, agent_id)
        self._record(user_id, agent_id)
        async with self._lock:
            runtime_key = (user_id, agent_id)
            state = self._runtimes.setdefault(runtime_key, AgentRuntimeState(user_id=user_id, agent_id=agent_id))
            if persist_desired:
                state.desired_state = AgentLifecycleState.STOPPED
            already_stopped = state.lifecycle_state == AgentLifecycleState.STOPPED
            if not already_stopped:
                state.lifecycle_state = AgentLifecycleState.STOPPING
            targets = [] if already_stopped else [
                run.ref
                for run in self._runs.values()
                if run.ref.user_id == user_id and run.ref.agent_id == agent_id and run.status in _ACTIVE_RUN_STATUSES
            ]
        if persist_desired:
            await self._persist_states(user_id)
        if already_stopped:
            return state
        await self._publish_control(user_id, "agent_stopping", agent_id=agent_id)
        uncertain = False
        try:
            async with asyncio.timeout(10):
                results = await asyncio.gather(*(self.cancel_run(ref) for ref in targets), return_exceptions=True)
                uncertain = any(
                    isinstance(result, RuntimeConflict) and result.code == "cancellation_uncertain"
                    for result in results
                )
                if not uncertain:
                    await self._backend_stop_agent(user_id, agent_id)
        except TimeoutError:
            uncertain = True
        async with self._lock:
            if not uncertain:
                for key in [key for key in self._sessions if key[:2] == (user_id, agent_id)]:
                    self._sessions.pop(key, None)
            state.lifecycle_state = AgentLifecycleState.ERROR if uncertain else AgentLifecycleState.STOPPED
            state.error_code = "cancellation_uncertain" if uncertain else None
        await self._publish_control(user_id, "agent_error" if uncertain else "agent_stopped", agent_id=agent_id)
        return state

    async def start_run(
        self,
        user_id: str,
        agent_id: str | GatewayInput,
        request: GatewayInput | str | None = None,
        session_id: str | None = None,
        client_request_id: str = "",
    ) -> RunState:
        if isinstance(agent_id, GatewayInput):
            # 旧单用户调用形态：(agent_id, request, session_id, client_request_id)。
            old_request, old_session, old_client_id = agent_id, request, session_id
            user_id, agent_id, request = "legacy", user_id, old_request
            session_id = old_session if isinstance(old_session, str) else None
            client_request_id = old_client_id or client_request_id
        assert isinstance(agent_id, str) and isinstance(request, GatewayInput)
        self._ensure_recovered()
        if request.type != GatewayInputType.USER_MESSAGE:
            raise ValueError("仅 user_message 可以创建 Run")
        _validate_resource_id(client_request_id, "client_request_id", max_length=128)
        target_session = session_id or request.session_id
        if target_session:
            _validate_resource_id(target_session, "session_id")
        fingerprint = _request_fingerprint(user_id, agent_id, target_session, request)
        request_key = (user_id, client_request_id)

        leader = False
        async with self._lock:
            reservation = self._request_reservations.get(request_key)
            if reservation is not None:
                if reservation.fingerprint != fingerprint:
                    raise RuntimeConflict("client_request_conflict", "client_request_id 已用于不同请求")
                future = reservation.future
            else:
                existing = self._idempotency.get(request_key)
                if existing:
                    old_fingerprint, key = existing
                    if old_fingerprint != fingerprint:
                        raise RuntimeConflict("client_request_conflict", "client_request_id 已用于不同请求")
                    return self._runs[key]
                future = asyncio.get_running_loop().create_future()
                self._request_reservations[request_key] = _RequestReservation(
                    fingerprint=fingerprint,
                    future=future,
                )
                leader = True

        if not leader:
            run, error = await asyncio.shield(future)
            if error is not None:
                raise error
            assert run is not None
            return run

        try:
            run = await self._start_run_reserved(
                agent_id=agent_id,
                user_id=user_id,
                request=request,
                target_session=target_session,
                client_request_id=client_request_id,
                fingerprint=fingerprint,
            )
        except BaseException as exc:
            async with self._lock:
                self._request_reservations.pop(request_key, None)
                if not future.done():
                    future.set_result((None, exc))
            raise
        async with self._lock:
            self._request_reservations.pop(request_key, None)
            if not future.done():
                future.set_result((run, None))
        return run

    async def _start_run_reserved(
        self,
        *,
        user_id: str,
        agent_id: str,
        request: GatewayInput,
        target_session: str | None,
        client_request_id: str,
        fingerprint: str,
    ) -> RunState:
        """执行首个幂等请求；调用方已持有 workspace 级请求预留。"""
        if self._schedule_coordinator is not None:
            self._schedule_coordinator.assert_session_idle(user_id, agent_id, target_session)
        profile = self._active_profile(user_id, agent_id)
        runtime_key = (user_id, agent_id)
        state = self._runtimes.get(runtime_key)
        if state is None or state.lifecycle_state != AgentLifecycleState.RUNNING:
            raise RuntimeConflict("agent_not_running", "Agent 尚未启动")

        if target_session:
            replay = await self._memory_replay(user_id, target_session)
            if self._schedule_coordinator is not None and self._schedule_coordinator.session_run(user_id, agent_id, target_session):
                store = self._schedule_coordinator.event_store
                if store is not None:
                    # 人工接续切换进程后仍保持 Session SSE 序号单调，不丢掉新消息。
                    self._event_bus.set_initial_seq(store.latest_seq(user_id, target_session), user_id)
            provider, model, thinking = self._locked_session_llm(user_id, agent_id, profile, replay, request)
            session_id_value = target_session
        else:
            replay = None
            provider, model, thinking = self._resolve_new_session_llm(profile, request)
            session_id_value = f"sess_{uuid4().hex}"
        key_base = (user_id, agent_id, session_id_value)
        run_id = f"run_{uuid4().hex}"
        run = RunState(
            ref=RunRef(user_id=user_id, agent_id=agent_id, session_id=session_id_value, run_id=run_id, revision_id=profile.revision_id),
            client_request_id=client_request_id,
            status=RunStatus.STARTING,
            created_at=utc_now_iso(),
            request_fingerprint=fingerprint,
            provider=provider,
            model=model,
            thinking_value=thinking,
            submission={"mode": "started", "message_id": request.metadata["_message_id"], "status": "accepted"}
            if request.metadata.get("_message_id") else None,
        )
        key = (*key_base, run_id)
        async with self._lock:
            # stop 与容量/Session 预留共享同一线性化点，STOPPING 后不会漏启动 Run。
            if profile.publication_id:
                publication = self._profile_provider.publications.get(profile.publication_id)
                if publication["status"] != "published":
                    raise RuntimeConflict("publication_unavailable", "公共 Agent 已撤回或下架")
            state = self._runtimes.get(runtime_key)
            if (
                state is None
                or state.desired_state != AgentLifecycleState.RUNNING
                or state.lifecycle_state != AgentLifecycleState.RUNNING
            ):
                raise RuntimeConflict("agent_not_running", "Agent 尚未启动或正在关闭")
            if self._active_run_total >= self._max_active_runs:
                code = "run_capacity_exceeded" if self._legacy_mode else "service_run_capacity_exceeded"
                raise RuntimeConflict(code, "服务活动 Run 容量已满", retry_after=1)
            if user_id != "legacy" and self._active_run_count(user_id) >= self._max_active_runs_per_user:
                raise RuntimeConflict("user_run_capacity_exceeded", "当前用户活动 Run 容量已满", retry_after=1)
            if key_base in self._active_sessions:
                raise RuntimeConflict("session_run_conflict", "目标 Session 已有活动 Run")
            self._runs[key] = run
            self._run_locks[key] = asyncio.Lock()
            self._terminal_events[key] = asyncio.Event()
            self._idempotency[(user_id, client_request_id)] = (fingerprint, key)
            self._active_sessions[key_base] = run_id
            self._active_run_total += 1
            state.active_run_count += 1
            session_handle = self._sessions.get(key_base)
        try:
            await self._stores.runs(user_id).append(run)
        except Exception:
            async with self._lock:
                self._runs.pop(key, None)
                self._run_locks.pop(key, None)
                self._terminal_events.pop(key, None)
                self._idempotency.pop((user_id, client_request_id), None)
                self._active_sessions.pop(key_base, None)
                self._active_run_total = max(0, self._active_run_total - 1)
                state.active_run_count = max(0, state.active_run_count - 1)
            raise

        try:
            if session_handle is None:
                execution = await self._backend_load_session(user_id, agent_id, session_id_value, replay, profile)
                session_handle = SessionRuntimeHandle(
                    execution=execution,
                    profile_revision_id=profile.revision_id,
                    last_accessed_at=utc_now_iso(),
                )
                self._sessions[key_base] = session_handle
            prepared = request.model_copy(
                update={
                    "session_id": session_id_value,
                    "agent_name": profile.name,
                    "provider": provider,
                    "model": model,
                    "metadata": {
                        "user_metadata": request.metadata.get("user_metadata", {}),
                        "thinking_value": thinking,
                        "user_id": user_id,
                        "agent_id": agent_id,
                        "revision_id": profile.revision_id,
                        "run_id": run_id,
                        "_message_id": request.metadata.get("_message_id"),
                    },
                }
            )
            scope = RunEventScope(self._event_bus, run.ref, initial_run_seq=run.run_seq)
            execution = await self._backend.start_run(session_handle.execution, run.ref, prepared, profile, scope)
        except Exception:
            await self._transition_terminal(run, RunStatus.FAILED, error_code="backend_start_failed")
            raise
        async with self._lock:
            session_handle.active_run_id = run_id
            run.status = RunStatus.RUNNING
            run.started_at = utc_now_iso()
            state.recent_session_id = session_id_value
            self._executions[key] = execution
        try:
            await self._stores.runs(user_id).append(run)
            await self._persist_states(user_id)
            await self._publish_control(user_id, "run_running", run=run)
        except Exception as exc:
            await asyncio.gather(
                self._backend.cancel_run(session_handle.execution, run.ref),
                return_exceptions=True,
            )
            async with self._lock:
                run.status = RunStatus.FAILED
                run.error_code = "runtime_state_persist_failed"
                run.ended_at = utc_now_iso()
                state.active_run_count = max(0, state.active_run_count - 1)
                session_handle.active_run_id = None
                self._active_sessions.pop(key_base, None)
                self._active_run_total = max(0, self._active_run_total - 1)
                state.lifecycle_state = AgentLifecycleState.ERROR
                state.error_code = "runtime_state_persist_failed"
                self._recovery_error = "runtime_recovery_incomplete"
            raise RuntimeConflict("runtime_recovery_incomplete", "Run 状态持久化失败，已停止执行") from exc
        watcher = asyncio.create_task(self._watch_run(run, execution), name=f"codepilot-run-watch-{run_id}")
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return run

    async def cancel_run(self, ref: RunRef) -> RunState:
        run = self._require_run(ref)
        key = _run_key(ref)
        run_lock = self._run_locks.setdefault(key, asyncio.Lock())
        terminal_event = self._terminal_events.setdefault(key, asyncio.Event())
        async with run_lock:
            if run.status in _TERMINAL_RUN_STATUSES:
                return run
            if run.status == RunStatus.CANCELLING:
                wait_for_terminal = True
            else:
                wait_for_terminal = False
                run.status = RunStatus.CANCELLING
                await self._stores.runs(ref.user_id).append(run)
            async with self._lock:
                handle = self._sessions.get((ref.user_id, ref.agent_id, ref.session_id))
                for interaction in self._interactions.values():
                    if (
                        interaction.ref.user_id == ref.user_id
                        and interaction.ref.agent_id == ref.agent_id
                        and interaction.ref.session_id == ref.session_id
                        and interaction.ref.run_id == ref.run_id
                        and interaction.status in {InteractionStatus.PENDING, InteractionStatus.RESOLVING}
                    ):
                        interaction.status = InteractionStatus.CANCELLED
                        interaction.ended_at = utc_now_iso()
        if wait_for_terminal:
            await terminal_event.wait()
            return run
        if handle is None:
            return await self._transition_terminal(run, RunStatus.FAILED, error_code="cancellation_uncertain")
        try:
            result = await asyncio.wait_for(self._backend.cancel_run(handle.execution, run.ref), timeout=10)
        except Exception:
            result = CancellationResult(
                confirmed=False,
                external_effect_uncertain=True,
                error_code="cancellation_uncertain",
            )
        if not result.confirmed:
            state = self._runtimes.get((ref.user_id, ref.agent_id))
            if state:
                state.lifecycle_state = AgentLifecycleState.ERROR
                state.error_code = "cancellation_uncertain"
            await self._transition_terminal(run, RunStatus.FAILED, error_code="cancellation_uncertain")
            raise RuntimeConflict("cancellation_uncertain", "无法确认外部副作用已停止")
        return await self._transition_terminal(run, RunStatus.CANCELLED)

    async def reply_interaction(self, ref: RunRef, interaction_id: str, payload: GatewayInput) -> RunState:
        _validate_resource_id(interaction_id, "interaction_id")
        result_fingerprint = hashlib.sha256(
            json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        async with self._lock:
            run = self._require_run(ref)
            handle = self._sessions.get((ref.user_id, ref.agent_id, ref.session_id))
            if handle is None or handle.active_run_id != ref.run_id:
                raise RuntimeConflict("resource_ownership_mismatch", "interaction 不属于目标 Run")
            key = (*_run_key(ref), interaction_id)
            interaction = self._interactions.get(key)
            if interaction and interaction.status == InteractionStatus.RESOLVED:
                if interaction.result_fingerprint == result_fingerprint:
                    return run
                raise RuntimeConflict("interaction_result_conflict", "interaction 已提交不同结果")
            if interaction is None:
                raise RuntimeConflict("interaction_result_conflict", "interaction 已过期或归属不匹配")
            if interaction.status != InteractionStatus.PENDING:
                raise RuntimeConflict("interaction_result_conflict", "interaction 正在处理或已经失效")
            interaction.status = InteractionStatus.RESOLVING
        interaction_ref = HumanInteractionRef(
            user_id=ref.user_id,
            agent_id=ref.agent_id,
            session_id=ref.session_id,
            run_id=ref.run_id,
            interaction_id=interaction_id,
        )
        try:
            await self._backend.reply_interaction(handle.execution, interaction_ref, payload)
        except Exception:
            async with self._lock:
                if run.status in _ACTIVE_RUN_STATUSES:
                    interaction.status = InteractionStatus.PENDING
                else:
                    interaction.status = InteractionStatus.CANCELLED
                    interaction.ended_at = utc_now_iso()
            raise
        async with self._lock:
            interaction.status = InteractionStatus.RESOLVED
            interaction.result_fingerprint = result_fingerprint
            interaction.ended_at = utc_now_iso()
        return run

    async def handle_domain_event(self, event: DomainEvent) -> None:
        """把 Runner 的人工交互事件投影为四级资源索引。"""
        if event.event_type.value == "message_submission" and event.user_id:
            for item in event.data["submissions"]:
                self._submissions[(event.user_id, item["client_request_id"])] = {key: value for key, value in item.items() if key != "message"}
            return
        if not isinstance(event, HumanInteractionEvent):
            return
        if self._legacy_mode and not event.user_id:
            event.user_id = "legacy"
        if not event.user_id or not event.agent_id or not event.session_id or not event.run_id:
            return
        kind = event.data.get("kind")
        if kind not in {"approval", "question"}:
            return
        key = (event.user_id, event.agent_id, event.session_id, event.run_id, event.interaction_id)
        changed_run: RunState | None = None
        async with self._lock:
            if event.data.get("status") == "pending":
                self._interactions[key] = HumanInteractionState(
                    ref=HumanInteractionRef(
                        user_id=event.user_id,
                        agent_id=event.agent_id,
                        session_id=event.session_id,
                        run_id=event.run_id,
                        interaction_id=event.interaction_id,
                    ),
                    kind=kind,
                    status=InteractionStatus.PENDING,
                    created_at=event.created_at,
                )
                state = self._runtimes.get((event.user_id, event.agent_id))
                if state:
                    state.waiting_human_count += 1
                run = self._runs.get((event.user_id, event.agent_id, event.session_id, event.run_id))
                if run is None and self._legacy_mode:
                    run = self._runs.get((event.agent_id, event.session_id, event.run_id))  # type: ignore[arg-type]
                if run and run.status in {RunStatus.RUNNING, RunStatus.STARTING}:
                    run.status = RunStatus.WAITING_HUMAN
                    changed_run = run
            else:
                interaction = self._interactions.get(key)
                if interaction and interaction.status != InteractionStatus.CANCELLED:
                    interaction.status = InteractionStatus.RESOLVED
                    interaction.ended_at = event.created_at
                state = self._runtimes.get((event.user_id, event.agent_id))
                if state:
                    state.waiting_human_count = max(0, state.waiting_human_count - 1)
                run = self._runs.get((event.user_id, event.agent_id, event.session_id, event.run_id))
                if run is None and self._legacy_mode:
                    run = self._runs.get((event.agent_id, event.session_id, event.run_id))  # type: ignore[arg-type]
                if run and run.status == RunStatus.WAITING_HUMAN:
                    run.status = RunStatus.RUNNING
                    changed_run = run
        if changed_run is not None:
            await self._stores.runs(event.user_id).append(changed_run)
        await self._publish_control(
            event.user_id,
            "interaction_pending" if event.data.get("status") == "pending" else "interaction_resolved",
            run=changed_run or self._runs.get((event.user_id, event.agent_id, event.session_id, event.run_id)),
            agent_id=event.agent_id,
            data={
                "interaction_id": event.interaction_id,
                "kind": kind,
                "status": "pending" if event.data.get("status") == "pending" else "resolved",
            },
        )

    async def load_session(self, user_id: str, agent_id: str, session_id: str) -> SessionExecutionHandle:
        if self._schedule_coordinator is not None:
            self._schedule_coordinator.assert_session_idle(user_id, agent_id, session_id)
        _validate_resource_id(session_id, "session_id")
        profile = self._record_profile(user_id, agent_id)
        key = (user_id, agent_id, session_id)
        existing = self._sessions.get(key)
        if existing:
            return existing.execution
        replay = await self._memory_replay(user_id, session_id)
        self._assert_session_owner(replay, user_id, agent_id, profile)
        execution = await self._backend.load_session(user_id, agent_id, session_id, replay, profile)
        self._sessions[key] = SessionRuntimeHandle(
            execution=execution,
            profile_revision_id=profile.revision_id,
            last_accessed_at=utc_now_iso(),
        )
        await self._evict_idle_sessions()
        return execution

    async def validate_session_owner(self, user_id: str, agent_id: str, session_id: str) -> dict[str, Any]:
        """只校验历史 Session 归属，不创建执行 Runner。"""
        _validate_resource_id(session_id, "session_id")
        profile = self._record_profile(user_id, agent_id)
        replay = await self._memory_replay(user_id, session_id)
        self._assert_session_owner(replay, user_id, agent_id, profile)
        return replay

    def get_agent_state(self, user_id: str, agent_id: str | None = None) -> AgentRuntimeState:
        user_id, agent_id = _normalize_user_agent(user_id, agent_id)
        self._record(user_id, agent_id)
        return self._runtimes.get((user_id, agent_id), AgentRuntimeState(user_id=user_id, agent_id=agent_id))

    def readiness_snapshot(self) -> dict[str, Any]:
        """返回不包含内部句柄或异常正文的只读就绪投影。"""
        return {
            "recovered": self._recovered,
            "error_code": self._recovery_error,
            "active_run_count": self._active_run_total,
            "started_agent_count": self._started_count(),
        }

    def get_run_state(self, ref: RunRef) -> RunState:
        return self._require_run(ref)

    def list_agent_states(self, user_id: str) -> list[AgentRuntimeState]:
        profiles = self._list_profiles(user_id)
        active_ids = {item.agent_id for item in profiles}
        ordered_ids = [item.agent_id for item in profiles]
        ordered_ids.extend(agent_id for owner, agent_id in self._runtimes if owner == user_id and agent_id not in active_ids)
        return [
            self._runtimes.get((user_id, agent_id), AgentRuntimeState(user_id=user_id, agent_id=agent_id)).model_copy(deep=True)
            for agent_id in ordered_ids
        ]

    async def get_runtime_overview(self, user_id: str = "legacy") -> dict[str, Any]:
        """在控制事件边界之后复制快照，客户端可从 cursor 无缝续接变化。"""
        cursor = self.current_runtime_cursor(user_id)
        profiles = self._list_profiles(user_id)
        async with self._lock:
            active_ids = {item.agent_id for item in profiles}
            ordered_ids = [item.agent_id for item in profiles]
            ordered_ids.extend(agent_id for owner, agent_id in self._runtimes if owner == user_id and agent_id not in active_ids)
            runtimes = [
                self._runtimes.get((user_id, agent_id), AgentRuntimeState(user_id=user_id, agent_id=agent_id)).model_copy(deep=True)
                for agent_id in ordered_ids
            ]
            capacity = {
                "started_agents": self._started_count(user_id),
                "service_started_agents": self._started_count(),
                "max_started_agents": self._max_started_agents,
                "max_started_agents_per_user": self._max_started_agents_per_user,
                "active_runs": self._active_run_count(user_id),
                "service_active_runs": self._active_run_total,
                "max_active_runs": self._max_active_runs,
                "max_active_runs_per_user": self._max_active_runs_per_user,
            }
            if self._legacy_mode:
                capacity = {
                    "started_agents": capacity["started_agents"],
                    "max_started_agents": capacity["max_started_agents"],
                    "active_runs": capacity["active_runs"],
                    "max_active_runs": capacity["max_active_runs"],
                }
        return {
            "runtimes": [item.model_dump(mode="json") for item in runtimes],
            "capacity": capacity,
            "cursor": cursor,
        }

    async def get_session_runtime_snapshot(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        replay: dict[str, Any],
    ) -> dict[str, Any]:
        """返回页面恢复所需的安全快照，不加载历史 SessionRunner。"""
        data = ((replay.get("session") or {}).get("data") or {})
        session_key = (user_id, agent_id, session_id)
        async with self._lock:
            active_run_id = self._active_sessions.get(session_key)
            active_run = (
                self._runs.get((user_id, agent_id, session_id, active_run_id))
                if active_run_id
                else None
            )
            pending = next(
                (
                    item.model_copy(deep=True)
                    for item in self._interactions.values()
                    if item.ref.user_id == user_id
                    and item.ref.agent_id == agent_id
                    and item.ref.session_id == session_id
                    and item.status in {InteractionStatus.PENDING, InteractionStatus.RESOLVING}
                ),
                None,
            )
            handle = self._sessions.get(session_key)
        live_snapshot = self._backend.get_session_snapshot(handle.execution) if handle else {}
        last_run = _latest_session_run(self._runs.values(), user_id, agent_id, session_id)
        if self._schedule_coordinator is not None and last_run is None:
            scheduled = self._schedule_coordinator.session_run(user_id, agent_id, session_id)
            if scheduled is not None:
                return self._schedule_coordinator.runtime_snapshot(scheduled)
        pending_request = (
            live_snapshot.get("pending_human_request")
            if pending
            and live_snapshot.get("pending_human_interaction_id") == pending.ref.interaction_id
            else None
        )
        return {
            "status": (
                active_run.status.value
                if active_run
                else str(data.get("status") or live_snapshot.get("status") or "IDLE")
            ),
            "stop_reason": last_run.error_code if last_run and not active_run else None,
            "provider": data.get("provider") or live_snapshot.get("provider"),
            "model": data.get("model") or live_snapshot.get("model"),
            "thinking_value": (
                (data.get("metadata") or {}).get("thinking_value")
                or live_snapshot.get("thinking_value")
                if isinstance(data.get("metadata"), dict)
                else live_snapshot.get("thinking_value")
            ),
            "active_run": (
                {
                    "run_id": active_run.ref.run_id,
                    "status": active_run.status.value,
                    "revision_id": active_run.ref.revision_id,
                    "started_at": active_run.started_at,
                }
                if active_run
                else None
            ),
            "last_run": last_run.model_dump(mode="json") if last_run else None,
            "pending_interaction": (
                {
                    "interaction_id": pending.ref.interaction_id,
                    "run_id": pending.ref.run_id,
                    "kind": pending.kind,
                    "request": pending_request if isinstance(pending_request, dict) else {},
                }
                if pending
                else None
            ),
        }

    def list_sessions(self, user_id: str, agent_id: str) -> list[dict[str, Any]]:
        profile = self._record_profile(user_id, agent_id)
        return [
            item
            for item in self._memory_list_sessions(user_id)
            if item.get("agent_id") == agent_id
            or (not item.get("agent_id") and item.get("agent_name") == profile.name)
        ]

    def list_recent_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """一次聚合每个 Agent 的最新 Session，避免首页逐 Agent 回放完整消息。"""
        public_fields = {
            "session_id", "title", "created_at", "updated_at", "status", "stop_reason",
            "agent_name", "provider", "model", "message_count", "preview", "source",
            "schedule_task_id", "schedule_run_id", "schedule_task_name",
        }
        profiles = [profile for profile in self._list_profiles(user_id) if profile.supports_direct]
        active_agent_ids = {profile.agent_id for profile in profiles}
        ids_by_name: dict[str, list[str]] = {}
        for profile in profiles:
            ids_by_name.setdefault(profile.name, []).append(profile.agent_id)

        recent: dict[str, dict[str, Any]] = {}
        for item in self._memory_list_sessions(user_id):
            agent_id = str(item.get("agent_id") or "")
            if not agent_id:
                candidates = ids_by_name.get(str(item.get("agent_name") or ""), [])
                if len(candidates) == 1:
                    agent_id = candidates[0]
            if agent_id not in active_agent_ids:
                continue
            current = recent.get(agent_id)
            item_time = str(item.get("updated_at") or item.get("created_at") or "")
            current_time = str((current or {}).get("updated_at") or (current or {}).get("created_at") or "")
            if current is not None and current_time >= item_time:
                continue
            recent[agent_id] = {
                **{key: item.get(key) for key in public_fields if key in item},
                "agent_id": agent_id,
            }
        return sorted(
            recent.values(),
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )

    def find_active_agent_id(self, user_id: str, agent_name: str) -> str:
        matches = [
            profile.agent_id
            for profile in self._list_profiles(user_id)
            if profile.name == agent_name
        ]
        if len(matches) != 1:
            raise KeyError("Agent 不存在或名称存在歧义")
        return matches[0]

    def get_session_status(self, user_id: str, agent_id: str, session_id: str) -> dict[str, Any]:
        handle = self._sessions.get((user_id, agent_id, session_id))
        if handle is None:
            raise KeyError("Session 未加载")
        return self._backend.get_session_snapshot(handle.execution)

    async def replay_runtime_events(self, user_id: str, cursor: str | None) -> list[tuple[int, str, StreamEvent]]:
        try:
            return await self._stores.control(user_id).replay(cursor)
        except ValueError as exc:
            raise RuntimeConflict("invalid_runtime_cursor", "运行时 cursor 无效", status=422) from exc

    def create_runtime_subscription(self, user_id: str = "legacy") -> Any:
        return self._control_bus(user_id).create_stream_subscription(user_id=user_id)

    def remove_runtime_subscription(self, user_id: str | Any, subscription: Any | None = None) -> None:
        if subscription is None:
            subscription, user_id = user_id, "legacy"
        self._control_bus(user_id).remove_stream_subscription(subscription)

    def current_runtime_cursor(self, user_id: str) -> str:
        return encode_runtime_cursor(self._stores.control(user_id).current_seq)

    def runtime_cursor_for_seq(self, seq: int) -> str:
        return encode_runtime_cursor(seq)

    def _control_bus(self, user_id: str) -> EventBus:
        bus = self._control_buses.get(user_id)
        if bus is None:
            bus = EventBus()
            bus.subscribe_stream(self._stores.control(user_id).append)
            self._control_buses[user_id] = bus
        return bus

    async def _watch_run(self, run: RunState, execution: RunExecutionHandle) -> None:
        try:
            result = await self._backend.wait_run(execution)
            status = result.status
            error_code = result.error_code
            error_summary = result.error_summary
            if result.external_effect_uncertain:
                state = self._runtimes.get((run.ref.user_id, run.ref.agent_id))
                if state is None and not run.ref.user_id:
                    state = self._runtimes.get(run.ref.agent_id)  # type: ignore[arg-type]
                if state:
                    state.lifecycle_state = AgentLifecycleState.ERROR
                    state.error_code = "cancellation_uncertain"
        except asyncio.CancelledError:
            status = RunStatus.CANCELLED
            error_code = None
            error_summary = None
        except Exception:
            status = RunStatus.FAILED
            error_code = "backend_wait_failed"
            error_summary = "后端在等待本次执行结果时失败。"
        run.run_seq = execution.event_scope.run_seq
        await self._transition_terminal(run, status, error_code=error_code, error_summary=error_summary)

    async def _transition_terminal(
        self,
        run: RunState,
        status: RunStatus,
        *,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> RunState:
        key = _run_key(run.ref)
        run_lock = self._run_locks.setdefault(key, asyncio.Lock())
        terminal_event = self._terminal_events.setdefault(key, asyncio.Event())
        async with run_lock:
            async with self._lock:
                if run.status in _TERMINAL_RUN_STATUSES:
                    return run
                if run.status == RunStatus.CANCELLING and status == RunStatus.COMPLETED:
                    status = RunStatus.CANCELLED
                run.status = status
                run.error_code = error_code
                run.error_summary = error_summary or _run_error_summary(error_code)
                run.ended_at = utc_now_iso()
                state = self._runtimes.get((run.ref.user_id, run.ref.agent_id))
                if state is None and not run.ref.user_id:
                    state = self._runtimes.get(run.ref.agent_id)  # type: ignore[arg-type]
                if state:
                    state.active_run_count = max(0, state.active_run_count - 1)
                session_key = (run.ref.user_id, run.ref.agent_id, run.ref.session_id)
                handle = self._sessions.get(session_key)
                if handle and handle.active_run_id == run.ref.run_id:
                    handle.active_run_id = None
                if self._active_sessions.get(session_key) == run.ref.run_id:
                    self._active_sessions.pop(session_key, None)
                    self._active_run_total = max(0, self._active_run_total - 1)
                self._executions.pop(key, None)
            try:
                await self._stores.runs(run.ref.user_id).append(run)
                await self._publish_control(run.ref.user_id, f"run_{status.value.lower()}", run=run)
            except Exception:
                if state:
                    state.lifecycle_state = AgentLifecycleState.ERROR
                    state.error_code = "runtime_state_persist_failed"
                self._recovery_error = "runtime_recovery_incomplete"
            finally:
                terminal_event.set()
        await self._evict_idle_sessions()
        return run

    async def _evict_idle_sessions(self) -> None:
        async with self._lock:
            idle = [
                (key, handle)
                for key, handle in self._sessions.items()
                if key not in self._active_sessions
            ]
            excess = max(0, len(idle) - 20)
            targets = sorted(idle, key=lambda item: item[1].last_accessed_at)[:excess]
            for key, _ in targets:
                self._sessions.pop(key, None)
        if targets:
            await asyncio.gather(
                *(self._backend.close_session(handle.execution) for _, handle in targets),
                return_exceptions=True,
            )

    async def _publish_control(
        self,
        user_id: str,
        event_type: str,
        *,
        agent_id: str | None = None,
        run: RunState | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._control_bus(user_id).publish_stream_event(
            StreamEvent(
                user_id=user_id,
                event_type=event_type,
                agent_id=agent_id or (run.ref.agent_id if run else None),
                session_id=run.ref.session_id if run else None,
                run_id=run.ref.run_id if run else None,
                run_seq=run.run_seq if run else 0,
                created_at=utc_now_iso(),
                data=(
                    data
                    if data is not None
                    else {"status": run.status.value, "error_code": run.error_code}
                    if run
                    else {}
                ),
            )
        )

    async def _persist_states(self, user_id: str) -> None:
        async with self._lock:
            generation = self._state_generation.get(user_id, 0) + 1
            self._state_generation[user_id] = generation
            payload = {
                "schema_version": 2,
                "user_id": user_id,
                "agents": {
                    agent_id: {
                        "desired_state": state.desired_state.value,
                        "recent_session_id": state.recent_session_id,
                        "updated_at": utc_now_iso(),
                    }
                    for (owner, agent_id), state in self._runtimes.items()
                    if owner == user_id
                },
            }
        async with self._state_persist_lock:
            if generation <= self._persisted_state_generation.get(user_id, 0):
                return
            state_store = RuntimeStateStore(self._stores.workspace_dir) if self._legacy_mode else self._stores.state(user_id)
            await state_store.write(payload)
            self._persisted_state_generation[user_id] = generation

    def _active_profile(self, user_id: str, agent_id: str) -> AgentProfile:
        try:
            try:
                profile = self._profile_provider.get_active_profile_snapshot(user_id, agent_id)
            except TypeError:
                profile = self._profile_provider.get_active_profile_snapshot(agent_id)  # type: ignore[call-arg]
            if not profile.supports_direct:
                raise RuntimeConflict(
                    "agent_direct_launch_forbidden",
                    "该 Agent 未启用 direct 模式，不能直接启动。",
                    status=409,
                )
            if profile.working_directory:
                from codepilot.session.working_directory import resolve_working_directory
                resolve_working_directory(self._config, self._workspace.workspace_path, profile.working_directory)
            return profile
        except Exception as exc:
            code = getattr(exc, "code", "agent_not_found")
            status = getattr(exc, "status", 404)
            raise RuntimeConflict(code, str(exc) if hasattr(exc, "code") else "Agent 配置加载失败，请联系管理员", status=status, issues=getattr(exc, "issues", [])) from exc

    def _record(self, user_id: str, agent_id: str) -> dict[str, Any]:
        try:
            try:
                return self._profile_provider.get_record_snapshot(user_id, agent_id)
            except TypeError:
                return self._profile_provider.get_record_snapshot(agent_id)  # type: ignore[call-arg]
        except Exception as exc:
            raise KeyError("Agent 不存在") from exc

    def _record_profile(self, user_id: str, agent_id: str) -> AgentProfile:
        record = self._record(user_id, agent_id)
        profile = record.get("profile")
        if not isinstance(profile, AgentProfile):
            raise RuntimeConflict("agent_invalid", "Agent 配置无效")
        return profile

    def _list_profiles(self, user_id: str) -> list[AgentProfile]:
        try:
            return self._profile_provider.list_active_profile_snapshots(user_id)
        except TypeError:
            return self._profile_provider.list_active_profile_snapshots()  # type: ignore[call-arg]

    async def _memory_replay(self, user_id: str, session_id: str | None) -> dict[str, Any]:
        try:
            return await self._session_memory.replay(user_id, session_id)
        except TypeError:
            return await self._session_memory.replay(session_id)  # type: ignore[call-arg]

    def _memory_list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        try:
            return self._session_memory.list_sessions(user_id)
        except TypeError:
            return self._session_memory.list_sessions()  # type: ignore[call-arg]

    async def _backend_start_agent(self, user_id: str, agent_id: str) -> None:
        try:
            await self._backend.start_agent(user_id, agent_id)
        except TypeError:
            await self._backend.start_agent(agent_id)  # type: ignore[call-arg]

    async def _backend_stop_agent(self, user_id: str, agent_id: str) -> None:
        try:
            await self._backend.stop_agent(user_id, agent_id)
        except TypeError:
            await self._backend.stop_agent(agent_id)  # type: ignore[call-arg]

    async def _backend_load_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        replay: dict[str, Any] | None,
        profile: AgentProfile,
    ) -> SessionExecutionHandle:
        try:
            handle = await self._backend.load_session(user_id, agent_id, session_id, replay, profile)
        except TypeError:
            handle = await self._backend.load_session(agent_id, session_id, replay, profile)  # type: ignore[call-arg]
        if not handle.user_id:
            handle.user_id = user_id
        return handle

    def _require_run(self, ref: RunRef) -> RunState:
        key = _run_key(ref)
        run = self._runs.get(key)
        if run is None:
            # 若 run_id 存在但归属不同，明确返回归属错误，避免跨 Agent 探测。
            raise KeyError("Run 不存在")
        return run

    def _assert_session_owner(
        self,
        replay: dict[str, Any],
        user_id: str,
        agent_id: str,
        profile: AgentProfile,
    ) -> None:
        session = replay.get("session")
        if not isinstance(session, dict) or not isinstance(session.get("data"), dict):
            raise KeyError("Session 不存在")
        data = session["data"]
        if data.get("user_id") not in {None, "", user_id}:
            raise RuntimeConflict("resource_ownership_mismatch", "Session 不属于当前用户")
        owner = data.get("agent_id")
        if owner and owner != agent_id:
            raise RuntimeConflict("resource_ownership_mismatch", "Session 不属于指定 Agent")
        if not owner and data.get("agent_name") != profile.name:
            raise RuntimeConflict("resource_ownership_mismatch", "旧 Session 无法解析到指定 Agent")

    def _locked_session_llm(
        self,
        user_id: str,
        agent_id: str,
        profile: AgentProfile,
        replay: dict[str, Any],
        request: GatewayInput,
    ) -> tuple[str, str, str | None]:
        self._assert_session_owner(replay, user_id, agent_id, profile)
        if profile.publication_id:
            # 公共发布的模型变化在新 Run 生效；私人 Session 继续沿用原有模型锁定规则。
            return self._resolve_new_session_llm(profile, request)
        data = replay["session"]["data"]
        provider, model = data.get("provider"), data.get("model")
        if request.provider and request.model and (request.provider != provider or request.model != model):
            raise RuntimeConflict("session_model_locked", "Session 创建后不能切换 Provider 或 Model")
        if bool(request.provider) != bool(request.model):
            raise ValueError("provider 与 model 必须同时提供")
        return str(provider), str(model), data.get("metadata", {}).get("thinking_value")

    def _resolve_new_session_llm(self, profile: AgentProfile, request: GatewayInput) -> tuple[str, str, str | None]:
        explicit_provider, explicit_model = request.provider, request.model
        if bool(explicit_provider) != bool(explicit_model):
            raise ValueError("provider 与 model 必须同时提供")
        provider, model = explicit_provider, explicit_model
        using_default = not provider
        if not provider:
            provider, model = profile.default_provider, profile.default_model
        if not provider or not model:
            raise ValueError("请提供完整 Provider/Model，或先为 Agent 配置默认模型")
        activated, selected = resolve_llm_selection(
            settings=self._config,
            requested_provider=provider,
            requested_model=model,
        )
        thinking = resolve_thinking_value(
            settings=self._config,
            provider=activated.provider,
            model=selected,
            metadata=request.metadata,
        )
        if "thinking_value" not in request.metadata and using_default:
            thinking = profile.default_thinking_value
        return activated.provider, selected, thinking

    def _active_run_count(self, user_id: str | None = None) -> int:
        if user_id is None:
            return self._active_run_total
        return sum(run.ref.user_id == user_id and run.status in _ACTIVE_RUN_STATUSES for run in self._runs.values())

    def _started_count(self, user_id: str | None = None) -> int:
        return sum(
            state.desired_state == AgentLifecycleState.RUNNING
            and state.lifecycle_state in {AgentLifecycleState.RUNNING, AgentLifecycleState.STARTING, AgentLifecycleState.ERROR}
            and (user_id is None or owner == user_id)
            for (owner, _), state in self._runtimes.items()
        )

    def _ensure_recovered(self) -> None:
        if self._recovery_error:
            raise RuntimeConflict("runtime_recovery_incomplete", "运行态恢复不完整，已拒绝新的 Run")


_ACTIVE_RUN_STATUSES = {
    RunStatus.STARTING,
    RunStatus.RUNNING,
    RunStatus.WAITING_HUMAN,
    RunStatus.CANCELLING,
}
_TERMINAL_RUN_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
_RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_resource_id(value: str, field: str, *, max_length: int = 160) -> None:
    if not value or len(value) > max_length or not _RESOURCE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field} 格式无效")


def _request_fingerprint(
    user_id: str,
    agent_id: str | None,
    session_id: str | GatewayInput | None = None,
    request: GatewayInput | None = None,
) -> str:
    if request is None and isinstance(session_id, GatewayInput):
        request, session_id, agent_id, user_id = session_id, agent_id, user_id, "legacy"
    assert request is not None and isinstance(session_id, (str, type(None))) and agent_id is not None
    attachments: list[dict[str, str]] = []
    for item in request.attachments:
        data, mime = decode_image_attachment(item.data_base64, item.mime)
        attachments.append(
            {
                "sha256": hashlib.sha256(data).hexdigest(),
                "mime": mime,
                "filename": sanitize_attachment_filename(item.filename),
            }
        )
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id or "__new_session__",
        "content_sha256": hashlib.sha256((request.content or "").encode("utf-8")).hexdigest(),
        "attachments": attachments,
        "provider": request.provider or "__agent_default__",
        "model": request.model or "__agent_default__",
        "thinking_value": request.metadata.get("thinking_value", "__agent_default__"),
    }
    # 空扩展维持旧指纹，已有 Run 的客户端重试仍然可识别。
    if request.metadata.get("user_metadata"):
        payload["user_metadata"] = request.metadata["user_metadata"]
    if request.metadata.get("expected_run_id"):
        payload["expected_run_id"] = request.metadata["expected_run_id"]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _run_key(ref: RunRef) -> RunKey:
    return ref.user_id, ref.agent_id, ref.session_id, ref.run_id


def _normalize_user_agent(user_id: str, agent_id: str | None) -> tuple[str, str]:
    return (user_id, agent_id) if agent_id is not None else ("legacy", user_id)


def _latest_session_run(runs: Any, user_id: str, agent_id: str, session_id: str) -> RunState | None:
    matches = [
        run
        for run in runs
        if run.ref.user_id == user_id and run.ref.agent_id == agent_id and run.ref.session_id == session_id
    ]
    return max(matches, key=lambda run: run.created_at, default=None)


def _run_error_summary(error_code: str | None) -> str | None:
    summaries = {
        "backend_start_failed": "后端未能启动本次执行。",
        "backend_wait_failed": "后端在等待本次执行结果时失败。",
        "cancellation_uncertain": "取消结果不确定，重启服务后再试。",
        "runtime_state_persist_failed": "运行状态保存失败，已阻止继续执行。",
        "service_restarted": "服务重启中断了本次执行。",
        "max_iterations": "已停止：达到轮数上限。",
        "user_requested": "用户已停止本次执行。",
    }
    return summaries.get(error_code, "本次执行失败，请查看会话中的运行错误。" if error_code else None)
