from __future__ import annotations

"""定时任务 worker 进程入口。"""

import argparse
import asyncio
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from codepilot.config import WorkspaceState, build_workspace_id, load_settings
from codepilot.gateway import GatewayInput
from codepilot.logging import configure_logging
from codepilot.runtime import build_runtime_bundle
from codepilot.scheduler.models import ScheduleRunStatus
from codepilot.session import SessionStatus


def main() -> None:
    args = parse_args()
    asyncio.run(run_worker(args))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行 CodePilot 定时任务 worker")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--execution-dir", required=True)
    parser.add_argument("--storage-workspace-dir", required=True)
    parser.add_argument("--report-url", required=True)
    parser.add_argument("--report-token-file", required=True)
    return parser.parse_args()


async def run_worker(args: argparse.Namespace) -> None:
    session_id: str | None = None
    try:
        backend_dir = _resolve_backend_dir()
        load_dotenv(dotenv_path=backend_dir / ".env", override=False)
        settings = _build_worker_settings(load_settings(backend_dir / "config.yaml"))
        workspace = _build_worker_workspace(
            execution_dir=Path(args.execution_dir),
            storage_workspace_dir=Path(args.storage_workspace_dir),
        )
        configure_logging(settings.logging, workspace.logs_dir)
        runtime = build_runtime_bundle(settings=settings, workspace=workspace, allow_human_interaction=False)
        _prepare_worker_runtime(runtime)
        prompt = _read_prompt_file(Path(args.prompt_file))
        payload = GatewayInput(
            type="user_message",
            content=prompt,
            agent_name=args.agent_name,
            provider=args.provider,
            model=args.model,
            metadata={
                "source": "schedule",
                "schedule_task_id": args.task_id,
                "schedule_run_id": args.run_id,
                "schedule_task_name": args.task_name,
            },
        )
        session = await runtime.session_runner.handle_input(payload)
        session_id = session.session_id if session else None
        finished = await runtime.session_runner.wait_current_run()
        status = _run_status_from_session(finished.status if finished else None)
        await report(
            args,
            status=status,
            session_id=finished.session_id if finished else session_id,
            summary=_summary_from_session(finished),
            error=None if status == ScheduleRunStatus.COMPLETED else f"session 状态为 {finished.status.value if finished else 'unknown'}",
        )
    except Exception as exc:  # noqa: BLE001
        await report(
            args,
            status=ScheduleRunStatus.FAILED,
            session_id=session_id,
            summary=None,
            error=str(exc),
        )
        raise


async def report(
    args: argparse.Namespace,
    *,
    status: ScheduleRunStatus,
    session_id: str | None,
    summary: str | None,
    error: str | None,
) -> None:
    token = Path(args.report_token_file).read_text(encoding="utf-8").strip()
    payload: dict[str, Any] = {
        "run_id": args.run_id,
        "status": status.value,
        "session_id": session_id,
        "summary": summary,
        "error": error,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(args.report_url, json=payload, headers={"x-codepilot-schedule-token": token})
        response.raise_for_status()


def _build_worker_workspace(*, execution_dir: Path, storage_workspace_dir: Path) -> WorkspaceState:
    execution_path = execution_dir.expanduser().resolve()
    storage_dir = storage_workspace_dir.expanduser().resolve()
    sessions_dir = storage_dir / "sessions"
    logs_dir = storage_dir / "logs"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    workspace_meta_file = storage_dir / "workspace.json"
    codepilot_home = storage_dir.parents[1] if len(storage_dir.parents) >= 2 else storage_dir
    return WorkspaceState(
        workspace_id=build_workspace_id(execution_path),
        workspace_path=execution_path,
        codepilot_home=codepilot_home,
        workspace_dir=storage_dir,
        sessions_dir=sessions_dir,
        logs_dir=logs_dir,
        workspace_meta_file=workspace_meta_file,
    )


def _read_prompt_file(prompt_file: Path) -> str:
    prompt = prompt_file.read_text(encoding="utf-8")
    try:
        prompt_file.unlink(missing_ok=True)
    except OSError:
        # runner 进程退出监控时还会兜底清理；这里优先缩短敏感内容落盘时间。
        pass
    return prompt


def _build_worker_settings(settings: Any) -> Any:
    """worker 使用配置副本，避免无人值守策略影响主进程会话。"""
    return settings.model_copy(deep=True)


def _prepare_worker_runtime(runtime: Any) -> None:
    """定时任务不暴露 question；需要审批的工具由无人值守策略统一阻断。"""
    for name, profile in list(runtime.agent_profiles.items()):
        if "question" not in profile.allowed_tools:
            continue
        runtime.agent_profiles[name] = profile.model_copy(
            update={"allowed_tools": [tool for tool in profile.allowed_tools if tool != "question"]}
        )


def _run_status_from_session(status: SessionStatus | None) -> ScheduleRunStatus:
    if status == SessionStatus.COMPLETED:
        return ScheduleRunStatus.COMPLETED
    if status == SessionStatus.FAILED:
        return ScheduleRunStatus.FAILED
    return ScheduleRunStatus.FAILED


def _summary_from_session(session: Any) -> str | None:
    if session is None:
        return None
    for message in reversed(session.messages):
        info = getattr(message, "info", None)
        if getattr(info, "role", "") != "assistant":
            continue
        texts = [
            str(getattr(part, "text", ""))
            for part in getattr(message, "parts", [])
            if getattr(part, "type", "") == "text" and getattr(part, "text", "")
        ]
        summary = " ".join(text.strip() for text in texts if text.strip())
        if summary:
            return summary[:240]
    return None


def _resolve_backend_dir() -> Path:
    current_file = Path(__file__).resolve()
    backend_dir = current_file.parents[3]
    if backend_dir.name != "backend":
        raise ValueError(f"无法根据源码路径定位 backend 目录: {current_file}")
    return backend_dir


if __name__ == "__main__":
    main()
