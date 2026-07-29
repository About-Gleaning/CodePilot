"""CODE-47 多 Agent 运行时验证原型。

本模块刻意不依赖 SessionRunner，用来验证控制面语义与进程边界；正式实现
必须在 ADR 确认后另行进入 CODE-51。
"""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class RunStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    CANCELLING = "CANCELLING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RuntimeCapacityError(RuntimeError):
    pass


class WorkspaceWriteBusy(RuntimeError):
    pass


class InteractionConflict(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class RunRef:
    agent_id: str
    session_id: str
    run_id: str


@dataclass(slots=True)
class RuntimeEvent:
    event_id: str
    agent_id: str
    session_id: str
    run_id: str
    run_seq: int
    event_type: str
    created_at: float
    data: dict[str, Any] = field(default_factory=dict)


class BoundedSubscriber:
    """慢消费者不允许反向阻塞 Agent；溢出后由调用方重连回放。"""

    def __init__(self, max_events: int = 1000) -> None:
        self.queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=max_events)
        self.resync_required = False

    def publish(self, event: RuntimeEvent) -> None:
        if self.resync_required:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.resync_required = True


@dataclass(slots=True)
class _RunState:
    ref: RunRef
    client_request_id: str
    writes_workspace: bool
    status: RunStatus = RunStatus.STARTING
    task: asyncio.Task[None] | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    interaction_id: str | None = None
    interaction_reply: bool | None = None


class InProcessRuntimeProbe:
    """同进程多 Runner 的可运行最小控制面验证。"""

    def __init__(self, *, max_active_runs: int = 5) -> None:
        self.max_active_runs = max_active_runs
        self._runs: dict[str, _RunState] = {}
        self._by_request: dict[str, str] = {}
        self._active_sessions: dict[str, str] = {}
        self._write_lease_owner: str | None = None
        self.events: list[RuntimeEvent] = []
        self.subscribers: list[BoundedSubscriber] = []

    def subscribe(self, max_events: int = 1000) -> BoundedSubscriber:
        subscriber = BoundedSubscriber(max_events)
        self.subscribers.append(subscriber)
        return subscriber

    async def start_run(
        self,
        *,
        agent_id: str,
        session_id: str,
        client_request_id: str,
        writes_workspace: bool = False,
        event_count: int = 3,
        delay_seconds: float = 0.001,
        require_interaction: bool = False,
    ) -> RunRef:
        existing = self._by_request.get(client_request_id)
        if existing:
            return self._runs[existing].ref
        if session_id in self._active_sessions:
            raise RuntimeCapacityError("SessionAlreadyRunning")
        if len(self._active_sessions) >= self.max_active_runs:
            raise RuntimeCapacityError("RunCapacityExceeded; Retry-After=1")
        if writes_workspace and self._write_lease_owner:
            raise WorkspaceWriteBusy("WorkspaceWriteBusy")
        run_id = f"run_{uuid4().hex}"
        state = _RunState(
            ref=RunRef(agent_id=agent_id, session_id=session_id, run_id=run_id),
            client_request_id=client_request_id,
            writes_workspace=writes_workspace,
        )
        self._runs[run_id] = state
        self._by_request[client_request_id] = run_id
        self._active_sessions[session_id] = run_id
        if writes_workspace:
            self._write_lease_owner = run_id
        state.task = asyncio.create_task(
            self._execute(state, event_count, delay_seconds, require_interaction),
            name=f"runtime-probe-{run_id}",
        )
        return state.ref

    async def cancel_run(self, run_id: str) -> None:
        state = self._runs[run_id]
        if state.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return
        state.status = RunStatus.CANCELLING
        state.cancel.set()
        if state.task:
            await state.task

    async def close_agent(self, agent_id: str) -> None:
        await asyncio.gather(*(self.cancel_run(run_id) for run_id, state in list(self._runs.items()) if state.ref.agent_id == agent_id))

    async def resolve_interaction(self, run_id: str, interaction_id: str, approved: bool) -> None:
        state = self._runs[run_id]
        if state.interaction_id != interaction_id:
            raise InteractionConflict("StaleInteraction")
        if state.interaction_reply is not None and state.interaction_reply != approved:
            raise InteractionConflict("InteractionConflict")
        state.interaction_reply = approved

    async def wait(self, run_id: str) -> RunStatus:
        state = self._runs[run_id]
        if state.task:
            await state.task
        return state.status

    def _publish(self, state: _RunState, event_type: str, data: dict[str, Any] | None = None) -> None:
        sequence = sum(1 for event in self.events if event.run_id == state.ref.run_id) + 1
        event = RuntimeEvent(
            event_id=f"evt_{uuid4().hex}",
            agent_id=state.ref.agent_id,
            session_id=state.ref.session_id,
            run_id=state.ref.run_id,
            run_seq=sequence,
            event_type=event_type,
            created_at=time.monotonic(),
            data=data or {},
        )
        self.events.append(event)
        for subscriber in self.subscribers:
            subscriber.publish(event)

    async def _execute(self, state: _RunState, event_count: int, delay_seconds: float, require_interaction: bool) -> None:
        state.status = RunStatus.RUNNING
        self._publish(state, "run_started")
        try:
            for index in range(event_count):
                if state.cancel.is_set():
                    state.status = RunStatus.CANCELLED
                    self._publish(state, "run_cancelled")
                    return
                self._publish(state, "llm_delta", {"index": index})
                await asyncio.sleep(delay_seconds)
            if require_interaction:
                state.status = RunStatus.WAITING_HUMAN
                state.interaction_id = f"interaction_{uuid4().hex}"
                self._publish(state, "human_approval_required", {"interaction_id": state.interaction_id})
                while state.interaction_reply is None and not state.cancel.is_set():
                    await asyncio.sleep(delay_seconds)
                if state.cancel.is_set():
                    state.status = RunStatus.CANCELLED
                    self._publish(state, "run_cancelled")
                    return
                self._publish(state, "human_approval_resolved", {"approved": state.interaction_reply})
            state.status = RunStatus.COMPLETED
            self._publish(state, "run_completed")
        finally:
            self._active_sessions.pop(state.ref.session_id, None)
            if self._write_lease_owner == state.ref.run_id:
                self._write_lease_owner = None


