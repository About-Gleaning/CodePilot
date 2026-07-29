from __future__ import annotations

"""面向资源化 API 的同进程 Agent 运行时。

CODE-49 只开放一个活动 Run，但该限制只存在于本类的容量策略；每个
SessionRunner 仍独立持有会话、停止信号与人工交互状态，CODE-51 提升容量时
不需要改动 Runner 或 HTTP 资源路径。
"""

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.config.settings import resolve_llm_selection, resolve_thinking_value
from codepilot.events import StreamEvent
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.memory import JsonlSessionMemory
from codepilot.session.agents import AgentProfile
from codepilot.session.session_runner import SessionRunner
from codepilot.session.state import AgentLifecycleState, AgentRuntimeState, RunRef, RunState, RunStatus, SessionStatus
from codepilot.utils import utc_now_iso


class RuntimeConflict(ValueError):
    """资源归属、幂等或容量冲突。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionRunnerFactory:
    """为每个 Session 创建互不共享可变状态的 Runner。"""

    def __init__(self, create: Callable[[], SessionRunner]) -> None:
        self._create = create

    def create_runner(self) -> SessionRunner:
        return self._create()


class InProcessAgentRuntimeBackend:
    """CODE-49 的同进程后端，实现最终形态的资源归属边界。"""

    def __init__(
        self,
        *,
        workspace: Any,
        config: Any,
        event_bus: Any,
        session_memory: JsonlSessionMemory,
        agent_profiles: dict[str, AgentProfile],
        runner_factory: SessionRunnerFactory,
        max_active_runs: int = 1,
        max_running_agents: int = 1,
    ) -> None:
        self._workspace = workspace
        self._config = config
        self._event_bus = event_bus
        self._session_memory = session_memory
        self._agent_profiles = agent_profiles
        self._runner_factory = runner_factory
        self._max_active_runs = max_active_runs
        self._max_running_agents = max_running_agents
        self._runtimes: dict[str, AgentRuntimeState] = {}
        self._runners: dict[tuple[str, str], SessionRunner] = {}
        self._runs: dict[tuple[str, str, str], RunState] = {}
        self._idempotency: dict[str, tuple[str, tuple[str, str, str]]] = {}
        self._lock = asyncio.Lock()
        self._state_path = Path(workspace.workspace_dir) / "agent-runtimes.json"

    async def recover(self) -> None:
        """恢复期望状态，但绝不重放可能产生副作用的旧 Run。"""
        payload = await asyncio.to_thread(self._read_state_file)
        agents = payload.get("agents") if isinstance(payload, dict) else {}
        if not isinstance(agents, dict):
            return
        for agent_id, item in agents.items():
            if not isinstance(item, dict) or item.get("desired_state") != AgentLifecycleState.RUNNING.value:
                continue
            if len([state for state in self._runtimes.values() if state.desired_state == AgentLifecycleState.RUNNING]) >= self._max_running_agents:
                break
            profile = self._profile_by_id(agent_id)
            if profile is None:
                continue
            self._runtimes[agent_id] = AgentRuntimeState(
                agent_id=agent_id,
                desired_state=AgentLifecycleState.RUNNING,
                lifecycle_state=AgentLifecycleState.RUNNING,
                recent_session_id=_string_or_none(item.get("recent_session_id")),
            )

    async def shutdown(self) -> None:
        for agent_id in list(self._runtimes):
            await self.stop_agent(agent_id, persist_desired=False)

    async def start_agent(self, agent_id: str) -> AgentRuntimeState:
        async with self._lock:
            profile = self._require_profile(agent_id)
            if profile.kind != "agent":
                raise RuntimeConflict("agent_not_runnable", "subagent 不能直接启动")
            state = self._runtimes.get(agent_id)
            if state and state.desired_state == AgentLifecycleState.RUNNING:
                return state
            running = [item for item in self._runtimes.values() if item.desired_state == AgentLifecycleState.RUNNING]
            if len(running) >= self._max_running_agents:
                raise RuntimeConflict("runtime_capacity_exceeded", "当前运行容量已满")
            state = AgentRuntimeState(agent_id=agent_id, desired_state=AgentLifecycleState.RUNNING, lifecycle_state=AgentLifecycleState.RUNNING)
            self._runtimes[agent_id] = state
            await self._persist_runtime_states()
            return state

    async def stop_agent(self, agent_id: str, *, persist_desired: bool = True) -> AgentRuntimeState:
        async with self._lock:
            self._require_profile(agent_id)
            state = self._runtimes.setdefault(agent_id, AgentRuntimeState(agent_id=agent_id))
            state.lifecycle_state = AgentLifecycleState.STOPPING
            targets = [run for run in self._runs.values() if run.ref.agent_id == agent_id and run.status in _ACTIVE_RUN_STATUSES]
        for run in targets:
            await self.cancel_run(run.ref)
        async with self._lock:
            state = self._runtimes[agent_id]
            state.lifecycle_state = AgentLifecycleState.STOPPED
            if persist_desired:
                state.desired_state = AgentLifecycleState.STOPPED
                await self._persist_runtime_states()
            return state

    async def start_run(self, agent_id: str, request: GatewayInput, session_id: str | None = None, client_request_id: str = "") -> RunState:
        if request.type != GatewayInputType.USER_MESSAGE:
            raise ValueError("仅 user_message 可以创建 Run")
        if not client_request_id:
            raise ValueError("client_request_id 不能为空")
        async with self._lock:
            state = self._runtimes.get(agent_id)
            if state is None or state.lifecycle_state != AgentLifecycleState.RUNNING:
                raise RuntimeConflict("agent_not_running", "Agent 尚未启动")
            profile = self._require_profile(agent_id)
            if len([run for run in self._runs.values() if run.status in _ACTIVE_RUN_STATUSES]) >= self._max_active_runs:
                raise RuntimeConflict("runtime_capacity_exceeded", "当前活动 Run 容量已满")
            target_session_id = session_id or request.session_id
            runner: SessionRunner
            if target_session_id:
                runner = await self._runner_for(agent_id, target_session_id)
                self._assert_session_owner(runner, agent_id)
                provider, model, thinking = self._locked_session_llm(runner, request)
            else:
                provider, model, thinking = self._resolve_new_session_llm(profile, request)
                runner = self._runner_factory.create_runner()
            fingerprint = self._fingerprint(agent_id, target_session_id, profile.revision_id, provider, model, request)
            existing = self._idempotency.get(client_request_id)
            if existing:
                old_fingerprint, old_key = existing
                if old_fingerprint != fingerprint:
                    raise RuntimeConflict("client_request_conflict", "client_request_id 已用于不同请求")
                return self._runs[old_key]
            run_id = f"run_{uuid4().hex}"
            # GatewayInput 的旧校验要求 provider/model 完整；运行时先完成默认值解析再交给旧 Runner。
            request = request.model_copy(update={"session_id": target_session_id, "agent_name": profile.name, "provider": provider, "model": model, "metadata": {**request.metadata, "thinking_value": thinking, "agent_id": agent_id, "revision_id": profile.revision_id, "run_id": run_id}})
            session = await runner.handle_input(request)
            assert session is not None
            key = (agent_id, session.session_id, run_id)
            run = RunState(ref=RunRef(agent_id=agent_id, session_id=session.session_id, run_id=run_id, revision_id=profile.revision_id), client_request_id=client_request_id, status=RunStatus.RUNNING, created_at=utc_now_iso(), started_at=utc_now_iso())
            self._runs[key] = run
            self._idempotency[client_request_id] = (fingerprint, key)
            self._runners[(agent_id, session.session_id)] = runner
            state.recent_session_id = session.session_id
            state.active_run_count += 1
            await self._persist_runtime_states()
            await self._publish_runtime_event(run, "run_started", {"client_request_id": client_request_id})
            asyncio.create_task(self._watch_run(run, runner), name=f"codepilot-run-watch-{run_id}")
            return run

    async def cancel_run(self, ref: RunRef) -> RunState:
        async with self._lock:
            run = self._require_run(ref)
            if run.status not in _ACTIVE_RUN_STATUSES:
                return run
            run.status = RunStatus.CANCELLING
            runner = self._runners[(ref.agent_id, ref.session_id)]
        await runner.handle_input(GatewayInput(type=GatewayInputType.STOP))
        try:
            await asyncio.wait_for(runner.wait_current_run(), timeout=1)
        except TimeoutError:
            # Runner 内部只拥有本 Session 的 task，强制取消不会串扰其他会话。
            task = getattr(runner, "_task", None)
            if task and not task.done():
                task.cancel()
        async with self._lock:
            was_active = run.status in _ACTIVE_RUN_STATUSES
            run.status = RunStatus.CANCELLED
            run.ended_at = utc_now_iso()
            if was_active:
                self._finish_run_counts(run)
            await self._publish_runtime_event(run, "run_cancelled", {})
            return run

    async def reply_interaction(self, ref: RunRef, interaction_id: str, payload: GatewayInput) -> RunState:
        async with self._lock:
            run = self._require_run(ref)
            runner = self._runners.get((ref.agent_id, ref.session_id))
            if runner is None:
                raise RuntimeConflict("interaction_not_found", "交互所属 Run 不存在")
        if payload.type == GatewayInputType.HUMAN_REPLY and payload.approval_id != interaction_id:
            raise RuntimeConflict("interaction_mismatch", "审批 ID 不匹配")
        if payload.type in {GatewayInputType.QUESTION_REPLY, GatewayInputType.QUESTION_DECLINE} and payload.question_id != interaction_id:
            raise RuntimeConflict("interaction_mismatch", "问题 ID 不匹配")
        await runner.handle_input(payload)
        return run

    async def load_session(self, agent_id: str, session_id: str) -> SessionRunner:
        runner = await self._runner_for(agent_id, session_id)
        self._assert_session_owner(runner, agent_id)
        return runner

    def get_agent_state(self, agent_id: str) -> AgentRuntimeState:
        self._require_profile(agent_id)
        return self._runtimes.get(agent_id, AgentRuntimeState(agent_id=agent_id))

    def get_run_state(self, ref: RunRef) -> RunState:
        return self._require_run(ref)

    def list_agent_states(self) -> list[AgentRuntimeState]:
        return [self.get_agent_state(profile.agent_id) for profile in self._agent_profiles.values() if profile.kind == "agent"]

    async def _runner_for(self, agent_id: str, session_id: str) -> SessionRunner:
        existing = self._runners.get((agent_id, session_id))
        if existing:
            return existing
        replay = await self._session_memory.replay(session_id)
        runner = self._runner_factory.create_runner()
        runner.load_session(session_id, replay)
        self._runners[(agent_id, session_id)] = runner
        return runner

    async def _watch_run(self, run: RunState, runner: SessionRunner) -> None:
        try:
            session = await runner.wait_current_run()
            status = RunStatus.COMPLETED
            if session and session.status == SessionStatus.FAILED:
                status = RunStatus.FAILED
            elif session and session.status == SessionStatus.CANCELLED:
                status = RunStatus.CANCELLED
        except asyncio.CancelledError:
            status = RunStatus.CANCELLED
        except Exception:  # AgentLoop 已持久化脱敏错误事件。
            status = RunStatus.FAILED
        async with self._lock:
            was_active = run.status in _ACTIVE_RUN_STATUSES
            if run.status == RunStatus.CANCELLING:
                status = RunStatus.CANCELLED
            run.status = status
            run.ended_at = utc_now_iso()
            if was_active:
                self._finish_run_counts(run)
            await self._publish_runtime_event(run, f"run_{status.value.lower()}", {})

    def _finish_run_counts(self, run: RunState) -> None:
        state = self._runtimes.get(run.ref.agent_id)
        if state:
            state.active_run_count = max(0, state.active_run_count - 1)

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
        activated, selected = resolve_llm_selection(settings=self._config, requested_provider=provider, requested_model=model)
        thinking = resolve_thinking_value(settings=self._config, provider=activated.provider, model=selected, metadata=request.metadata)
        if "thinking_value" not in request.metadata and using_default:
            thinking = profile.default_thinking_value
        return activated.provider, selected, thinking

    def _locked_session_llm(self, runner: SessionRunner, request: GatewayInput) -> tuple[str, str, str | None]:
        snapshot = runner.get_status_snapshot()
        provider, model = snapshot["provider"], snapshot["model"]
        if request.provider and request.model and (request.provider != provider or request.model != model):
            raise RuntimeConflict("session_model_locked", "Session 创建后不能切换 Provider 或 Model")
        if bool(request.provider) != bool(request.model):
            raise ValueError("provider 与 model 必须同时提供")
        return str(provider), str(model), snapshot.get("thinking_value")

    def _require_profile(self, agent_id: str) -> AgentProfile:
        profile = self._profile_by_id(agent_id)
        if profile is None:
            raise KeyError("Agent 不存在")
        return profile

    def _profile_by_id(self, agent_id: str) -> AgentProfile | None:
        return next((item for item in self._agent_profiles.values() if item.agent_id == agent_id), None)

    def _assert_session_owner(self, runner: SessionRunner, agent_id: str) -> None:
        snapshot = runner.get_status_snapshot()
        profile = self._require_profile(agent_id)
        if snapshot.get("agent_name") != profile.name:
            raise RuntimeConflict("session_agent_mismatch", "Session 不属于指定 Agent")

    def _require_run(self, ref: RunRef) -> RunState:
        try:
            run = self._runs[(ref.agent_id, ref.session_id, ref.run_id)]
        except KeyError as exc:
            raise KeyError("Run 不存在") from exc
        if ref.revision_id and run.ref.revision_id != ref.revision_id:
            raise RuntimeConflict("run_revision_mismatch", "Run revision 不匹配")
        return run

    def _fingerprint(self, agent_id: str, session_id: str | None, revision_id: str, provider: str, model: str, request: GatewayInput) -> str:
        attachment_hashes = [hashlib.sha256(item.data_base64.encode()).hexdigest() for item in request.attachments]
        payload = {"agent_id": agent_id, "session_id": session_id, "revision_id": revision_id, "provider": provider, "model": model, "content": hashlib.sha256((request.content or "").encode()).hexdigest(), "attachments": attachment_hashes}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def _publish_runtime_event(self, run: RunState, event_type: str, data: dict[str, Any]) -> None:
        run.run_seq += 1
        await self._event_bus.publish_stream_event(StreamEvent(event_type=event_type, agent_id=run.ref.agent_id, session_id=run.ref.session_id, run_id=run.ref.run_id, run_seq=run.run_seq, created_at=utc_now_iso(), data=data))

    async def _persist_runtime_states(self) -> None:
        payload = {"schema_version": 1, "agents": {agent_id: {"desired_state": state.desired_state.value, "recent_session_id": state.recent_session_id, "updated_at": utc_now_iso()} for agent_id, state in self._runtimes.items() if state.desired_state == AgentLifecycleState.RUNNING}}
        await asyncio.to_thread(self._atomic_write, payload)

    def _read_state_file(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self._state_path)
            os.chmod(self._state_path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)


_ACTIVE_RUN_STATUSES = {RunStatus.STARTING, RunStatus.RUNNING, RunStatus.WAITING_HUMAN, RunStatus.CANCELLING}


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
