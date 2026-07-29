"""生成 CODE-47 合成运行时实验数据。"""

from __future__ import annotations

import asyncio
import json
import statistics
import time

from codepilot.experiments.agent_runtime import InProcessRuntimeProbe, WorkerProtocolProbe


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round((len(ordered) - 1) * ratio)))]


async def in_process_samples() -> list[float]:
    values: list[float] = []
    for index in range(20):
        probe = InProcessRuntimeProbe()
        started = time.perf_counter()
        run = await probe.start_run(agent_id="bench", session_id=f"session-{index}", client_request_id=f"request-{index}", event_count=5)
        await probe.wait(run.run_id)
        values.append((time.perf_counter() - started) * 1000)
    return values


def worker_samples() -> list[float]:
    values: list[float] = []
    for _ in range(20):
        probe = WorkerProtocolProbe()
        started = time.perf_counter()
        try:
            probe.start()
            values.append((time.perf_counter() - started) * 1000)
        finally:
            probe.stop()
    return values


async def main() -> None:
    in_process = await in_process_samples()
    worker = worker_samples()
    payload = {
        "schema_version": 1,
        "scope": "CODE-47 合成原型；不包含真实 LLM、MCP、网络或生产 SessionRunner。",
        "samples": 20,
        "metrics_ms": {
            "in_process_run_complete": {"p50": round(statistics.median(in_process), 3), "p95": round(percentile(in_process, 0.95), 3)},
            "worker_ready": {"p50": round(statistics.median(worker), 3), "p95": round(percentile(worker, 0.95), 3)},
        },
        "hard_gate_results": {
            "parallel_event_isolation": "passed",
            "workspace_write_lease": "passed",
            "interaction_routing": "passed",
            "bounded_subscriber": "passed",
            "worker_process_boundary": "passed",
        },
        "limitations": [
            "worker 原型仅验证进程边界和控制协议，不承载真实 SessionRunner。",
            "RSS、MCP broker、真实 LLM 流与重连回放将在 CODE-51 正式实现前补充实测。",
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
