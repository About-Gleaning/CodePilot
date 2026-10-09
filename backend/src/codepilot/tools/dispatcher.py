from __future__ import annotations

import asyncio
from typing import Any

from dataclasses import dataclass, field

from codepilot.hooks import HookContext, HookManager, HookResult, HookType, RuntimeHandles
from codepilot.logging import get_logger
from codepilot.session import AgentState, ApprovalRequest, PendingApproval, PendingQuestion, QuestionRequest, SessionState
from codepilot.session.message import ToolPart
from codepilot.session.interactions import SessionMessageAppender
from codepilot.tools.base import BaseTool, ToolExecutionContext
from codepilot.tools.registry import ToolRegistry
from codepilot.tools.results import ToolEventPublisher, ToolResultBuilder
from codepilot.tools.workspace_lease import WorkspaceWriteBusy, get_workspace_write_lease_manager
from codepilot.utils import utc_now_iso


@dataclass(slots=True)
class ToolResumeBatch:
    """记录工具批次暂停后的最小恢复上下文。"""

    items: list[dict[str, Any]]
    approved_call_id: str | None = None


@dataclass(slots=True)
class ToolExecutionBatch:
    tool_parts: list[ToolPart] = field(default_factory=list)
    pending_approval: PendingApproval | None = None
    pending_question: PendingQuestion | None = None
    resume_batch: ToolResumeBatch | None = None
    hook_result: HookResult | None = None


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
        return await self._execute_items(session, workspace, agent, tool_calls, runtime, config, stop_event=stop_event)

    async def resume_tool_batch(
        self,
        session: SessionState,
        workspace: Any,
        agent: AgentState,
        resume_batch: ToolResumeBatch,
        runtime: RuntimeHandles,
        config: Any,
        stop_event: Any | None = None,
    ) -> ToolExecutionBatch:
        return await self._execute_items(
            session,
            workspace,
            agent,
            resume_batch.items,
            runtime,
            config,
            approved_call_id=resume_batch.approved_call_id,
            stop_event=stop_event,
        )

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

    async def _execute_items(
        self,
        session: SessionState,
        workspace: Any,
        agent: AgentState,
        tool_calls: list[dict[str, Any]],
        runtime: RuntimeHandles,
        config: Any,
        approved_call_id: str | None = None,
        stop_event: Any | None = None,
    ) -> ToolExecutionBatch:
        result_parts: list[ToolPart] = []
        groups = self._group_tool_calls(tool_calls)
        selected = getattr(agent, "hook_ids", None)
        hooks = [hook for stage in (HookType.TOOL_BEFORE, HookType.TOOL_AFTER)
                 for hook in self._hook_manager.get_hooks(stage)
                 if hook.selectable and (selected is None or hook.hook_id in selected)]
        if any(not hook.applies_to_tools or item["tool_name"] in hook.applies_to_tools
               for hook in hooks for item in tool_calls):
            # 工具本身只读不代表其 Hook 无副作用；命中时保留原调用顺序。
            groups = [[item] for item in tool_calls]
        for group_index, group in enumerate(groups):
            if any(item["spec"] is None or not item["spec"].can_parallel for item in group):
                for item_index, item in enumerate(group):
                    part, approval, question = await self._execute_one(
                        session,
                        workspace,
                        agent,
                        item,
                        runtime,
                        config,
                        skip_approval=item.get("tool_call_id") == approved_call_id,
                        stop_event=stop_event,
                    )
                    if approval:
                        return ToolExecutionBatch(
                            tool_parts=result_parts,
                            pending_approval=PendingApproval(request=approval, source="hook_tool" if approval.action.get("hook_origin") else "tool", resume_item=item),
                            resume_batch=ToolResumeBatch(
                                items=self._remaining_items(groups, group_index, item_index),
                                approved_call_id=item.get("tool_call_id"),
                            ),
                        )
                    if question:
                        return ToolExecutionBatch(
                            tool_parts=result_parts,
                            pending_question=PendingQuestion(request=question, source="tool", resume_item=item),
                            resume_batch=ToolResumeBatch(items=self._remaining_items(groups, group_index, item_index + 1)),
                        )
                    result_parts.append(part)
                    if runtime.tool_hook_control is not None:
                        control = runtime.tool_hook_control
                        runtime.tool_hook_control = None
                        for remaining in self._remaining_items(groups, group_index, item_index + 1):
                            result_parts.append(self._hook_blocked_part(remaining))
                        return ToolExecutionBatch(tool_parts=result_parts, hook_result=control)
            else:
                group_results = await asyncio.gather(
                    *[
                        self._execute_one(
                            session,
                            workspace,
                            agent,
                            item,
                            runtime,
                            config,
                            skip_approval=item.get("tool_call_id") == approved_call_id,
                            stop_event=stop_event,
                        )
                        for item in group
                    ]
                )
                pending_index: int | None = None
                pending_approval: ApprovalRequest | None = None
                pending_question: QuestionRequest | None = None
                unresolved_items: list[dict[str, Any]] = []
                group_parts: list[ToolPart] = []
                for item_index, (item, (part, approval, question)) in enumerate(zip(group, group_results, strict=False)):
                    if approval:
                        if pending_index is None:
                            pending_index = item_index
                            pending_approval = approval
                        unresolved_items.append(item)
                        continue
                    if question:
                        if pending_index is None:
                            pending_index = item_index
                            pending_question = question
                        unresolved_items.append(item)
                        continue
                    group_parts.append(part)
                if len(group_parts) > 1:
                    self._mark_parallel_group(group_parts, group_index)
                result_parts.extend(group_parts)
                if runtime.tool_hook_control is not None:
                    control = runtime.tool_hook_control
                    runtime.tool_hook_control = None
                    for remaining in unresolved_items + self._remaining_items(groups, group_index + 1, 0):
                        result_parts.append(self._hook_blocked_part(remaining))
                    return ToolExecutionBatch(tool_parts=result_parts, hook_result=control)
                if pending_index is not None:
                    remaining_groups = self._remaining_items(groups, group_index + 1, 0)
                    pending_item = group[pending_index]
                    if pending_approval:
                        return ToolExecutionBatch(
                            tool_parts=result_parts,
                            pending_approval=PendingApproval(request=pending_approval, source="hook_tool" if pending_approval.action.get("hook_origin") else "tool", resume_item=pending_item),
                            resume_batch=ToolResumeBatch(
                                items=unresolved_items + remaining_groups,
                                approved_call_id=pending_item.get("tool_call_id"),
                            ),
                        )
                    return ToolExecutionBatch(
                        tool_parts=result_parts,
                        pending_question=PendingQuestion(request=pending_question, source="tool", resume_item=pending_item),
                        resume_batch=ToolResumeBatch(items=[item for item in unresolved_items if item is not pending_item] + remaining_groups),
                    )
        return ToolExecutionBatch(tool_parts=result_parts)

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

    def _mark_parallel_group(self, parts: list[ToolPart], group_index: int) -> None:
        group_id = f"parallel_{group_index + 1}_{parts[0].call_id}"
        for part in parts:
            part.metadata = {**part.metadata, "execution_group": group_id}

    def _remaining_items(self, groups: list[list[dict[str, Any]]], group_index: int, item_index: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index, group in enumerate(groups[group_index:], start=group_index):
            start = item_index if index == group_index else 0
            items.extend(group[start:])
        return items

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
        forbidden_part = self._forbidden_tool_part(tool, agent, tool_call_id, tool_name, tool_args)
        if forbidden_part is not None:
            return forbidden_part, None, None
        tool_context = ToolExecutionContext(
            session=session,
            workspace=workspace,
            agent=agent,
            runtime=runtime,
            config=config,
            tool_call_id=tool_call_id,
            stop_event=stop_event,
            skip_approval=skip_approval,
            run_ref=runtime.run_ref,
            principal=getattr(workspace, "principal", None),
        )
        preflight_part, preflight_approval = await self._run_preflight(
            session, runtime, agent, tool, tool_context, tool_name, tool_args, tool_call_id, skip_approval
        )
        if preflight_part is not None:
            return preflight_part, None, None
        if preflight_approval is not None:
            return self._result_builder.pending_part(tool_call_id, tool_name, tool_args), preflight_approval, None
        approval = self._spec_approval(tool, tool_name, tool_args, tool_call_id, skip_approval, config)
        if approval is not None:
            return self._result_builder.pending_part(tool_call_id, tool_name, tool_args), approval, None

        hook_approval = await self._run_before_hook(
            session, workspace, agent, runtime, config, tool_name, tool_args, tool_call_id, skip_approval
        )
        if hook_approval is not None:
            return self._result_builder.pending_part(tool_call_id, tool_name, tool_args), hook_approval, None
        if runtime.tool_hook_control is not None:
            return self._hook_blocked_part(item), None, None
        runtime.completed_tool_hooks.pop((getattr(agent, "context_id", None) or "main", tool_call_id), None)

        if tool.spec.side_effect == "workspace_mutation" and runtime.run_ref is not None:
            try:
                lease_root = getattr(workspace, "shared_runtime_dir", workspace.workspace_dir)
                await get_workspace_write_lease_manager(lease_root).acquire(runtime.run_ref)
            except WorkspaceWriteBusy:
                result = self._result_builder.error_result(
                    tool_name,
                    "WorkspaceWriteBusy",
                    "workspace 正被其他 Run 修改；租约释放后请重新读取相关文件再重试",
                )
                result["recoverable"] = True
                return self._result_builder.completed_part(
                    tool_call_id, tool_name, result, tool_args=tool_args
                ), None, None

        await self._event_publisher.publish_started(
            session=session,
            runtime=runtime,
            agent=agent,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_args=tool_args,
        )

        runtime.active_tools[tool_call_id] = {
            "tool_name": tool_name,
            "side_effect": tool.spec.side_effect,
            "started_at": utc_now_iso(),
            "executing": True,
        }
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
        finally:
            runtime.active_tools.pop(tool_call_id, None)

        question_args, question = self._question_request(session, item, tool_call_id, tool_args, result)
        if question is not None:
            return self._result_builder.pending_part(tool_call_id, tool_name, question_args), None, question

        return await self._complete_tool_execution(
            session, workspace, agent, runtime, config, tool_name, tool_args, tool_call_id, result
        ), None, None

    async def _complete_tool_execution(
        self,
        session: SessionState,
        workspace: Any,
        agent: AgentState,
        runtime: RuntimeHandles,
        config: Any,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        result: dict[str, Any],
    ) -> ToolPart:
        context = HookContext(
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
        # 已发生的工具结果先发布，再进入可能等待人工的后置 Hook。
        await self._event_publisher.publish_finished(
            session=session,
            runtime=runtime,
            agent=agent,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=result,
        )
        hook_result = await self._hook_manager.run(HookType.TOOL_AFTER, context)
        await self._apply_tool_hook(session, runtime, hook_result)
        return self._result_builder.completed_part(tool_call_id, tool_name, result, tool_args=tool_args)

    def _forbidden_tool_part(
        self,
        tool: BaseTool,
        agent: AgentState,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> ToolPart | None:
        """在执行层重复校验权限，阻止重放或手工调用绕过 Agent 配置。"""
        code_id = getattr(tool, "code_tool_id", None)
        if code_id:
            if code_id in getattr(agent, "tool_ids", []) and not agent.readonly:
                return None
            return self._result_builder.completed_part(tool_call_id, tool_name,
                self._result_builder.error_result(tool_name, "ToolAgentForbidden", "当前 Agent 无权执行代码工具"), tool_args=tool_args)
        required_permission = f"mcp:{tool.mcp_server_name}" if getattr(tool, "mcp_server_name", None) else tool_name
        allowed_tools = getattr(agent, "allowed_tools", []) or []
        if required_permission in allowed_tools:
            return None
        result = self._result_builder.error_result(
            tool_name,
            "ToolAgentForbidden",
            f"当前 Agent 不允许调用工具：{tool_name}",
        )
        return self._result_builder.completed_part(tool_call_id, tool_name, result, tool_args=tool_args)

    async def _run_preflight(
        self,
        session: SessionState,
        runtime: RuntimeHandles,
        agent: AgentState,
        tool: BaseTool,
        tool_context: ToolExecutionContext,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        skip_approval: bool,
    ) -> tuple[ToolPart | None, ApprovalRequest | None]:
        if skip_approval:
            return None, None
        try:
            preflight = await tool.preflight(tool_args, tool_context)
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("tool preflight failed", tool_name=tool_name, error=str(exc))
            result = self._result_builder.error_result(tool_name, exc.__class__.__name__, str(exc))
            await self._publish_preflight_error(
                session=session,
                runtime=runtime,
                agent=agent,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=result,
            )
            return self._result_builder.completed_part(tool_call_id, tool_name, result, tool_args=tool_args), None
        if preflight.status == "blocked" and preflight.result is not None:
            if preflight.result.get("status") == "error":
                await self._publish_preflight_error(
                    session=session,
                    runtime=runtime,
                    agent=agent,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    result=preflight.result,
                )
            return self._result_builder.completed_part(tool_call_id, tool_name, preflight.result, tool_args=tool_args), None
        if preflight.status != "requires_approval":
            return None, None
        if is_manual_approval_disabled(tool_context.config):
            # 预检仍已执行；仅把“可审批”结论自动放行，不绕过 blocked 或其他安全校验。
            tool_context.skip_approval = True
            return None, None
        return None, ApprovalRequest(
            approval_id=f"approval_tool_{tool_call_id}",
            reason=preflight.reason or "该工具调用需要人工确认后才能执行",
            action={"type": "tool_call", "tool_name": tool_name, "args": tool_args},
            created_at=utc_now_iso(),
        )

    def _spec_approval(
        self,
        tool: BaseTool,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        skip_approval: bool,
        config: Any,
    ) -> ApprovalRequest | None:
        if not tool.spec.requires_approval or skip_approval or is_manual_approval_disabled(config):
            return None
        return ApprovalRequest(
            approval_id=f"approval_tool_{tool_call_id}",
            reason="该工具需要人工确认后才能执行",
            action={"type": "tool_call", "tool_name": tool_name, "args": tool_args},
            created_at=utc_now_iso(),
        )

    async def _run_before_hook(
        self,
        session: SessionState,
        workspace: Any,
        agent: AgentState,
        runtime: RuntimeHandles,
        config: Any,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        skip_approval: bool,
    ) -> ApprovalRequest | None:
        context = HookContext(
            hook_type=HookType.TOOL_BEFORE.value,
            session=session,
            workspace=workspace,
            agent=agent,
            messages=session.messages,
            tool_call={"tool_name": tool_name, "args": tool_args, "tool_call_id": tool_call_id},
            config=config,
            runtime=runtime,
        )
        key = (getattr(agent, "context_id", None) or "main", tool_call_id)
        result = runtime.completed_tool_hooks.get(key)
        if result is None:
            result = await self._hook_manager.run(HookType.TOOL_BEFORE, context)
            runtime.completed_tool_hooks[key] = result
            await self._append_hook_effects(session, runtime, result)
        if result.stop_loop or result.fail_session:
            runtime.tool_hook_control = result
            return None
        if result.requires_human_input and result.human_request and not skip_approval:
            return result.human_request.model_copy(update={"action": {**result.human_request.action, "hook_origin": True}})
        return None

    async def _apply_tool_hook(self, session: SessionState, runtime: RuntimeHandles, result: HookResult) -> None:
        await self._append_hook_effects(session, runtime, result)
        if result.stop_loop or result.fail_session or result.requires_human_input:
            previous = runtime.tool_hook_control
            runtime.tool_hook_control = self._hook_manager._merge_results(previous, result) if previous else result

    async def _append_hook_effects(self, session: SessionState, runtime: RuntimeHandles, result: HookResult) -> None:
        if result.messages_to_append or result.events_to_emit or result.context_patch or result.fail_session or result.stop_loop:
            await SessionMessageAppender().apply_hook_result(session, result, runtime)

    def _hook_blocked_part(self, item: dict[str, Any]) -> ToolPart:
        name = item["tool_name"]
        return self._result_builder.completed_part(
            item["tool_call_id"], name,
            self._result_builder.error_result(name, "HookBlocked", "Hook 已阻止后续工具执行"),
            tool_args=item.get("arguments", {}),
        )

    def _question_request(
        self,
        session: SessionState,
        item: dict[str, Any],
        tool_call_id: str,
        tool_args: dict[str, Any],
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], QuestionRequest | None]:
        if result.get("status") != "question_required":
            return tool_args, None
        # question_id 是恢复人机交互的唯一标识；随 pending ToolPart 持久化，供页面回放后继续提交答案。
        question_id = str(result.get("question_id") or f"question_tool_{tool_call_id}")
        question_args = {**tool_args, "question_id": question_id}
        item["arguments"] = question_args
        self._persist_question_id(session, tool_call_id, question_id)
        return question_args, QuestionRequest(
            question_id=question_id,
            questions=[question for question in result.get("questions", []) if isinstance(question, dict)],
            created_at=utc_now_iso(),
        )

    def _persist_question_id(self, session: SessionState, tool_call_id: str, question_id: str) -> None:
        """把交互 ID 写回已落盘前的 pending ToolPart，支持刷新后的 question 回放。"""
        for message in reversed(session.messages):
            for part in message.parts:
                if isinstance(part, ToolPart) and part.call_id == tool_call_id:
                    part.state.input = {**part.state.input, "question_id": question_id}
                    return

    async def _publish_preflight_error(
        self,
        *,
        session: SessionState,
        runtime: RuntimeHandles,
        agent: AgentState,
        tool_name: str,
        tool_call_id: str,
        result: dict[str, Any],
    ) -> None:
        # preflight 发生在真实工具执行前；这里仍发布失败事件，避免前端只看到消息入库、事件流缺失。
        await self._event_publisher.publish_finished(
            session=session,
            runtime=runtime,
            agent=agent,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=result,
        )


def is_manual_approval_disabled(config: Any) -> bool:
    """仅判断审批开关；question 是否可等待由会话运行策略单独决定。"""
    hitl = getattr(config, "human_in_the_loop", None)
    return hitl is not None and not bool(getattr(hitl, "enabled", True))
