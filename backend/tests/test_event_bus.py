from __future__ import annotations

import asyncio

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
