from __future__ import annotations

import asyncio
from typing import Any

from dataclasses import dataclass, field

from codepilot.hooks import HookContext, HookManager, HookType, RuntimeHandles
from codepilot.logging import get_logger
from codepilot.session import AgentState, ApprovalRequest, PendingApproval, PendingQuestion, QuestionRequest, SessionState
from codepilot.session.message import ToolPart
from codepilot.tools.base import BaseTool, ToolExecutionContext
from codepilot.tools.registry import ToolRegistry
from codepilot.tools.results import ToolEventPublisher, ToolResultBuilder
from codepilot.utils import utc_now_iso


@dataclass(slots=True)
class ToolExecutionBatch:
    tool_parts: list[ToolPart] = field(default_factory=list)
    pending_approval: PendingApproval | None = None
    pending_question: PendingQuestion | None = None


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry, hook_manager: HookManager) -> None:
        self._registry = registry
        self._hook_manager = hook_manager
        self._logger = get_logger("codepilot.tools")
        self._result_builder = ToolResultBuilder()
        self._event_publisher = ToolEventPublisher()

    async def execute_tool_calls(
        self,
        session: SessionState,
        workspace: Any,
        agent: AgentState,
        tool_calls: list[dict[str, Any]],
        runtime: RuntimeHandles,
        config: Any,
        stop_event: Any | None = None,
    ) -> ToolExecutionBatch:
        result_parts: list[ToolPart] = []
        for group in self._group_tool_calls(tool_calls):
            if any(not item["spec"].can_parallel for item in group):
                for item in group:
                    part, approval, question = await self._execute_one(
                        session, workspace, agent, item, runtime, config, stop_event=stop_event
                    )
                    if approval:
                        return ToolExecutionBatch(
                            tool_parts=result_parts,
                            pending_approval=PendingApproval(request=approval, source="tool", resume_item=item),
                        )
                    if question:
                        return ToolExecutionBatch(
                            tool_parts=result_parts,
                            pending_question=PendingQuestion(request=question, source="tool", resume_item=item),
                        )
                    result_parts.append(part)
            else:
                group_results = await asyncio.gather(
                    *[
                        self._execute_one(session, workspace, agent, item, runtime, config, stop_event=stop_event)
                        for item in group
                    ]
                )
                for item, (part, approval, question) in zip(group, group_results, strict=False):
                    if approval:
                        pending_item = next(call for call in group if call["tool_name"] == approval.action.get("tool_name"))
                        return ToolExecutionBatch(
                            tool_parts=result_parts,
                            pending_approval=PendingApproval(request=approval, source="tool", resume_item=pending_item),
                        )
                    if question:
                        return ToolExecutionBatch(
                            tool_parts=result_parts,
                            pending_question=PendingQuestion(request=question, source="tool", resume_item=item),
                        )
                    result_parts.append(part)
        return ToolExecutionBatch(tool_parts=result_parts)

    async def execute_approved_tool_call(
        self,
        session: SessionState,
        workspace: Any,
        agent: AgentState,
        item: dict[str, Any],
        runtime: RuntimeHandles,
        config: Any,
        stop_event: Any | None = None,
    ) -> ToolPart:
        tool = item.get("tool")
        if tool is None:
            tool = self._registry.get(item["tool_name"])
            item["tool"] = tool
            item["spec"] = tool.spec if tool else None
        part, _, _ = await self._execute_one(
            session, workspace, agent, item, runtime, config, skip_approval=True, stop_event=stop_event
        )
        return part

    def _group_tool_calls(self, tool_calls: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        grouped: list[list[dict[str, Any]]] = []
        current_parallel: list[dict[str, Any]] = []
        for call in tool_calls:
            tool = self._registry.get(call["tool_name"])
            if tool is None:
                call["tool"] = None
                call["spec"] = None
                grouped.append([call])
                continue
            call["tool"] = tool
            call["spec"] = tool.spec
            if tool.spec.can_parallel:
                current_parallel.append(call)
                continue
            if current_parallel:
                grouped.append(current_parallel)
                current_parallel = []
            grouped.append([call])
        if current_parallel:
            grouped.append(current_parallel)
        return grouped

    async def _execute_one(
        self,
        session: SessionState,
        workspace: Any,
        agent: AgentState,
        item: dict[str, Any],
        runtime: RuntimeHandles,
        config: Any,
        skip_approval: bool = False,
        stop_event: Any | None = None,
    ) -> tuple[ToolPart, ApprovalRequest | None, QuestionRequest | None]:
        tool: BaseTool | None = item.get("tool")
        tool_name = item["tool_name"]
        tool_args = item.get("arguments", {})
        tool_call_id = item.get("tool_call_id") or f"call_{utc_now_iso()}"
        item["tool_call_id"] = tool_call_id

        if tool is None:
            return self._result_builder.completed_part(
                tool_call_id,
                tool_name,
                self._result_builder.missing_tool_result(tool_name),
            ), None, None

        tool_context = ToolExecutionContext(
            session=session,
            workspace=workspace,
            agent=agent,
            runtime=runtime,
            config=config,
            tool_call_id=tool_call_id,
            stop_event=stop_event,
        )
        if not skip_approval:
            preflight = await tool.preflight(tool_args, tool_context)
            if preflight.status == "blocked" and preflight.result is not None:
                return self._result_builder.completed_part(tool_call_id, tool_name, preflight.result, tool_args=tool_args), None, None
            if preflight.status == "requires_approval":
                approval = ApprovalRequest(
                    approval_id=f"approval_tool_{tool_call_id}",
                    reason=preflight.reason or "该工具调用需要人工确认后才能执行",
                    action={"type": "tool_call", "tool_name": tool_name, "args": tool_args},
                    created_at=utc_now_iso(),
                )
                return self._result_builder.pending_part(tool_call_id, tool_name, tool_args), approval, None

        if tool.spec.requires_approval and not skip_approval:
            approval = ApprovalRequest(
                approval_id=f"approval_tool_{tool_call_id}",
                reason="该工具需要人工确认后才能执行",
                action={"type": "tool_call", "tool_name": tool_name, "args": tool_args},
                created_at=utc_now_iso(),
            )
            return self._result_builder.pending_part(tool_call_id, tool_name, tool_args), approval, None

        ctx = HookContext(
            hook_type=HookType.TOOL_BEFORE.value,
            session=session,
            workspace=workspace,
            agent=agent,
            messages=session.messages,
            tool_call={"tool_name": tool_name, "args": tool_args, "tool_call_id": tool_call_id},
            config=config,
            runtime=runtime,
        )
        hook_result = await self._hook_manager.run(HookType.TOOL_BEFORE, ctx)
        if hook_result.requires_human_input and hook_result.human_request:
            return self._result_builder.pending_part(tool_call_id, tool_name, tool_args), hook_result.human_request, None

        await self._event_publisher.publish_started(
            session=session,
            runtime=runtime,
            agent=agent,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_args=tool_args,
        )

        try:
            result = await asyncio.wait_for(
                tool.execute(tool_args, context=tool_context),
                timeout=tool.spec.timeout_seconds,
            )
        except TimeoutError:
            result = self._result_builder.error_result(tool_name, "ToolTimeoutError", "工具执行超时")
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("tool failed", tool_name=tool_name, error=str(exc))
            result = self._result_builder.error_result(tool_name, exc.__class__.__name__, str(exc))

        if result.get("status") == "question_required":
            question = QuestionRequest(
                question_id=str(result.get("question_id") or f"question_tool_{tool_call_id}"),
                questions=[question for question in result.get("questions", []) if isinstance(question, dict)],
                created_at=utc_now_iso(),
            )
            return self._result_builder.pending_part(tool_call_id, tool_name, tool_args), None, question

        after_ctx = HookContext(
            hook_type=HookType.TOOL_AFTER.value,
            session=session,
            workspace=workspace,
            agent=agent,
            messages=session.messages,
            tool_call={"tool_name": tool_name, "args": tool_args, "tool_call_id": tool_call_id},
            tool_result=result,
            config=config,
            runtime=runtime,
        )
        await self._hook_manager.run(HookType.TOOL_AFTER, after_ctx)

        await self._event_publisher.publish_finished(
            session=session,
            runtime=runtime,
            agent=agent,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=result,
        )
        return self._result_builder.completed_part(tool_call_id, tool_name, result, tool_args=tool_args), None, None
