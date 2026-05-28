from __future__ import annotations

"""CodePilot 后端应用入口。

负责创建 FastAPI 实例，并串联配置加载、工作区初始化、事件持久化、
工具注册与 Agent 运行时装配，最终对外暴露可启动的 `app` 对象。
"""

import json
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from codepilot.api import build_api_router
from codepilot.config import AppSettings, WorkspaceState, build_workspace_id, load_settings
from codepilot.events import EventBus
from codepilot.hooks import (
    AgentPluginHook,
    ApprovalDemoHook,
    CommandPluginHook,
    HookManager,
    HookType,
    HttpPluginHook,
    PromptPluginHook,
)
from codepilot.llm import LiteLLMClient
from codepilot.logging import configure_logging
from codepilot.memory import JsonlEventStore, JsonlSessionMemory
from codepilot.session import AgentLoop, SessionRunner, build_agent_profiles
from codepilot.tools import (
    EchoTool,
    EditFileTool,
    McpToolAdapter,
    ReadFileTool,
    ToolDispatcher,
    ToolRegistry,
    WriteFileTool,
    WritePlanTool,
)


@dataclass(slots=True)
class AppContext:
    # 聚合应用启动后需要长期复用的核心运行时对象，避免在路由层重复创建依赖。
    settings: AppSettings
    workspace: WorkspaceState
    event_bus: EventBus
    event_store: JsonlEventStore
    session_memory: JsonlSessionMemory
    tool_registry: ToolRegistry
    hook_manager: HookManager
    llm_client: LiteLLMClient
    agent_profiles: dict[str, object]
    session_runner: SessionRunner


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
    configure_logging(settings.logging, workspace.logs_dir)
    event_bus = EventBus()
    session_memory = JsonlSessionMemory(workspace.sessions_dir)
    event_store = JsonlEventStore(workspace.sessions_dir)

    # 用持久化事件序号恢复事件总线状态，防止服务重启后流式事件序号回退。
    event_bus.set_initial_seq(event_store.latest_seq())
    event_bus.subscribe_domain(session_memory.handle_domain_event)
    event_bus.subscribe_stream(event_store.append)

    tool_registry = ToolRegistry()
    # 一期先注册内置工具与 MCP 占位适配器，后续再替换为真实 MCP 工具发现流程。
    tool_registry.register(EchoTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(ReadFileTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(WriteFileTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(EditFileTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(WritePlanTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(McpToolAdapter(name="mcp_placeholder_tool"))

    hook_manager = _build_hook_manager(settings)
    llm_client = LiteLLMClient()
    tool_dispatcher = ToolDispatcher(tool_registry, hook_manager)
    agent_profiles = build_agent_profiles(settings.agent.max_loop_iterations)
    # AgentLoop 负责单轮推理与工具调用；SessionRunner 负责面向会话编排整个执行生命周期。
    agent_loop = AgentLoop(llm_client=llm_client, tool_registry=tool_registry, tool_dispatcher=tool_dispatcher, hook_manager=hook_manager)
    session_runner = SessionRunner(
        workspace=workspace,
        config=settings,
        event_bus=event_bus,
        hook_manager=hook_manager,
        agent_loop=agent_loop,
        agent_profiles=agent_profiles,
    )

    app_state = AppContext(
        settings=settings,
        workspace=workspace,
        event_bus=event_bus,
        event_store=event_store,
        session_memory=session_memory,
        tool_registry=tool_registry,
        hook_manager=hook_manager,
        llm_client=llm_client,
        agent_profiles=agent_profiles,
        session_runner=session_runner,
    )

    app = FastAPI(title="CodePilot", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 统一把运行时上下文挂到 app.state，供路由层按需访问，而不是重复创建依赖对象。
    app.state.context = app_state
    app.include_router(build_api_router(app_state))

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await app_state.session_runner.shutdown()

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


def _build_hook_manager(settings: AppSettings) -> HookManager:
    """根据配置注册内置 Hook 与插件 Hook，生成运行期统一使用的 Hook 管理器。"""
    manager = HookManager()
    # 内置审批演示 Hook 始终注册，便于在没有外部插件时验证 Hook 链路是否正常。
    manager.register(
        ApprovalDemoHook(
            hook_id="approval-demo-hook",
            hook_type=HookType.LOOP_BEFORE,
            name="approval_demo_hook",
            description="使用 [[approve]] 标记触发审批演示。",
            order=10,
        )
    )
    for plugin in settings.hooks.plugins:
        # 按配置中的插件类型映射到具体 Hook 实现，保持配置驱动，避免在其他位置分散判断逻辑。
        if plugin.plugin_type == "prompt":
            manager.register(
                PromptPluginHook(
                    hook_id=plugin.hook_id,
                    hook_type=HookType(plugin.hook_type),
                    name=plugin.hook_id,
                    enabled=plugin.enabled,
                    order=plugin.order,
                    role=plugin.config.get("role", "system"),
                    content=plugin.config.get("content", ""),
                )
            )
        elif plugin.plugin_type == "command":
            manager.register(
                CommandPluginHook(
                    hook_id=plugin.hook_id,
                    hook_type=HookType(plugin.hook_type),
                    name=plugin.hook_id,
                    enabled=plugin.enabled,
                    order=plugin.order,
                    config=plugin.config,
                )
            )
        elif plugin.plugin_type == "http":
            manager.register(
                HttpPluginHook(
                    hook_id=plugin.hook_id,
                    hook_type=HookType(plugin.hook_type),
                    name=plugin.hook_id,
                    enabled=plugin.enabled,
                    order=plugin.order,
                    config=plugin.config,
                )
            )
        elif plugin.plugin_type == "agent":
            manager.register(
                AgentPluginHook(
                    hook_id=plugin.hook_id,
                    hook_type=HookType(plugin.hook_type),
                    name=plugin.hook_id,
                    enabled=plugin.enabled,
                    order=plugin.order,
                    config=plugin.config,
                )
            )
    return manager


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
