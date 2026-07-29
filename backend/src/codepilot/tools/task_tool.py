from __future__ import annotations

import asyncio
from typing import Any

from codepilot.hooks import RuntimeHandles
from codepilot.session.agents import AgentProfile
from codepilot.session.message import Message
from codepilot.session.state import SessionStatus
from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import FileToolError, build_tool_failure, build_tool_success, load_tool_description


_AVAILABLE_SUBAGENTS_PLACEHOLDER = "{{available_subagents}}"


class TaskTool(BaseTool):
    def __init__(
        self,
        *,
        agent_loop: Any,
        agent_profiles: dict[str, AgentProfile],
        timeout_seconds: int,
    ) -> None:
        self._agent_loop = agent_loop
        self._agent_profiles = agent_profiles
        self.spec = ToolSpec(
            name="task",
            description=load_tool_description("task"),
            input_schema={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "目标 subagent 名称，必须从工具描述的可用 subagent 列表中选择。"},
                    "task": {"type": "string", "description": "交给 subagent 的自包含任务描述。"},
                },
                "required": ["agent", "task"],
            },
            can_parallel=False,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
            side_effect="runtime_mutation",
        )

    def get_llm_description(self, *, agent_name: str | None = None, agent_readonly: bool | None = None) -> str:
        available_subagents = self._available_subagents_description()
        if _AVAILABLE_SUBAGENTS_PLACEHOLDER in self.spec.description:
            return self.spec.description.replace(_AVAILABLE_SUBAGENTS_PLACEHOLDER, available_subagents)
        return f"{self.spec.description}\n\n可用 subagent：\n{available_subagents}"

    def _available_subagents_description(self) -> str:
        subagent_lines = [
            f"- {profile.name}: {profile.description or '未提供用途说明。'}"
            for profile in self._agent_profiles.values()
            if profile.kind == "subagent"
        ]
        if not subagent_lines:
            return "当前没有可用 subagent。"
        return "\n".join(subagent_lines)

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            if context is None:
                raise FileToolError("task 缺少运行上下文。", error_type="ToolContextMissing")
            if context.runtime is None or context.config is None or not context.tool_call_id:
                raise FileToolError("task 缺少调度运行时。", error_type="TaskRuntimeMissing")
            if getattr(context.agent, "kind", "agent") != "agent" or not getattr(context.agent, "can_call_subagent", False):
                raise FileToolError("当前 Agent 不允许分派 subagent。", error_type="TaskAgentForbidden")

            target_name = str(args.get("agent") or "").strip()
            task_text = str(args.get("task") or "").strip()
            if not target_name or not task_text:
                raise FileToolError("agent 和 task 均不能为空。", error_type="TaskInputInvalid")

            target_profile = self._agent_profiles.get(target_name)
            if target_profile is None:
                raise FileToolError(f"subagent 不存在：{target_name}", error_type="TaskTargetNotFound")
            if target_profile.kind != "subagent":
                raise FileToolError(f"task 只能调用 subagent，不能调用：{target_name}", error_type="TaskTargetForbidden")

            scoped_runtime = RuntimeHandles(
                event_bus=_SubagentEventBus(
                    parent_bus=context.runtime.event_bus,
                    agent=target_profile.name,
                    parent_call_id=context.tool_call_id,
                )
            )
            child_session = await self._agent_loop.run_subagent(
                parent_session=context.session,
                workspace=context.workspace,
                agent_profile=target_profile,
                task=task_text,
                parent_call_id=context.tool_call_id,
                runtime=scoped_runtime,
                config=context.config,
                stop_event=context.stop_event or asyncio.Event(),
            )
            if child_session.status != SessionStatus.COMPLETED:
                raise FileToolError(
                    _subagent_failure_message(target_profile.name, child_session),
                    error_type=_subagent_failure_type(child_session.status),
                )
            summary = _last_assistant_text(child_session.messages)
            return build_tool_success(
                self.spec.name,
                agent=target_profile.name,
                agent_kind=target_profile.kind,
                context_id=child_session.metadata.get("agent_context_id"),
                parent_call_id=context.tool_call_id,
                summary=summary,
                output=summary,
                transcript=_compact_transcript(child_session.messages),
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)


class _SubagentEventBus:
    def __init__(self, *, parent_bus: Any, agent: str, parent_call_id: str) -> None:
        self._parent_bus = parent_bus
        self._agent = agent
        self._parent_call_id = parent_call_id
        self._context_id: str | None = None

    async def publish_stream_event(self, event: Any) -> Any:
        data = dict(event.data or {})
        self._context_id = str(data.get("context_id") or self._context_id or "")
        event.data = {
            "agent": data.get("agent") or self._agent,
            "agent_kind": data.get("agent_kind") or "subagent",
            "context_id": data.get("context_id") or self._context_id,
            "parent_call_id": data.get("parent_call_id") or self._parent_call_id,
            **data,
        }
        return await self._parent_bus.publish_stream_event(event)

    async def publish_domain_event(self, event: Any) -> Any:
        return await self._parent_bus.publish_domain_event(event)


def _last_assistant_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.info.role == "assistant":
            text = message.text_content().strip()
            if text:
                return text
    return "subagent 未返回文本结果。"


def _subagent_failure_type(status: SessionStatus) -> str:
    if status == SessionStatus.CANCELLED:
        return "TaskSubagentCancelled"
    return "TaskSubagentFailed"


def _subagent_failure_message(agent: str, child_session: Any) -> str:
    detail = str(child_session.metadata.get("subagent_error") or "").strip()
    status = getattr(child_session.status, "value", str(child_session.status))
    if detail:
        return f"subagent {agent} 执行失败：{detail}"
    return f"subagent {agent} 执行失败，状态：{status}"


def _compact_transcript(messages: list[Message]) -> list[dict[str, str | None]]:
    transcript: list[dict[str, str | None]] = []
    for message in messages:
        transcript.append(
            {
                "role": message.info.role,
                "agent": getattr(message.info, "agent", None),
                "agent_kind": message.info.agent_kind,
                "context_id": message.info.context_id,
                "parent_call_id": message.info.parent_call_id,
                "text": message.text_content()[:4000],
            }
        )
    return transcript
