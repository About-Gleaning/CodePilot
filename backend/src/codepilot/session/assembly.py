from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import copy
from dataclasses import replace

from codepilot.session.agent_config import AgentConfigError
from codepilot.session.connections import ConnectionStore
from codepilot.tools import ToolDispatcher, ToolRegistry
from codepilot.tools.mcp import McpClientManager


@asynccontextmanager
async def assemble_loop(loop, profile, workspace, settings, runtime):
    """按执行上下文复制工具路由；私人 MCP 发现结果从不进入平台注册表。"""
    # 每个上下文构造自己的 Hook 表，个人定义不能污染平台注册表。
    if profile.resolved_hook_versions:
        from codepilot.hooks import HookManager
        from codepilot.hooks.store import HookStore
        hook_manager = HookManager()
        for hooks in loop._hook_manager._hooks.values():
            for hook in hooks:
                hook_manager.register(hook.model_copy(deep=True))
        store = HookStore(workspace.codepilot_home)
        for identity, version in profile.resolved_hook_versions.items():
            if identity.startswith("public:"):
                from codepilot.session.publications import split_identity
                publication, resource = split_identity(identity)
                public_store = HookStore(workspace.codepilot_home, publication_id=publication)
                hook = await asyncio.to_thread(public_store.instantiate, workspace.user_id, "personal:" + resource, version)
                hook.hook_id = identity
            else:
                hook = await asyncio.to_thread(store.instantiate, workspace.user_id, identity, version)
            if hasattr(hook, "config") and "argv" in hook.config:
                await asyncio.to_thread(store.check_command, {"plugin_type": "command", "argv": hook.config["argv"]}, workspace.workspace_path)
            hook_manager.register(hook)
        scoped = copy(loop)
        scoped._hook_manager = hook_manager
        scoped._turn_executor = replace(loop._turn_executor, hook_manager=hook_manager,
                                        tool_dispatcher=ToolDispatcher(loop._turn_executor.tool_registry, hook_manager))
        loop = scoped
    selected = profile.connection_ids
    tools = getattr(loop._turn_executor.tool_registry, "_tools", {})
    directory_changed = any(getattr(tool, "mcp_server_name", None) and tool.manager._workspace.workspace_path != workspace.workspace_path for tool in tools.values())
    if selected is None and not profile.working_directory and not directory_changed and not profile.tool_ids:
        yield loop
        return
    store = ConnectionStore(workspace.codepilot_home, settings)
    runtime.explicit_connections = selected is not None
    identities = {}
    for identity in selected or []:
        record, credentials = store.resolve(workspace.user_id, identity, profile.resolved_connection_versions.get(identity))
        target = record["target"]
        if target in identities:
            raise AgentConfigError("同一服务只能选择一个连接身份", code="connection_conflict", status=409)
        identities[target] = (record, credentials)
        if target.startswith("hook:"):
            runtime.hook_connections[target[5:]] = lambda identity=identity, revision=record["revision"]: store.resolve(workspace.user_id, identity, revision)
    registry = ToolRegistry()
    for tool in loop._turn_executor.tool_registry._tools.values():
        if not getattr(tool, "mcp_server_name", None) and not getattr(tool, "code_tool_id", None):
            registry.register(tool)
    from codepilot.tools.code_store import ToolStore
    from codepilot.tools.python_code_tool import PythonCodeTool
    tool_store = ToolStore(workspace.codepilot_home)
    for identity in profile.tool_ids:
        resolver = None
        if "tool:" + identity in identities:
            connection, _ = identities["tool:" + identity]
            resolver = lambda cid=connection["connection_id"], rev=connection["revision"]: store.resolve(workspace.user_id, cid, rev)
        tool = PythonCodeTool(tool_store, workspace.user_id, identity, profile.resolved_tool_versions[identity], resolver)
        registry.register(tool)
    servers, credentials, guards = {}, {}, {}
    for permission in profile.allowed_tools:
        if not permission.startswith("mcp:"):
            continue
        name = permission[4:]
        config = settings.mcp.servers.get(name)
        if config is None or not config.enabled:
            raise AgentConfigError("MCP 服务待配置", code="mcp_unavailable", status=409)
        if selected is not None:
            if permission not in identities:
                raise AgentConfigError("MCP 服务尚未选择连接身份", code="connection_missing", status=409)
            record, secret = identities[permission]
            identity, revision = record["connection_id"], record["revision"]
            if not identity.startswith("team:"):
                credentials[name] = secret
            guards[name] = lambda identity=identity, revision=revision: store.resolve(workspace.user_id, identity, revision)
        servers[name] = config
    manager = McpClientManager(settings.mcp.model_copy(update={"servers": servers}), workspace, registry,
                               credentials=credentials, guards=guards)
    try:
        await manager.start()
        if any(item["status"] != "available" for item in manager.list_server_capabilities()):
            raise AgentConfigError("MCP 服务暂时不可用", code="mcp_temporarily_unavailable", status=503)
        scoped = copy(loop)
        scoped._turn_executor = replace(loop._turn_executor, tool_registry=registry,
                                         tool_dispatcher=ToolDispatcher(registry, loop._turn_executor.hook_manager))
        yield scoped
    finally:
        await manager.shutdown()
