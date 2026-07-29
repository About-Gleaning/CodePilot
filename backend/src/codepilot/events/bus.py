"""事件总线实现。

该模块负责在运行时分发流式事件与领域事件，并为 SSE 等场景提供
可独立消费的事件队列。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any

from pyee.asyncio import AsyncIOEventEmitter

from codepilot.events.definitions import DomainEvent, StreamEvent
from codepilot.logging import get_logger

StreamSubscriber = Callable[[StreamEvent], Awaitable[None] | None]
DomainSubscriber = Callable[[DomainEvent], Awaitable[None] | None]


@dataclass(eq=False, slots=True)
class StreamSubscription:
    """有界 SSE 订阅；溢出后显式要求客户端重连回放。"""

    queue: asyncio.Queue[StreamEvent] = field(default_factory=lambda: asyncio.Queue(maxsize=1000))
    resync_required: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False


class RunEventScope:
    """为一个 Run 的全部流式和领域事件补齐稳定归属。"""

    def __init__(self, parent_bus: Any, run_ref: Any, *, initial_run_seq: int = 0) -> None:
        self._parent_bus = parent_bus
        self.run_ref = run_ref
        self.run_seq = initial_run_seq

    async def publish_stream_event(self, event: StreamEvent) -> StreamEvent:
        self.run_seq += 1
        event.agent_id = self.run_ref.agent_id
        event.session_id = self.run_ref.session_id
        event.run_id = self.run_ref.run_id
        event.run_seq = self.run_seq
        return await self._parent_bus.publish_stream_event(event)

    async def publish_domain_event(self, event: DomainEvent) -> DomainEvent:
        event.agent_id = self.run_ref.agent_id
        event.session_id = self.run_ref.session_id
        event.run_id = self.run_ref.run_id
        event.revision_id = self.run_ref.revision_id
        return await self._parent_bus.publish_domain_event(event)


class EventBus:
    """负责事件订阅、发布与流式队列管理的轻量事件总线。"""

    def __init__(self) -> None:
        """初始化事件发射器、日志器、流式订阅队列与全局序号。"""
        self._emitter = AsyncIOEventEmitter()
        self._logger = get_logger("codepilot.events")
        self._stream_subscribers: set[StreamSubscription] = set()
        self._legacy_subscriptions: dict[asyncio.Queue[StreamEvent], StreamSubscription] = {}
        self._stream_persist_subscribers: list[StreamSubscriber] = []
        self._domain_persist_subscribers: list[DomainSubscriber] = []
        self._domain_subscribers: list[DomainSubscriber] = []
        self._seq = 0

    def subscribe_stream(self, subscriber: StreamSubscriber) -> None:
        """注册流式事件订阅者。"""
        # 持久化订阅者必须在 SSE 可见之前完成，避免客户端拿到未落盘事件。
        self._stream_persist_subscribers.append(subscriber)

    def subscribe_domain(self, subscriber: DomainSubscriber, *, critical: bool = False) -> None:
        """注册领域事件订阅者。"""
        if critical:
            self._domain_persist_subscribers.append(subscriber)
        else:
            self._domain_subscribers.append(subscriber)

    async def publish_stream_event(self, event: StreamEvent) -> StreamEvent:
        """发布流式事件，并同步推送到所有已创建的流式消费队列。"""
        # 流式事件需要严格递增的序号，便于前端按顺序消费与恢复断点。
        self._seq += 1
        event.seq = self._seq

        for subscriber in list(self._stream_persist_subscribers):
            result = subscriber(event)
            if asyncio.iscoroutine(result):
                await result

        # 发布期间订阅关系可能变化，因此基于快照遍历，避免集合迭代失效。
        for subscription in list(self._stream_subscribers):
            # 慢消费者不能反压 Agent 执行；溢出后由 SSE 连接主动重连并回放。
            if subscription.queue.full():
                subscription.closed = True
                subscription.resync_required.set()
                self._stream_subscribers.discard(subscription)
                continue
            subscription.queue.put_nowait(event)
        return event

    async def publish_domain_event(self, event: DomainEvent) -> DomainEvent:
        """发布领域事件。"""
        for subscriber in list(self._domain_persist_subscribers):
            result = subscriber(event)
            if asyncio.iscoroutine(result):
                await result
        for subscriber in list(self._domain_subscribers):
            await self._run_subscriber(subscriber, event, "domain")
        return event

    def create_stream_queue(self) -> asyncio.Queue[StreamEvent]:
        """兼容旧接口；新 SSE 应使用 create_stream_subscription。"""
        subscription = self.create_stream_subscription()
        self._legacy_subscriptions[subscription.queue] = subscription
        return subscription.queue

    def create_stream_subscription(self) -> StreamSubscription:
        subscription = StreamSubscription()
        self._stream_subscribers.add(subscription)
        return subscription

    def remove_stream_queue(self, queue: asyncio.Queue[StreamEvent]) -> None:
        """移除不再使用的流式事件消费队列。"""
        subscription = self._legacy_subscriptions.pop(queue, None)
        if subscription is not None:
            subscription.closed = True
            self._stream_subscribers.discard(subscription)

    def remove_stream_subscription(self, subscription: StreamSubscription) -> None:
        subscription.closed = True
        self._stream_subscribers.discard(subscription)

    def current_seq(self) -> int:
        """返回当前已分配的最新流式事件序号。"""
        return self._seq

    def set_initial_seq(self, seq: int) -> None:
        """基于外部状态初始化序号，并保证序号不会回退。"""
        self._seq = max(self._seq, seq)

    def _safe_wrapper(self, subscriber: Callable[..., Awaitable[None] | None], channel: str) -> Callable[..., Any]:
        """为订阅者增加异常隔离，避免单个订阅者影响总线分发。"""

        async def _runner(event: Any) -> None:
            """执行订阅者，并兼容同步与异步两种回调形式。"""
            await self._run_subscriber(subscriber, event, channel)

        return _runner

    async def _run_subscriber(self, subscriber: Callable[..., Awaitable[None] | None], event: Any, channel: str) -> None:
        """执行订阅者并隔离异常，保证单个订阅失败不影响后续分发。"""
        try:
            result = subscriber(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("event subscriber failed", channel=channel, error=str(exc))
