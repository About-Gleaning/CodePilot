from __future__ import annotations

"""执行 CODE-53 源码发布门禁，并生成不含业务正文的结构化结果。"""

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(BACKEND / "src"))

from codepilot.config import load_settings  # noqa: E402
from codepilot.llm import LiteLLMClient  # noqa: E402
from codepilot.session import LLMState, SessionState, SessionStatus  # noqa: E402

_SECRET_PATTERNS = (
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(rb"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]{32,}"),
)


class _LiveBus:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.first_event = asyncio.Event()
        self.first_event_at: float | None = None
        self.event_count = 0
        self.cross_routed = False

    async def publish_stream_event(self, event: Any) -> Any:
        if event.session_id != self.session_id:
            self.cross_routed = True
        self.event_count += 1
        if self.first_event_at is None:
            self.first_event_at = time.perf_counter()
            self.first_event.set()
        return event


def _run(command: list[str], cwd: Path, *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
    }


def _parse_pytest(output: str) -> dict[str, Any]:
    passed = re.search(r"(\d+) passed", output)
    skipped = re.search(r"(\d+) skipped", output)
    return {
        "passed_count": int(passed.group(1)) if passed else 0,
        "skipped_count": int(skipped.group(1)) if skipped else 0,
    }


def _deterministic_checks(temp_root: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    commands = {
        "uv_lock": (["uv", "lock", "--check"], BACKEND),
        "backend_pytest": (["uv", "run", "pytest", "-q"], BACKEND),
        "frontend_test": (["pnpm", "test", "--run"], FRONTEND),
        "frontend_build": (["pnpm", "build"], FRONTEND),
        "mcp_protocol": (["uv", "run", "pytest", "-q", "tests/test_mcp.py"], BACKEND),
    }
    for name, (command, cwd) in commands.items():
        completed = _run(command, cwd)
        combined = f"{completed.stdout}\n{completed.stderr}"
        item: dict[str, Any] = {"passed": completed.returncode == 0}
        if "pytest" in command:
            item.update(_parse_pytest(combined))
        checks[name] = item
        if completed.returncode != 0:
            item["error_code"] = f"{name}_failed"

    parallel_output = temp_root / "parallel.json"
    completed = _run(
        [
            "uv",
            "run",
            "python",
            "scripts/validate_parallel_agent_runtime.py",
            "--output",
            str(parallel_output),
        ],
        BACKEND,
    )
    parallel = json.loads(parallel_output.read_text(encoding="utf-8")) if completed.returncode == 0 else {}
    checks["parallel_runtime"] = {
        "passed": completed.returncode == 0
        and bool(parallel.get("scenarios", {}).get("event_ordering", {}).get("passed"))
        and bool(parallel.get("scenarios", {}).get("workspace_write_lease", {}).get("passed")),
        "metrics_ms": parallel.get("metrics_ms", {}),
    }
    return checks


def _dependency_audits(temp_root: Path) -> dict[str, Any]:
    requirements = temp_root / "requirements.txt"
    exported = _run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        BACKEND,
    )
    python_result: dict[str, Any] = {"passed": False, "vulnerability_count": None}
    if exported.returncode == 0:
        audited = _run(
            ["uv", "run", "pip-audit", "-r", str(requirements), "--format", "json", "--disable-pip"],
            BACKEND,
        )
        try:
            payload = json.loads(audited.stdout or "[]")
            dependencies = payload.get("dependencies", []) if isinstance(payload, dict) else payload
            vulnerabilities = sum(
                len(item.get("vulns") or [])
                for item in dependencies
                if isinstance(item, dict)
            )
            python_result = {
                "passed": audited.returncode == 0 and vulnerabilities == 0,
                "vulnerability_count": vulnerabilities,
            }
            if audited.returncode != 0 and vulnerabilities == 0:
                python_result["error_code"] = "audit_unavailable"
        except json.JSONDecodeError:
            python_result = {"passed": False, "vulnerability_count": None, "error_code": "audit_unavailable"}
    else:
        python_result["error_code"] = "lock_export_failed"

    audited = _run(
        [
            "pnpm",
            "audit",
            "--prod",
            "--audit-level",
            "high",
            "--json",
            "--registry=https://registry.npmjs.org",
        ],
        FRONTEND,
    )
    frontend_result: dict[str, Any]
    try:
        payload = json.loads(audited.stdout or "{}")
        metadata = payload.get("metadata") if isinstance(payload, dict) else {}
        counts = metadata.get("vulnerabilities") if isinstance(metadata, dict) else {}
        high = int((counts or {}).get("high") or 0)
        critical = int((counts or {}).get("critical") or 0)
        frontend_result = {
            "passed": audited.returncode == 0 and high == 0 and critical == 0,
            "high": high,
            "critical": critical,
        }
        if audited.returncode != 0 and high == 0 and critical == 0:
            frontend_result["error_code"] = "audit_unavailable"
    except (json.JSONDecodeError, TypeError, ValueError):
        frontend_result = {"passed": False, "high": None, "critical": None, "error_code": "audit_unavailable"}
    return {"python": python_result, "frontend": frontend_result}