def worker_protocol_main(commands: Any, events: Any) -> None:
    """worker 拓扑的最小真实子进程探针；stdout 不参与协议。"""
    events.put({"type": "ready", "protocol": 1})
    while True:
        command = commands.get()
        kind = command.get("type")
        if kind == "stop":
            events.put({"type": "stopped"})
            return
        if kind == "ping":
            events.put({"type": "pong", "request_id": command["request_id"]})
        if kind == "crash":
            raise RuntimeError("synthetic worker crash")


class WorkerProtocolProbe:
    """验证子进程启动、受限消息和崩溃隔离，不是生产 worker。"""

    def __init__(self) -> None:
        self._context = multiprocessing.get_context("spawn")
        self.commands = self._context.Queue()
        self.events = self._context.Queue()
        self.process = self._context.Process(target=worker_protocol_main, args=(self.commands, self.events))

    def start(self) -> dict[str, Any]:
        self.process.start()
        return self.events.get(timeout=5)

    def ping(self, request_id: str) -> dict[str, Any]:
        self.commands.put({"type": "ping", "request_id": request_id})
        return self.events.get(timeout=5)

    def stop(self) -> None:
        if self.process.is_alive():
            self.commands.put({"type": "stop"})
            self.events.get(timeout=5)
        self.process.join(timeout=5)

    def crash(self) -> None:
        self.commands.put({"type": "crash"})
        self.process.join(timeout=5)

    @property
    def alive(self) -> bool:
        return self.process.is_alive()
