from __future__ import annotations

"""CodePilot 后端应用入口。

负责创建 FastAPI 实例，并串联配置加载、工作区初始化、事件持久化、
工具注册与 Agent 运行时装配，最终对外暴露可启动的 `app` 对象。
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from dotenv import load_dotenv

from codepilot.api import build_api_router
from codepilot.api.security import LocalAccessMiddleware
from codepilot.api.security import AuthenticationMiddleware
from codepilot.auth import AuthService, AuthStore
from codepilot.config import AppSettings, WorkspaceState, build_workspace_id, load_settings
from codepilot.events import EventBus
from codepilot.hooks import HookManager
from codepilot.llm import LiteLLMClient
from codepilot.logging import configure_logging
from codepilot.memory import UserPartitionedEventStore, UserPartitionedSessionMemory
from codepilot.migration import MultiUserMigration
from codepilot.runtime import build_hook_manager as _build_hook_manager
from codepilot.runtime import build_runtime_bundle
from codepilot.scheduler import UserScheduleCoordinator
from codepilot.skills import SkillRegistry
from codepilot.session import SessionRunner
from codepilot.session.agent_runtime import AgentRuntimeManager
from codepilot.session.agent_config import MultiUserAgentConfigService
from codepilot.tools import ScheduleManageTool, ToolRegistry
from codepilot.tools import McpClientManager


@dataclass(slots=True)
class AppContext:
    # 聚合应用启动后需要长期复用的核心运行时对象，避免在路由层重复创建依赖。
    settings: AppSettings
    workspace: WorkspaceState
    event_bus: EventBus
    event_store: UserPartitionedEventStore
    session_memory: UserPartitionedSessionMemory
    tool_registry: ToolRegistry
    hook_manager: HookManager
    llm_client: LiteLLMClient
    skill_registry: SkillRegistry
    agent_profiles: dict[str, object]
    session_runner: SessionRunner
    agent_runtime: AgentRuntimeManager
    schedule_coordinator: UserScheduleCoordinator
    agent_config_service: MultiUserAgentConfigService
    mcp_manager: McpClientManager
    auth_store: AuthStore
    auth_service: AuthService
    migration_required: bool


def create_app() -> FastAPI:
    """完成后端应用的整体装配，并返回可直接启动的 FastAPI 实例。"""
    # 启动时始终从源码位置反推仓库根目录，确保配置加载不依赖当前工作目录。
    repo_root = _resolve_repo_root()
    backend_dir = repo_root / "backend"
    _load_backend_env(backend_dir / ".env")
    settings = load_settings(backend_dir / "config.yaml")
    if not settings.llm_runtime.activated_providers:
        raise ValueError("没有检测到已激活的 LLM 厂商，请检查 backend/.env 中的密钥配置")

    # 先完成本地工作区初始化，再装配日志、会话与运行时组件，避免后续组件缺少目录依赖。
    workspace = _build_workspace_state(repo_root, settings)
    auth_store = AuthStore(
        workspace.codepilot_home / "auth.sqlite3",
        idle_timeout_seconds=settings.auth.idle_timeout_seconds,
        absolute_timeout_seconds=settings.auth.absolute_timeout_seconds,
    )
    auth_service = AuthService(auth_store)
    migration = MultiUserMigration(codepilot_home=workspace.codepilot_home, workspace_dir=workspace.workspace_dir)
    configure_logging(settings.logging, workspace.logs_dir)
    runtime = build_runtime_bundle(
        settings=settings,
        workspace=workspace,
        allow_manual_approval=settings.human_in_the_loop.enabled,
        allow_question_interaction=True,
        principal_resolver=auth_store.find_enabled_user,
    )
    schedule_tool = ScheduleManageTool(
        store=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        settings=settings,
        agent_profiles={},
        timeout_seconds=settings.tools.default_timeout_seconds,
    )
    runtime.tool_registry.register(schedule_tool)
    agent_config_service = MultiUserAgentConfigService(
        settings=settings,
        shared_root=workspace.codepilot_home / "agents" / "shared",
        users_root=workspace.codepilot_home / "users",
        builtin_profiles=runtime.agent_profiles,
        tool_registry=runtime.tool_registry,
        mcp_manager=runtime.mcp_manager,
    )
    task_tool = runtime.tool_registry.get("task")
    if task_tool is not None and hasattr(task_tool, "set_profile_provider"):
        task_tool.set_profile_provider(agent_config_service)
    schedule_coordinator = UserScheduleCoordinator(
        settings=settings,
        workspace=workspace,
        profile_provider=agent_config_service,
    )
    schedule_tool._store = schedule_coordinator
    schedule_tool._runner = schedule_coordinator
    agent_runtime = AgentRuntimeManager(
        workspace=workspace,
        config=settings,
        event_bus=runtime.event_bus,
        session_memory=runtime.session_memory,
        profile_provider=agent_config_service,
        backend=runtime.agent_backend,
        max_active_runs=5,
        max_started_agents=settings.agent.max_started_agents,
        max_active_runs_per_user=settings.auth.max_active_runs_per_user,
        max_started_agents_per_user=settings.auth.max_started_agents_per_user,
    )
    runtime.event_bus.subscribe_domain(agent_runtime.handle_domain_event)
    auth_service.add_disable_listener(agent_runtime.disable_user)
    auth_service.add_disable_listener(schedule_coordinator.disable_user)

    app_state = AppContext(
        settings=settings,
        workspace=workspace,
        event_bus=runtime.event_bus,
        event_store=runtime.event_store,
        session_memory=runtime.session_memory,
        tool_registry=runtime.tool_registry,
        hook_manager=runtime.hook_manager,
        llm_client=runtime.llm_client,
        skill_registry=runtime.skill_registry,
        agent_profiles=runtime.agent_profiles,
        session_runner=runtime.session_runner,
        agent_runtime=agent_runtime,
        schedule_coordinator=schedule_coordinator,
        agent_config_service=agent_config_service,
        mcp_manager=runtime.mcp_manager,
        auth_store=auth_store,
        auth_service=auth_service,
        migration_required=migration.required(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for user_id in app_state.event_store.known_user_ids():
            app_state.event_bus.set_initial_seq(app_state.event_store.latest_seq(user_id), user_id)
        await runtime.start()
        enabled_states = await asyncio.to_thread(app_state.auth_store.user_enabled_states)
        enabled_user_ids = {user_id for user_id, enabled in enabled_states.items() if enabled}
        await app_state.agent_runtime.recover(enabled_user_ids)
        if not app_state.migration_required:
            # 迁移完成前不得拉起旧 Schedule，避免服务在只读保护期产生外部副作用。
            await app_state.schedule_coordinator.start(enabled_user_ids)
        await app_state.auth_service.start(enabled_states)
        try:
            yield
        finally:
            await app_state.auth_service.shutdown()
            await app_state.schedule_coordinator.shutdown()
            await app_state.agent_runtime.shutdown()
            await app_state.session_runner.shutdown()
            await runtime.shutdown()

    app = FastAPI(title="CodePilot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(LocalAccessMiddleware)
    app.add_middleware(AuthenticationMiddleware)
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]"]
    if settings.auth.public_origin:
        from urllib.parse import urlsplit
        public_host = urlsplit(settings.auth.public_origin).hostname
        if public_host:
            allowed_hosts.append(public_host)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r"^http://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 统一把运行时上下文挂到 app.state，供路由层按需访问，而不是重复创建依赖对象。
    app.state.context = app_state
    app.include_router(build_api_router(app_state))

    return app


def _load_backend_env(env_path: Path) -> None:
    """从 backend/.env 补充本地环境变量，但不覆盖外部已显式注入的配置。"""
    if not env_path.exists():
        return
    # 只补齐未显式传入的环境变量，避免覆盖部署环境中的更高优先级配置。
    load_dotenv(dotenv_path=env_path, override=False)


def _build_workspace_state(repo_root: Path, settings: AppSettings) -> WorkspaceState:
    """构建当前仓库对应的工作区目录结构，并返回统一的工作区状态对象。"""
    workspace_path = repo_root.resolve()
    workspace_id = build_workspace_id(workspace_path)
    codepilot_home = Path(settings.storage.codepilot_home).expanduser()
    workspace_dir = codepilot_home / "workspace" / workspace_id
    sessions_dir = workspace_dir / "sessions"
    logs_dir = workspace_dir / "logs"
    workspace_meta_file = workspace_dir / "workspace.json"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if not workspace_meta_file.exists():
        # 首次初始化时固化工作区元数据，便于后续定位 sessions/logs 与排查多仓库隔离问题。
        workspace_meta_file.write_text(
            json.dumps(
                {
                    "workspace_id": workspace_id,
                    "workspace_path": str(workspace_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return WorkspaceState(
        workspace_id=workspace_id,
        workspace_path=workspace_path,
        codepilot_home=codepilot_home,
        workspace_dir=workspace_dir,
        sessions_dir=sessions_dir,
        logs_dir=logs_dir,
        workspace_meta_file=workspace_meta_file,
    )


def _resolve_repo_root() -> Path:
    """基于当前源码文件位置稳定反推出仓库根目录，避免启动命令目录影响配置定位。"""
    # 以源码文件位置为锚点定位仓库根目录，避免依赖当前启动目录。
    current_file = Path(__file__).resolve()
    backend_dir = current_file.parents[2]
    repo_root = backend_dir.parent
    if backend_dir.name != "backend":
        raise ValueError(f"无法根据源码路径定位 backend 目录: {current_file}")
    return repo_root


app = create_app()