async def _live_deepseek(provider: str, model: str) -> dict[str, Any]:
    load_dotenv(BACKEND / ".env", override=False)
    settings = load_settings(BACKEND / "config.yaml")
    activated = settings.llm_runtime.activated_providers.get(provider)
    if activated is None or model not in activated.models:
        return {"passed": False, "error_code": "external_provider_unavailable"}
    state = LLMState(
        provider=provider,
        model=model,
        max_tokens=64,
        metadata={"litellm_model_prefix": activated.litellm_model_prefix},
    )
    client = LiteLLMClient(log_requests=False)

    async def call(marker: str) -> tuple[float, float, bool, bool]:
        session_id = f"release_{marker.lower().replace('-', '_')}"
        session = SessionState(
            session_id=session_id,
            workspace_id="release_validation",
            workspace_path="<TEMP_WORKSPACE>",
            agent_name="release_probe",
            provider=provider,
            model=model,
            status=SessionStatus.RUNNING,
            created_at="2026-07-31T00:00:00Z",
            updated_at="2026-07-31T00:00:00Z",
        )
        bus = _LiveBus(session_id)
        started = time.perf_counter()
        result = await asyncio.wait_for(
            client.stream_chat(
                session=session,
                llm_state=state,
                provider_messages=[
                    {"role": "system", "content": "Follow the user instruction exactly."},
                    {"role": "user", "content": f"Reply with only this marker: {marker}"},
                ],
                tools=[],
                event_bus=bus,
            ),
            timeout=45,
        )
        ended = time.perf_counter()
        first_ms = ((bus.first_event_at or ended) - started) * 1000
        return first_ms, (ended - started) * 1000, marker in result.text, not bus.cross_routed

    try:
        await call("RELEASE-WARMUP")
        first_samples: list[float] = []
        completion_samples: list[float] = []
        marker_checks: list[bool] = []
        routing_checks: list[bool] = []
        for batch in range(3):
            rows = await asyncio.gather(*(call(f"RELEASE-{batch}-{index}") for index in range(5)))
            for first, completion, marker_ok, routing_ok in rows:
                first_samples.append(first)
                completion_samples.append(completion)
                marker_checks.append(marker_ok)
                routing_checks.append(routing_ok)

        cancel_session = SessionState(
            session_id="release_cancel",
            workspace_id="release_validation",
            workspace_path="<TEMP_WORKSPACE>",
            agent_name="release_probe",
            provider=provider,
            model=model,
            status=SessionStatus.RUNNING,
            created_at="2026-07-31T00:00:00Z",
            updated_at="2026-07-31T00:00:00Z",
        )
        cancel_bus = _LiveBus(cancel_session.session_id)
        cancel_task = asyncio.create_task(
            client.stream_chat(
                session=cancel_session,
                llm_state=state,
                provider_messages=[{"role": "user", "content": "Count upward with one number per line until stopped."}],
                tools=[],
                event_bus=cancel_bus,
            )
        )
        await asyncio.wait_for(cancel_bus.first_event.wait(), timeout=45)
        cancel_started = time.perf_counter()
        cancel_task.cancel()
        try:
            await asyncio.wait_for(cancel_task, timeout=10)
        except asyncio.CancelledError:
            cancelled = True
        else:
            cancelled = False
        cancel_ms = (time.perf_counter() - cancel_started) * 1000
    except Exception:
        return {"passed": False, "error_code": "external_provider_unavailable"}

    return {
        "passed": all(marker_checks) and all(routing_checks) and cancelled and cancel_ms <= 10_000,
        "provider": provider,
        "model": model,
        "warmup_calls": 1,
        "measured_calls": 15,
        "cancel_calls": 1,
        "first_event_ms": _summary(first_samples),
        "completion_ms": _summary(completion_samples),
        "cancel_ms": round(cancel_ms, 3),
        "zero_cross_run_routing": all(routing_checks),
        "marker_match_count": sum(marker_checks),
    }


