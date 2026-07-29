"""事件总线实现。

该模块负责在运行时分发流式事件与领域事件，并为 SSE 等场景提供
可独立消费的事件队列。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pyee.asyncio import AsyncIOEventEmitter

from codepilot.events.definitions import DomainEvent, StreamEvent
from codepilot.logging import get_logger

StreamSubscriber = Callable[[StreamEvent], Awaitable[None] | None]
DomainSubscriber = Callable[[DomainEvent], Awaitable[None] | None]


class EventBus:
    """负责事件订阅、发布与流式队列管理的轻量事件总线。"""

    def __init__(self) -> None:
        """初始化事件发射器、日志器、流式订阅队列与全局序号。"""
        self._emitter = AsyncIOEventEmitter()
        self._logger = get_logger("codepilot.events")
        self._stream_subscribers: set[asyncio.Queue[StreamEvent]] = set()
        self._stream_persist_subscribers: list[StreamSubscriber] = []
        self._domain_subscribers: list[DomainSubscriber] = []
        self._seq = 0

    def subscribe_stream(self, subscriber: StreamSubscriber) -> None:
        """注册流式事件订阅者。"""
        # 持久化订阅者必须在 SSE 可见之前完成，避免客户端拿到未落盘事件。
        self._stream_persist_subscribers.append(subscriber)

    def subscribe_domain(self, subscriber: DomainSubscriber) -> None:
        """注册领域事件订阅者。"""
        self._domain_subscribers.append(subscriber)

    async def publish_stream_event(self, event: StreamEvent) -> StreamEvent:
        """发布流式事件，并同步推送到所有已创建的流式消费队列。"""
        # 流式事件需要严格递增的序号，便于前端按顺序消费与恢复断点。
        self._seq += 1
        event.seq = self._seq

        for subscriber in list(self._stream_persist_subscribers):
            await self._run_subscriber(subscriber, event, "stream")

        # 发布期间订阅关系可能变化，因此基于快照遍历，避免集合迭代失效。
        for queue in list(self._stream_subscribers):
            # 慢消费者不能反压 Agent 执行；溢出后由 SSE 连接主动重连并回放。
            if queue.full():
                self._stream_subscribers.discard(queue)
                continue
            queue.put_nowait(event)
        return event

    async def publish_domain_event(self, event: DomainEvent) -> DomainEvent:
        """发布领域事件。"""
        for subscriber in list(self._domain_subscribers):
            await self._run_subscriber(subscriber, event, "domain")
        return event

    def create_stream_queue(self) -> asyncio.Queue[StreamEvent]:
        """创建并登记一个独立的流式事件消费队列。"""
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=1000)
        self._stream_subscribers.add(queue)
        return queue

    def remove_stream_queue(self, queue: asyncio.Queue[StreamEvent]) -> None:
        """移除不再使用的流式事件消费队列。"""
        self._stream_subscribers.discard(queue)

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
