from __future__ import annotations

import asyncio

import pytest

from codepilot.events import DomainEvent, EventBus
from codepilot.events.definitions import DomainEventType
from codepilot.utils import utc_now_iso


def test_publish_domain_event_waits_for_async_subscriber() -> None:
    async def run_case() -> None:
        bus = EventBus()
        calls: list[str] = []

        async def subscriber(event: DomainEvent) -> None:
            await asyncio.sleep(0)
            calls.append(str(event.session_id))

        bus.subscribe_domain(subscriber)

        await bus.publish_domain_event(
            DomainEvent(
                event_type=DomainEventType.SESSION_META,
                session_id="session_1",
                created_at=utc_now_iso(),
                data={},
            )
        )

        assert calls == ["session_1"]

    asyncio.run(run_case())


@pytest.mark.asyncio
async def test_critical_persistence_failure_propagates_before_stream_delivery() -> None:
    bus = EventBus()

    async def broken(_: DomainEvent) -> None:
        raise OSError("disk unavailable")

    bus.subscribe_domain(broken, critical=True)
    with pytest.raises(OSError, match="disk unavailable"):
        await bus.publish_domain_event(
            DomainEvent(
                event_type=DomainEventType.SESSION_META,
                session_id="session_1",
                created_at=utc_now_iso(),
                data={},
            )
        )