def _sensitive_scan(result: dict[str, Any]) -> dict[str, Any]:
    home = str(Path.home()).encode()
    findings: list[str] = []
    tracked = _run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], ROOT)
    for raw_name in tracked.stdout.split("\0"):
        if not raw_name:
            continue
        path = ROOT / raw_name
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        data = path.read_bytes()
        if home and home in data:
            findings.append("absolute_user_path")
        if any(pattern.search(data) for pattern in _SECRET_PATTERNS):
            findings.append("credential_pattern")
    encoded_result = json.dumps(result, ensure_ascii=False).encode()
    if home and home in encoded_result:
        findings.append("result_absolute_user_path")
    if any(pattern.search(encoded_result) for pattern in _SECRET_PATTERNS):
        findings.append("result_credential_pattern")
    return {"passed": not findings, "finding_codes": sorted(set(findings))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["deterministic", "live", "all"], default="deterministic")
    parser.add_argument("--live-provider", default="deepseek")
    parser.add_argument("--live-model", default="deepseek-v4-flash")
    parser.add_argument(
        "--reuse-live-from",
        type=Path,
        help="仅在修复非运行时门禁后复用同一代码轮次已通过的真实模型结果。",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="codepilot-release-") as raw:
        os.environ["CODEPILOT_HOME"] = str(Path(raw) / "home")
        result: dict[str, Any] = {
            "schema_version": 1,
            "work_item": "CODE-53",
            "environment": "isolated-local",
            "deterministic": {},
            "dependency_audits": {},
            "live_deepseek": {"status": "not_run"},
            "external_mcp": {"status": "not_configured"},
        }
        if args.mode in {"deterministic", "all"}:
            result["deterministic"] = _deterministic_checks(Path(raw))
            result["dependency_audits"] = _dependency_audits(Path(raw))
        if args.mode in {"live", "all"}:
            result["live_deepseek"] = asyncio.run(_live_deepseek(args.live_provider, args.live_model))
        elif args.reuse_live_from:
            previous = json.loads(args.reuse_live_from.read_text(encoding="utf-8"))
            live = previous.get("live_deepseek") if isinstance(previous, dict) else None
            if not isinstance(live, dict) or not live.get("passed"):
                raise ValueError("复用的真实模型结果未通过")
            result["live_deepseek"] = {**live, "reused_after_gate_only_changes": True}
        result["sensitive_data_scan"] = _sensitive_scan(result)
        sections = [
            *result["deterministic"].values(),
            *result["dependency_audits"].values(),
            (
                result["live_deepseek"]
                if args.mode in {"live", "all"} or args.reuse_live_from
                else {"passed": True}
            ),
            result["sensitive_data_scan"],
        ]
        result["passed"] = all(bool(item.get("passed")) for item in sections if isinstance(item, dict))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"passed": result["passed"], "output": str(args.output)}, ensure_ascii=False))
        raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
