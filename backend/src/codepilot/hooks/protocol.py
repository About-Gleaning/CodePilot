"""个人 Hook 的公开协议，不向插件暴露内部会话对象。"""

import json
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from codepilot.hooks.contracts import HookError, HookResult
from codepilot.session import ApprovalRequest
from codepilot.utils import utc_now_iso


PREPARATION_STAGES = {"session.before", "loop.before", "llm.before"}
LIMIT = 65536


class HookOutcomeUncertain(ValueError):
    """请求已发出，平台无法证明外部副作用是否发生。"""


class ExternalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol_version: Literal[1]
    action: Literal["continue", "stop", "fail", "human"]
    reason: str = Field(default="", max_length=2000)
    context: str = Field(default="", max_length=16000)


def safe_data(value):
    if isinstance(value, dict):
        return {key: safe_data(item) for key, item in value.items()
                if not re.search(r"secret|password|authorization|cookie|api.?key|token|base64|data_url|headers|credential", key, re.I)}
    if isinstance(value, list):
        return [safe_data(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"data:image/[^\s]+", "[图片已移除]", value)
    return value


def input_bytes(ctx, hook_id: str) -> bytes:
    message = ctx.current_message
    content = "" if message is None else "\n".join(
        part.text for part in message.parts if getattr(part, "type", None) == "text"
    )
    data = {
        "protocol_version": 1, "invocation_id": uuid4().hex, "hook_id": hook_id,
        "stage": ctx.hook_type, "session_id": ctx.session.session_id,
        "run_id": getattr(getattr(ctx.runtime, "run_ref", None), "run_id", None),
        "context_id": ctx.agent.context_id, "iteration": getattr(ctx.runtime, "iteration", 0),
        "parameters": ctx.metadata.get("hook_parameters", {}), "message": content,
        "tool_call": ctx.tool_call, "tool_result": ctx.tool_result,
    }
    raw = json.dumps(safe_data(data), ensure_ascii=False).encode()
    if len(raw) > LIMIT:
        raise ValueError("Hook 输入超过 64 KiB")
    return raw


async def parse_result(raw: bytes, ctx, hook_id: str) -> HookResult:
    from codepilot.hooks.plugins import PromptPluginHook

    value = ExternalResult.model_validate_json(raw)
    if value.context and ctx.hook_type not in PREPARATION_STAGES:
        raise ValueError("此节点不允许补充模型上下文")
    result = HookResult(stop_loop=value.action == "stop", fail_session=value.action == "fail")
    if value.action == "stop":
        result.error = HookError(code="hook_stopped", message="Hook 请求停止")
    if value.context:
        result.messages_to_append = (await PromptPluginHook(
            hook_id=hook_id, hook_type=ctx.hook_type, name=hook_id,
            content=f"[Hook: {hook_id}]\n{value.context}",
        ).execute(ctx)).messages_to_append
    if value.action == "human":
        result.requires_human_input = True
        result.human_request = ApprovalRequest(approval_id=f"hook_{uuid4().hex}",
                                               reason=value.reason or "Hook 请求人工确认", created_at=utc_now_iso())
    if value.action == "fail":
        result.error = HookError(code="hook_failed", message="Hook 返回失败")
    return result
