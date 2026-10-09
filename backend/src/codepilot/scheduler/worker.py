from __future__ import annotations

"""定时任务 worker 进程入口。"""

import argparse
import asyncio
import signal
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from codepilot.auth import AuthStore
from codepilot.config import WorkspaceState, build_workspace_id, load_settings
from codepilot.gateway import GatewayInput
from codepilot.events import RunEventScope
from codepilot.logging import configure_logging
from codepilot.runtime import build_runtime_bundle
from codepilot.scheduler.models import ScheduleRunStatus
from codepilot.session import SessionStatus
from codepilot.session.agents import AgentProfile
from codepilot.session.state import RunRef


def main() -> None:
    args = parse_args()
    asyncio.run(run_worker(args))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行 CodePilot 定时任务 worker")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--execution-bundle-file", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--revision-id", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--execution-dir", required=True)
    parser.add_argument("--storage-workspace-dir", required=True)
    parser.add_argument("--report-url", required=True)
    parser.add_argument("--report-token-file", required=True)
    return parser.parse_args()


async def run_worker(args: argparse.Namespace) -> None:
    session_id: str | None = args.session_id
    runtime: Any | None = None
    progress_task: asyncio.Task | None = None
    current_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, current_task.cancel)
    try:
        backend_dir = _resolve_backend_dir()
        load_dotenv(dotenv_path=backend_dir / ".env", override=False)
        settings = _build_worker_settings(load_settings(backend_dir / "config.yaml"))
        workspace = _build_worker_workspace(
            execution_dir=Path(args.execution_dir),
            storage_workspace_dir=Path(args.storage_workspace_dir),
        )
        user_workspace = workspace.for_user(args.user_id)
        configure_logging(settings.logging, user_workspace.logs_dir)
        auth_store = AuthStore(
            workspace.codepilot_home / "auth.sqlite3",
            idle_timeout_seconds=settings.auth.idle_timeout_seconds,
            absolute_timeout_seconds=settings.auth.absolute_timeout_seconds,
        )
        principal = auth_store.find_enabled_user(args.user_id)
        if principal is None:
            raise ValueError("定时任务用户已禁用或不存在")
        runtime = build_runtime_bundle(
            settings=settings,
            workspace=workspace,
            allow_manual_approval=False,
            allow_question_interaction=False,
            principal_resolver=lambda user_id: principal if user_id == principal.user_id else auth_store.find_enabled_user(user_id),
        )
        await runtime.start()
        from codepilot.scheduler.remote_tool import WorkerScheduleTool
        runtime.tool_registry.register(WorkerScheduleTool(args, settings))
        from codepilot.session.agent_config import MultiUserAgentConfigService
        provider = MultiUserAgentConfigService(
            workspace_path=workspace.workspace_path,
            settings=settings, shared_root=workspace.codepilot_home / "agents" / "shared",
            users_root=workspace.codepilot_home / "users", builtin_profiles=runtime.agent_profiles,
            tool_registry=runtime.tool_registry, mcp_manager=runtime.mcp_manager,
        )
        runtime.tool_registry.get("task").set_profile_provider(provider)
        _prepare_worker_runtime(runtime)
        prompt, profile = _read_execution_bundle(Path(args.execution_bundle_file))
        stored = provider.get_profile_revision_snapshot(args.user_id, profile.agent_id, profile.revision_id, resolve_resources=False)
        excluded = {"resolved_connection_versions", "resolved_hook_versions", "resolved_platform_hook_versions", "resolved_tool_versions"}
        def comparable(value):
            result = value.model_dump(exclude=excluded)
            if value.publication_id:
                # 公共配置由发布记录校验，用户连接仍在下方逐项验证归属和有效性。
                result.pop("connection_ids", None)
                result.pop("working_directory", None)
                result["publication_children"] = {key: comparable(AgentProfile.model_validate(child)) for key, child in value.publication_children.items()}
            return result
        if comparable(stored) != comparable(profile):
            raise ValueError("执行包与 Agent revision 不一致")
        profile = provider.resolve_execution_profile(args.user_id, profile)
        profile = _prepare_worker_profile(profile)
        if profile.kind != "agent" or profile.agent_id != args.agent_id or profile.revision_id != args.revision_id:
            raise ValueError("定时任务 Agent 不存在或已归档")
        progress = {"phase": "starting", "iteration": 0}

        async def observe(event):
            if event.run_id != args.run_id or event.data.get("agent_kind") == "subagent":
                return
            if event.event_type == "loop_iteration_started":
                progress["iteration"] = int(event.data.get("iteration") or 0)
                progress["phase"] = "model"
            elif event.event_type == "tool_call_started":
                progress["phase"] = "tool"
            elif event.event_type == "tool_call_finished":
                progress["phase"] = "decision"
        runtime.event_bus.subscribe_stream(observe)

        async def publish_progress():
            while True:
                with suppress(httpx.HTTPError, OSError):
                    await report(args, status=ScheduleRunStatus.RUNNING, session_id=session_id,
                                 summary=None, error=None, **progress)
                await asyncio.sleep(3)
        progress_task = asyncio.create_task(publish_progress())
        run_ref = RunRef(
            user_id=args.user_id,
            agent_id=profile.agent_id,
            session_id=session_id,
            run_id=args.run_id,
            revision_id=profile.revision_id,
        )
        payload = GatewayInput(
            type="user_message",
            session_id=session_id,
            content=prompt,
            agent_name=profile.name,
            provider=args.provider,
            model=args.model,
            metadata={
                "source": "schedule",
                "schedule_task_id": args.task_id,
                "schedule_run_id": args.run_id,
                "schedule_task_name": args.task_name,
            },
        )
        handle = await runtime.agent_backend.load_session(
            args.user_id,
            args.agent_id,
            session_id,
            None,
            profile,
        )
        session = await handle.runner.start_resource_run(
            payload,
            run_ref=run_ref,
            profile=profile,
            event_scope=RunEventScope(runtime.event_bus, run_ref),
        )
        session_id = session.session_id if session else None
        finished = await handle.runner.wait_current_run()
        status = _run_status_from_session(finished.status if finished else None)
        await report(
            args,
            status=status,
            session_id=finished.session_id if finished else session_id,
            summary=_summary_from_session(finished),
            error=None if status == ScheduleRunStatus.COMPLETED else (
                "已停止：达到轮数上限" if finished and finished.stop_reason == "max_iterations"
                else "Hook 要求人工确认，定时执行不支持人工等待。" if finished and finished.stop_reason == "hook_requires_human"
                else f"session 状态为 {finished.status.value if finished else 'unknown'}"
            ),
            stop_reason=finished.stop_reason if finished else None,
            phase="finished", iteration=progress["iteration"],
        )
    except asyncio.CancelledError:
        # SIGTERM 先取消模型和工具并回收其子进程，再向主进程报告；不重放未知副作用。
        if runtime is not None:
            await runtime.agent_backend.shutdown()
        with suppress(httpx.HTTPError, OSError):
            await report(args, status=ScheduleRunStatus.CANCELLED, session_id=session_id,
                         summary=None, error="本轮执行已停止，已发出的外部操作可能仍然生效。", stop_reason="user_cancelled")
    except Exception as exc:  # noqa: BLE001
        await report(
            args,
            status=ScheduleRunStatus.FAILED,
            session_id=session_id,
            summary=None,
            error="定时执行失败，请检查 Agent 依赖、连接及服务运行环境。",
        )
        raise
    finally:
        if progress_task is not None:
            progress_task.cancel()
            with suppress(asyncio.CancelledError):
                await progress_task
        if runtime is not None:
            await runtime.agent_backend.shutdown()
            await runtime.session_runner.shutdown()
            await runtime.shutdown()
        loop.remove_signal_handler(signal.SIGTERM)


