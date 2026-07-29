from __future__ import annotations

"""生成 CODE-50 可重复、脱敏的控制面验证结果。"""

import asyncio
import json
import resource
import statistics
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codepilot.events import EventBus, StreamEvent
from codepilot.memory import JsonlSessionMemory
from codepilot.session.agent_runtime import AgentRuntimeManager, RuntimeConflict
from codepilot.session.agents import AgentProfile
from codepilot.utils import utc_now_iso


class _Profiles:
    def __init__(self) -> None:
        self.items = {
            f"agent-{index}": AgentProfile(
                agent_id=f"agent-{index}",
                revision_id=f"revision-{index}",
                name=f"agent_{index}",
                description="验证 Agent",
                system_prompt="validation",
                allowed_tools=["read_file"],
            )
            for index in range(6)
        }

    def get_active_profile_snapshot(self, agent_id: str) -> AgentProfile:
        return self.items[agent_id].model_copy(deep=True)

    def get_record_snapshot(self, agent_id: str) -> dict[str, Any]:
        profile = self.items[agent_id]
        return {"agent_id": agent_id, "name": profile.name, "archived": False, "profile": profile.model_copy(deep=True)}

    def list_active_profile_snapshots(self) -> list[AgentProfile]:
        return [item.model_copy(deep=True) for item in self.items.values()]


class _Backend:
    async def start_agent(self, agent_id: str) -> None:
        return None

    async def stop_agent(self, agent_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


async def _run() -> dict[str, Any]:
    ready_samples: list[float] = []
    control_samples: list[float] = []
    capacity_rejected = False
    rss_before_five = 0
    rss_after_five = 0
    with tempfile.TemporaryDirectory(prefix="codepilot-code50-") as raw_root:
        root = Path(raw_root)
        # 预热 3 轮、正式 20 轮；每轮都以全新运行目录验证 5+1 容量边界。
        for round_index in range(23):
            round_root = root / f"round-{round_index}"
            manager = AgentRuntimeManager(
                workspace=SimpleNamespace(workspace_dir=round_root),
                config=SimpleNamespace(),
                event_bus=EventBus(),
                session_memory=JsonlSessionMemory(round_root / "sessions"),
                profile_provider=_Profiles(),
                backend=_Backend(),  # type: ignore[arg-type]
                max_started_agents=5,
            )
            if round_index == 3:
                rss_before_five = _rss_bytes()
            round_samples: list[float] = []
            for index in range(5):
                started = time.perf_counter()
                await manager.start_agent(f"agent-{index}")
                round_samples.append((time.perf_counter() - started) * 1000)
            if round_index == 3:
                rss_after_five = _rss_bytes()
            try:
                await manager.start_agent("agent-5")
            except RuntimeConflict as exc:
                capacity_rejected = capacity_rejected or exc.code == "started_agent_capacity_exceeded"
            if round_index >= 3:
                ready_samples.extend(round_samples)
            await manager.shutdown()

        bus = EventBus()

        async def persist(_: StreamEvent) -> None:
            return None

        bus.subscribe_stream(persist)
        subscription = bus.create_stream_subscription()
        for index in range(1001):
            started = time.perf_counter()
            await bus.publish_stream_event(
                StreamEvent(event_type="validation", created_at=utc_now_iso(), data={"index": index})
            )
            control_samples.append((time.perf_counter() - started) * 1000)
        bounded_overflow = subscription.resync_required.is_set() and subscription.closed
    rss_delta_bytes = max(0, rss_after_five - rss_before_five)
    return {
        "schema_version": 1,
        "work_item": "CODE-50",
        "generated_at": utc_now_iso(),
        "contains_sensitive_payloads": False,
        "scenarios": [
            {"id": "CODE50-CAPACITY-001", "passed": capacity_rejected, "started_agents": 5},
            {"id": "CODE50-STREAM-001", "passed": bounded_overflow, "queue_limit": 1000},
        ],
        "metrics": {
            "agent_ready_ms": {
                "samples": len(ready_samples),
                "p50": round(statistics.median(ready_samples), 3),
                "p95": round(_p95(ready_samples), 3),
                "threshold_p95": 2000,
            },
            "control_event_route_ms": {
                "samples": len(control_samples),
                "p50": round(statistics.median(control_samples), 3),
                "p95": round(_p95(control_samples), 3),
                "threshold_p95": 50,
            },
            "five_idle_agents_peak_rss_delta_bytes": rss_delta_bytes,
            "five_idle_agents_threshold_bytes": 1024 * 1024 * 1024,
        },
    }


def _rss_bytes() -> int:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    unit = 1 if resource.getpagesize() > 4096 else 1024
    return int(raw) * unit


def main() -> None:
    result = asyncio.run(_run())
    output = Path(__file__).resolve().parents[2] / "docs" / "agent-platform" / "multi-agent-control-plane-validation-results.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
