from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from codepilot.config.settings import McpServerSettings, McpSettings, McpStdioServerSettings
from codepilot.logging import get_logger
from codepilot.session.attachments import AttachmentError, attachment_message_dir, decode_image_attachment
from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec


@dataclass(slots=True)
class _CallRequest:
    tool_name: str
    arguments: dict[str, Any]
    future: asyncio.Future[Any]


@dataclass(slots=True)
class _ServerRuntime:
    name: str
    config: McpServerSettings
    queue: asyncio.Queue[_CallRequest | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=20)
    )
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    error: str | None = None
    discovered_tools: set[str] = field(default_factory=set)


class McpClientManager:
    """维护 MCP 连接，并把远端能力注册为 CodePilot 工具。"""

    def __init__(self, settings: McpSettings, workspace: Any, tool_registry: Any) -> None:
        self._settings = settings
        self._workspace = workspace
        self._tool_registry = tool_registry
        self._servers: dict[str, _ServerRuntime] = {}
        self._logger = get_logger("codepilot.mcp")
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for name, config in self._settings.servers.items():
            if not config.enabled:
                continue
            runtime = _ServerRuntime(name=name, config=config)
            self._servers[name] = runtime
            runtime.task = asyncio.create_task(self._run_server(runtime), name=f"mcp-{name}")
        if self._servers:
            await asyncio.gather(*(runtime.ready.wait() for runtime in self._servers.values()))

    async def shutdown(self) -> None:
        if not self._started:
            return
        for runtime in self._servers.values():
            for _ in range(5):
                await runtime.queue.put(None)
        tasks = [runtime.task for runtime in self._servers.values() if runtime.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._servers.clear()
        self._started = False

    def list_server_capabilities(self) -> list[dict[str, Any]]:
        """提供给配置页的脱敏 MCP 服务目录。"""
        capabilities: list[dict[str, Any]] = []
        for name, config in sorted(self._settings.servers.items()):
            runtime = self._servers.get(name)
            if not config.enabled:
                status = "disabled"
            elif runtime is not None and runtime.error is None and runtime.task is not None and not runtime.task.done():
                status = "available"
            else:
                status = "unavailable"
            capabilities.append({"name": name, "status": status, "requires_approval": config.requires_approval,
                                 "description": "MCP 服务权限按服务整体授予，不展示连接配置或凭证。"})
        return capabilities

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        runtime = self._servers.get(server_name)
        if runtime is None or runtime.error is not None or runtime.task is None or runtime.task.done():
            raise RuntimeError("McpServerUnavailable")
        future = asyncio.get_running_loop().create_future()
        try:
            runtime.queue.put_nowait(_CallRequest(tool_name=tool_name, arguments=arguments, future=future))
        except asyncio.QueueFull as exc:
            raise RuntimeError("McpCapacityExceeded") from exc
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def _run_server(self, runtime: _ServerRuntime) -> None:
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(runtime.config.timeout_seconds):
                    session = await stack.enter_async_context(
                        _open_session(runtime.config, self._workspace.workspace_path)
                    )
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                self._register_tools(runtime, tools)
                runtime.error = None
                runtime.ready.set()
                workers = [
                    asyncio.create_task(
                        self._consume_calls(runtime, session),
                        name=f"mcp-{runtime.name}-call-{index}",
                    )
                    for index in range(5)
                ]
                await asyncio.gather(*workers)
        except Exception as exc:  # noqa: BLE001
            # 连接错误不包含底层地址或凭证；已经发出的调用绝不自动重放。
            runtime.error = exc.__class__.__name__
            self._logger.error(
                "mcp server stopped",
                server=runtime.name,
                error_type=exc.__class__.__name__,
            )
        finally:
            runtime.ready.set()
            while not runtime.queue.empty():
                queued = runtime.queue.get_nowait()
                if queued is not None:
                    _set_future_exception(queued.future, RuntimeError("McpServerUnavailable"))

    async def _consume_calls(self, runtime: _ServerRuntime, session: ClientSession) -> None:
        while True:
            request = await runtime.queue.get()
            if request is None:
                return
            if request.future.cancelled():
                continue
            try:
                async with asyncio.timeout(runtime.config.timeout_seconds):
                    result = await session.call_tool(request.tool_name, arguments=request.arguments)
            except Exception:
                _set_future_exception(request.future, RuntimeError("McpOutcomeUncertain"))
            else:
                _set_future_result(request.future, result)

    def _register_tools(self, runtime: _ServerRuntime, tools: list[Any]) -> None:
        used_names: set[str] = set()
        for remote_tool in tools:
            remote_name = str(remote_tool.name)
            local_name = build_mcp_tool_name(runtime.name, remote_name)
            if local_name in used_names or self._tool_registry.get(local_name) is not None:
                self._logger.error("mcp tool name collision", server=runtime.name, tool=remote_name)
                continue
            used_names.add(local_name)
            runtime.discovered_tools.add(remote_name)
            schema = dict(remote_tool.inputSchema) if isinstance(remote_tool.inputSchema, dict) else {}
            schema.setdefault("type", "object")
            self._tool_registry.register(
                McpToolAdapter(
                    manager=self,
                    server_name=runtime.name,
                    remote_tool_name=remote_name,
                    local_tool_name=local_name,
                    description=str(remote_tool.description or f"MCP 工具 {remote_name}"),
                    input_schema=schema,
                    requires_approval=runtime.config.requires_approval,
                    timeout_seconds=runtime.config.timeout_seconds,
                    max_output_chars=runtime.config.max_output_chars,
                )
            )


class McpToolAdapter(BaseTool):
    def __init__(
        self,
        *,
        manager: McpClientManager,
        server_name: str,
        remote_tool_name: str,
        local_tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        requires_approval: bool,
        timeout_seconds: int,
        max_output_chars: int,
    ) -> None:
        self.manager = manager
        self.mcp_server_name = server_name
        self.remote_tool_name = remote_tool_name
        self.max_output_chars = max_output_chars
        self.spec = ToolSpec(
            name=local_tool_name,
            description=f"来自 MCP server `{server_name}`：{description}",
            input_schema=input_schema,
            can_parallel=False,
            requires_approval=requires_approval,
            timeout_seconds=timeout_seconds,
            side_effect="external_mutation",
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        permission = f"mcp:{self.mcp_server_name}"
        allowed_tools = getattr(getattr(context, "agent", None), "allowed_tools", []) or []
        if permission not in allowed_tools:
            return {
                "status": "error",
                "tool_name": self.spec.name,
                "error_type": "McpPermissionError",
                "error_message": f"当前 Agent 未通过 `{permission}` 授权此 MCP server",
                "recoverable": False,
            }
        try:
            result = await self.manager.call_tool(self.mcp_server_name, self.remote_tool_name, args)
            return _normalize_call_result(
                result,
                tool_name=self.spec.name,
                server_name=self.mcp_server_name,
                context=context,
                max_output_chars=self.max_output_chars,
            )
        except Exception as exc:  # noqa: BLE001
            code = str(exc)
            if code not in {"McpCapacityExceeded", "McpOutcomeUncertain", "McpServerUnavailable"}:
                code = "McpConnectionError"
            return {
                "status": "error",
                "tool_name": self.spec.name,
                "error_type": code,
                "error_message": "MCP 调用未完成",
                "recoverable": code == "McpCapacityExceeded",
            }


@asynccontextmanager
async def _open_session(config: McpServerSettings, workspace_path: Path) -> AsyncIterator[ClientSession]:
    if isinstance(config, McpStdioServerSettings):
        cwd = _resolve_stdio_cwd(config.cwd, workspace_path)
        # stdio MCP 服务通常依赖 PATH、HOME 等运行环境；仅覆盖显式映射的密钥变量。
        env = dict(os.environ)
        env.update(_resolve_env(config.env_from_process))
        parameters = StdioServerParameters(command=config.command, args=config.args, env=env, cwd=cwd)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                yield session
        return

    headers = _resolve_env(config.headers_from_env)
    async with httpx.AsyncClient(headers=headers, timeout=config.timeout_seconds) as http_client:
        async with streamable_http_client(config.url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                yield session


def _resolve_stdio_cwd(raw_cwd: str | None, workspace_path: Path) -> Path:
    root = workspace_path.resolve()
    target = (root / (raw_cwd or ".")).resolve()
    if not target.is_relative_to(root):
        raise ValueError("MCP stdio cwd 必须位于当前 workspace 内")
    return target


def _resolve_env(mapping: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for target_name, source_name in mapping.items():
        value = os.environ.get(source_name)
        if value is None:
            missing.append(source_name)
        else:
            resolved[target_name] = value
    if missing:
        raise ValueError(f"MCP 所需环境变量未设置：{', '.join(sorted(set(missing)))}")
    return resolved


def build_mcp_tool_name(server_name: str, remote_tool_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", remote_tool_name).strip("_") or "tool"
    candidate = f"mcp__{server_name}__{normalized}"
    if candidate == f"mcp__{server_name}__{remote_tool_name}" and len(candidate) <= 64:
        return candidate
    digest = hashlib.sha256(remote_tool_name.encode("utf-8")).hexdigest()[:8]
    prefix_length = max(1, 64 - len(f"mcp__{server_name}____{digest}"))
    return f"mcp__{server_name}__{normalized[:prefix_length]}__{digest}"


def _normalize_call_result(
    result: Any,
    *,
    tool_name: str,
    server_name: str,
    context: ToolExecutionContext | None,
    max_output_chars: int,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    remaining = max_output_chars
    for index, block in enumerate(getattr(result, "content", []) or []):
        block_type = str(getattr(block, "type", "unknown"))
        if block_type == "text":
            text = str(getattr(block, "text", ""))
            clipped = text[:remaining]
            remaining -= len(clipped)
            content.append({"type": "text", "text": clipped, "truncated": len(clipped) < len(text)})
        elif block_type == "image":
            attachment = _persist_image_block(block, index=index, context=context)
            if attachment is not None:
                attachments.append(attachment)
                content.append({"type": "image", "mime": attachment["mime"], "filename": attachment["filename"]})
            else:
                content.append({"type": "image", "mime": getattr(block, "mimeType", None), "omitted": True})
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            if text is not None:
                clipped = str(text)[:remaining]
                remaining -= len(clipped)
                content.append({"type": "resource", "text": clipped, "truncated": len(clipped) < len(str(text))})
            else:
                content.append({"type": "resource", "mime": getattr(resource, "mimeType", None), "omitted": True})
        else:
            content.append({"type": block_type, "mime": getattr(block, "mimeType", None), "omitted": True})
        if remaining <= 0:
            break

    structured = getattr(result, "structuredContent", None)
    structured_payload: Any = structured
    if structured is not None:
        serialized = json.dumps(structured, ensure_ascii=False, default=str)
        if len(serialized) > remaining:
            structured_payload = {"preview": serialized[: max(0, remaining)], "truncated": True}

    is_error = bool(getattr(result, "isError", False))
    payload: dict[str, Any] = {
        "status": "error" if is_error else "ok",
        "tool_name": tool_name,
        "mcp_server": server_name,
        "content": content,
        "structured_content": structured_payload,
    }
    if attachments:
        payload["attachments"] = attachments
    if is_error:
        payload.update(
            {
                "error_type": "McpToolError",
                "error_message": _error_message(content),
                "recoverable": True,
            }
        )
    return payload


def _persist_image_block(block: Any, *, index: int, context: ToolExecutionContext | None) -> dict[str, Any] | None:
    if context is None or context.session is None or context.workspace is None or not context.tool_call_id:
        return None
    mime = str(getattr(block, "mimeType", "application/octet-stream"))
    encoded = str(getattr(block, "data", ""))
    # 与用户图片附件保持相同上限，避免不可信 MCP server 写入超大二进制。
    if len(encoded) > 7_000_000:
        return None
    try:
        decoded, validated_mime = decode_image_attachment(encoded, mime)
    except AttachmentError:
        return None

    extension = mimetypes.guess_extension(validated_mime) or ".bin"
    filename = f"mcp-{index + 1}{extension}"
    # MCP 返回的调用 ID 来自外部协议，必须与用户附件共用受控路径分段，防止路径穿越。
    target_dir = attachment_message_dir(
        context.workspace,
        str(context.session.session_id),
        str(context.tool_call_id),
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    target.write_bytes(decoded)
    return {"type": "image", "mime": validated_mime, "filename": filename, "source_path": str(target)}


def _error_message(content: list[dict[str, Any]]) -> str:
    texts = [str(item.get("text", "")) for item in content if item.get("type") == "text" and item.get("text")]
    return "\n".join(texts) or "MCP 工具返回错误"


def _set_future_result(future: asyncio.Future[Any], result: Any) -> None:
    if not future.done():
        future.set_result(result)


def _set_future_exception(future: asyncio.Future[Any], error: Exception) -> None:
    if not future.done():
        future.set_exception(error)
