"""插件 Hook 基础类型。

这里放置四类插件 Hook 的基础实现：
- `PromptPluginHook` 用于向会话补充提示消息。
- `CommandPluginHook` 执行受控命令。
- `HttpPluginHook` 调用固定 HTTP 目标。
- `AgentPluginHook` 预留 Agent 类插件入口。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from codepilot.hooks.base import BaseHook
from codepilot.hooks.contracts import HookContext, HookResult
from codepilot.session import Message, TextPart, build_assistant_message_info, build_user_message_info
from codepilot.utils import utc_now_millis


class PromptPluginHook(BaseHook):
    """把配置中的提示内容追加为一条 synthetic 会话消息。"""

    role: str = "system"
    content: str = ""

    async def execute(self, ctx: HookContext) -> HookResult:
        """根据配置角色构造消息，并把它追加到会话历史中。"""
        # 会话历史只允许 user/assistant 两种角色，system 配置统一降级为 user 消息。
        role = "assistant" if self.role == "assistant" else "user"
        message_id = f"msg_{uuid4().hex}"
        message = Message(
            info=(
                build_assistant_message_info(
                    message_id=message_id,
                    session_id=ctx.session.session_id,
                    created_at_ms=utc_now_millis(),
                    parent_id=self._find_latest_user_message_id(ctx),
                    agent=ctx.agent.name,
                    agent_kind=ctx.agent.kind,
                    context_id=ctx.agent.context_id,
                    parent_call_id=ctx.agent.parent_call_id,
                    provider_id=ctx.session.provider,
                    model_id=ctx.session.model,
                    cwd=str(Path.cwd()),
                    root=ctx.session.workspace_path,
                )
                if role == "assistant"
                else build_user_message_info(
                    message_id=message_id,
                    session_id=ctx.session.session_id,
                    created_at_ms=utc_now_millis(),
                    agent=ctx.agent.name,
                    agent_kind=ctx.agent.kind,
                    context_id=ctx.agent.context_id,
                    parent_call_id=ctx.agent.parent_call_id,
                    provider_id=ctx.session.provider,
                    model_id=ctx.session.model,
                )
            ),
            parts=[TextPart(text=self.content, synthetic=True)],
        )
        return HookResult(messages_to_append=[message])

    def _find_latest_user_message_id(self, ctx: HookContext) -> str:
        """查找最新用户消息，作为 synthetic assistant 消息的父节点。"""
        # Hook 追加 assistant 文本时，保持和主执行链相同的父消息关联规则。
        for message in reversed(ctx.session.messages):
            if message.info.role == "user":
                return message.info.id
        raise ValueError("Hook 追加 assistant 消息时缺少用户父消息")


class CommandPluginHook(BaseHook):
    """执行管理员声明的参数数组；不经 shell，也不自动重试。"""

    config: dict[str, Any] = {}
    timeout_seconds: float = 30
    protocol_version: int = 0

    async def execute(self, ctx: HookContext) -> HookResult:
        argv = self.config.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item and "\x00" not in item for item in argv):
            raise ValueError("Command Hook 必须由管理员配置 argv 参数数组")
        from codepilot.hooks.protocol import HookOutcomeUncertain, input_bytes, parse_result
        payload = input_bytes(ctx, self.hook_id) if self.protocol_version else _hook_input(ctx)
        if ctx.runtime and ctx.runtime.run_ref:
            from codepilot.tools.workspace_lease import get_workspace_write_lease_manager
            await get_workspace_write_lease_manager(ctx.workspace.shared_runtime_dir).acquire(ctx.runtime.run_ref)
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(ctx.workspace.workspace_path), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            env={key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}},
        )

        async def exchange() -> bytes:
            async def send() -> None:
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()

            # 分别限制 stdout/stderr；communicate 会在检查上限前读入无限输出。
            async def read(stream) -> bytes:
                output = bytearray()
                while chunk := await stream.read(8192):
                    output.extend(chunk)
                    if len(output) > _HOOK_LIMIT:
                        raise ValueError("Hook 输出超过限制")
                return bytes(output)

            results = await asyncio.gather(send(), read(process.stdout), read(process.stderr))
            if await process.wait() != 0:
                raise ValueError("Command Hook 执行失败")
            return results[1]

        try:
            raw = await asyncio.wait_for(exchange(), timeout=self.timeout_seconds)
            return await parse_result(raw, ctx, self.hook_id) if self.protocol_version else _hook_result(raw)
        except Exception as exc:
            if self.protocol_version:
                raise HookOutcomeUncertain("Command Hook 已发出，执行结果不确定") from exc
            raise
        finally:
            # 即使主进程已经退出，也回收同组子进程，避免停止后留下后台写入。
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


class HttpPluginHook(BaseHook):
    """只向管理员固定地址发送有界上下文，禁止跟随重定向。"""

    config: dict[str, Any] = {}
    timeout_seconds: float = 30
    protocol_version: int = 0

    async def execute(self, ctx: HookContext) -> HookResult:
        url = self.config.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("HTTP Hook 缺少管理员配置的目标")
        headers = {"Content-Type": "application/json"}
        mapping = self.config.get("headers_from_env", {})
        credentials = None
        if ctx.runtime is not None:
            resolver = ctx.runtime.hook_connections.get(self.hook_id)
            if resolver is not None:
                record, secret = await asyncio.to_thread(resolver)
                credentials = None if record["connection_id"].startswith("team:") else secret
            elif (ctx.runtime.explicit_connections or self.protocol_version) and mapping:
                raise ValueError("HTTP Hook 尚未选择连接身份")
        for target, source in mapping.items():
            value = os.environ.get(source) if credentials is None else credentials.get(source)
            if value is None:
                raise ValueError("HTTP Hook 凭证待配置")
            headers[target] = value
        from codepilot.hooks.protocol import HookOutcomeUncertain, input_bytes, parse_result
        payload = input_bytes(ctx, self.hook_id) if self.protocol_version else _hook_input(ctx)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False, trust_env=False) as client:
                async with client.stream("POST", url, content=payload, headers=headers) as response:
                    response.raise_for_status()
                    output = bytearray()
                    async for chunk in response.aiter_bytes():
                        output.extend(chunk)
                        if len(output) > _HOOK_LIMIT:
                            raise ValueError("Hook 输出超过限制")
            return await parse_result(bytes(output), ctx, self.hook_id) if self.protocol_version else _hook_result(bytes(output))
        except Exception as exc:
            if self.protocol_version:
                raise HookOutcomeUncertain("HTTP Hook 已发出，执行结果不确定") from exc
            raise


class AgentPluginHook(BaseHook):
    """预留给 Agent 类插件的 Hook 实现入口。"""

    config: dict[str, Any] = {}

    async def execute(self, ctx: HookContext) -> HookResult:
        raise ValueError("Agent 类型 Hook 尚不支持，请修改配置")


_HOOK_LIMIT = 65536


def _hook_input(ctx: HookContext) -> bytes:
    # 不序列化 config/workspace/runtime，避免把凭证和服务端对象传给插件。
    payload = {"hook_type": ctx.hook_type, "session_id": ctx.session.session_id,
               "parameters": ctx.metadata.get("hook_parameters", {}),
               "agent_id": ctx.agent.agent_id, "context_id": ctx.agent.context_id,
               "run_id": getattr(getattr(ctx.runtime, "run_ref", None), "run_id", None),
               "tool_call": ctx.tool_call, "tool_result": ctx.tool_result}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(raw) > _HOOK_LIMIT:
        raise ValueError("Hook 输入超过限制")
    return raw


def _hook_result(raw: bytes) -> HookResult:
    try:
        return HookResult.model_validate_json(raw, strict=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Hook 返回值不符合 HookResult") from exc
