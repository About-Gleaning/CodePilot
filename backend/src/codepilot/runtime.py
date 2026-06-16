from __future__ import annotations

"""运行时装配公共入口。

主 HTTP 进程和定时任务 worker 都需要构建同一套会话运行时。把装配逻辑放在
这里，避免 worker 为了复用 SessionRunner 而导入 `main.py` 并意外创建 FastAPI app。
"""

from dataclasses import dataclass
from typing import Any

from codepilot.config import AppSettings, WorkspaceState
from codepilot.events import EventBus
from codepilot.hooks import (
    AgentPluginHook,
    ApprovalHook,
    CommandPluginHook,
    HookManager,
    HookType,
    HttpPluginHook,
    PromptPluginHook,
)
from codepilot.llm import LiteLLMClient
from codepilot.memory import JsonlEventStore, JsonlSessionMemory
from codepilot.session import AgentLoop, SessionRunner, build_agent_profiles
from codepilot.session.title import SessionTitleService
from codepilot.skills import SkillRegistry
from codepilot.tools import (
    BashTool,
    EditFileTool,
    LoadSkillTool,
    McpToolAdapter,
    QuestionTool,
    ReadFileTool,
    TaskTool,
    TodoReadTool,
    TodoWriteTool,
    ToolDispatcher,
    ToolRegistry,
    WebFetchTool,
    WriteFileTool,
    WritePlanTool,
)


@dataclass(slots=True)
class RuntimeBundle:
    """应用和 worker 共享的长期运行时对象集合。"""

    event_bus: EventBus
    event_store: JsonlEventStore
    session_memory: JsonlSessionMemory
    tool_registry: ToolRegistry
    hook_manager: HookManager
    llm_client: LiteLLMClient
    agent_profiles: dict[str, Any]
    session_runner: SessionRunner


def build_runtime_bundle(
    settings: AppSettings,
    workspace: WorkspaceState,
    *,
    allow_human_interaction: bool = True,
) -> RuntimeBundle:
    """按指定 workspace 构建一套独立的 Agent 会话运行时。"""
    event_bus = EventBus()
    session_memory = JsonlSessionMemory(workspace.sessions_dir)
    event_store = JsonlEventStore(workspace.sessions_dir)
    event_bus.set_initial_seq(event_store.latest_seq())
    event_bus.subscribe_domain(session_memory.handle_domain_event)
    event_bus.subscribe_stream(event_store.append)

    skill_registry = SkillRegistry(workspace.codepilot_home / "skills")
    skill_registry.discover()

    tool_registry = ToolRegistry()
    tool_registry.register(BashTool(settings=settings.tools.bash, timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(ReadFileTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(WriteFileTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(EditFileTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(WritePlanTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(TodoWriteTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(TodoReadTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(QuestionTool(timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(LoadSkillTool(registry=skill_registry, timeout_seconds=settings.tools.default_timeout_seconds))
    tool_registry.register(WebFetchTool(timeout_seconds=settings.tools.default_timeout_seconds))

    hook_manager = build_hook_manager(settings)
    llm_client = LiteLLMClient(log_requests=settings.llm.log_requests)
    tool_dispatcher = ToolDispatcher(tool_registry, hook_manager)
    agent_profiles = build_agent_profiles(
        max_iterations=settings.agent.max_loop_iterations,
        subagent_max_iterations=settings.agent.subagent_max_loop_iterations,
    )
    agent_loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        tool_dispatcher=tool_dispatcher,
        hook_manager=hook_manager,
        skill_registry=skill_registry,
    )
    tool_registry.register(
        TaskTool(
            agent_loop=agent_loop,
            agent_profiles=agent_profiles,
            timeout_seconds=settings.tools.default_timeout_seconds,
        )
    )
    tool_registry.register(McpToolAdapter(name="mcp_placeholder_tool"))

    session_runner = SessionRunner(
        workspace=workspace,
        config=settings,
        event_bus=event_bus,
        hook_manager=hook_manager,
        agent_loop=agent_loop,
        agent_profiles=agent_profiles,
        title_service=build_title_service(settings),
        allow_human_interaction=allow_human_interaction,
    )
    return RuntimeBundle(
        event_bus=event_bus,
        event_store=event_store,
        session_memory=session_memory,
        tool_registry=tool_registry,
        hook_manager=hook_manager,
        llm_client=llm_client,
        agent_profiles=agent_profiles,
        session_runner=session_runner,
    )


def build_title_service(settings: AppSettings) -> SessionTitleService:
    """从配置解析标题生成模型；标题请求固定不打印 LLM 请求日志。"""
    provider = settings.llm.title_provider
    model = settings.llm.title_model
    activated_provider = settings.llm_runtime.activated_providers.get(provider)
    if activated_provider is None:
        raise ValueError(f"标题生成 provider `{provider}` 未激活或不存在")
    if model not in activated_provider.models:
        raise ValueError(f"标题生成 model `{model}` 不属于 provider `{provider}`")
    return SessionTitleService(
        provider=provider,
        model=model,
        litellm_model_prefix=activated_provider.litellm_model_prefix,
        llm_client=LiteLLMClient(log_requests=False),
    )


def build_hook_manager(settings: AppSettings) -> HookManager:
    """根据配置注册内置 Hook 与插件 Hook，生成运行期统一使用的 Hook 管理器。"""
    manager = HookManager()
    manager.register(
        ApprovalHook(
            hook_id="approval-hook",
            hook_type=HookType.LOOP_BEFORE,
            name="approval_hook",
            description="使用 [[approve]] 标记触发人工审批。",
            order=10,
        )
    )
    for plugin in settings.hooks.plugins:
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