async def report(
    args: argparse.Namespace,
    *,
    status: ScheduleRunStatus,
    session_id: str | None,
    summary: str | None,
    error: str | None,
    phase: str = "",
    iteration: int = 0,
    stop_reason: str | None = None,
) -> None:
    token = Path(args.report_token_file).read_text(encoding="utf-8").strip()
    if not hasattr(args, "report_lock"):
        args.report_lock = asyncio.Lock()
        args.report_seq = 0
    async with args.report_lock:
        args.report_seq += 1
        payload: dict[str, Any] = {
        "run_id": args.run_id,
        "status": status.value,
        "session_id": session_id,
        "summary": None,
        "error": error,
        "user_id": args.user_id, "agent_id": args.agent_id, "revision_id": args.revision_id,
        "report_seq": args.report_seq, "phase": phase, "iteration": iteration, "stop_reason": stop_reason,
        }
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            try:
                response = await client.post(args.report_url, json=payload, headers={"x-codepilot-schedule-token": token})
                response.raise_for_status()
            except httpx.HTTPError:
                # 终态事实已经写入会话；父进程在退出后对账，不因上报失败改写或重跑任务。
                if status == ScheduleRunStatus.RUNNING:
                    raise


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


def _read_execution_bundle(bundle_file: Path) -> tuple[str, AgentProfile]:
    import json

    payload = json.loads(bundle_file.read_text(encoding="utf-8"))
    try:
        bundle_file.unlink(missing_ok=True)
    except OSError:
        # runner 进程退出监控时还会兜底清理；这里优先缩短敏感内容落盘时间。
        pass
    if payload.get("schema_version") not in {1, 2, 3, 4, 5}:
        raise ValueError("定时任务执行包版本无效")
    profile = dict(payload.get("profile") or {})
    # 旧执行包只丢弃已废弃的 Skill 锁定字段，不恢复旧内容。
    profile.pop("resolved_skill_versions", None)
    profile.pop("resource_snapshot_version", None)
    return str(payload.get("prompt") or ""), AgentProfile.model_validate(profile)


def _build_worker_settings(settings: Any) -> Any:
    """worker 使用配置副本，避免无人值守策略影响主进程会话。"""
    return settings.model_copy(deep=True)


def _prepare_worker_runtime(runtime: Any) -> None:
    """定时任务不暴露 question；普通工具审批由会话无人值守策略自动通过。"""
    for name, profile in list(runtime.agent_profiles.items()):
        if "question" not in profile.allowed_tools:
            continue
        runtime.agent_profiles[name] = profile.model_copy(
            update={"allowed_tools": [tool for tool in profile.allowed_tools if tool != "question"]}
        )


def _prepare_worker_profile(profile: AgentProfile) -> AgentProfile:
    """执行包是独立快照，必须在交给 SessionRunner 前移除无人值守工具。"""
    return profile.model_copy(
        update={"allowed_tools": [tool for tool in profile.allowed_tools if tool != "question"]},
        deep=True,
    )


def _run_status_from_session(status: SessionStatus | None) -> ScheduleRunStatus:
    if status == SessionStatus.COMPLETED:
        return ScheduleRunStatus.COMPLETED
    if status == SessionStatus.FAILED:
        return ScheduleRunStatus.FAILED
    if status == SessionStatus.CANCELLED:
        return ScheduleRunStatus.CANCELLED
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
