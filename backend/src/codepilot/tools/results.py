from __future__ import annotations

"""工具执行结果与工具流事件构建辅助。"""

from typing import Any

from codepilot.events import StreamEvent
from codepilot.hooks import RuntimeHandles
from codepilot.session import AgentState, AssistantMessageError, SessionState
from codepilot.session.message import ToolPart, ToolPartState
from codepilot.utils import utc_now_iso


class ToolResultBuilder:
    """集中构建 ToolPart 和标准错误结果，避免调度器混入展示细节。"""

    def pending_part(self, tool_call_id: str | None, tool_name: str, tool_args: dict[str, Any]) -> ToolPart:
        return ToolPart(
            call_id=tool_call_id or f"call_{utc_now_iso()}",
            tool=tool_name,
            state=ToolPartState(
                status="pending",
                input=tool_args,
                time={"created": utc_now_iso()},
            ),
        )

    def completed_part(
        self,
        tool_call_id: str | None,
        tool_name: str,
        result: dict[str, Any],
        tool_args: dict[str, Any] | None = None,
    ) -> ToolPart:
        status = "error" if result.get("status") == "error" else "completed"
        return ToolPart(
            call_id=tool_call_id or f"call_{utc_now_iso()}",
            tool=tool_name,
            state=ToolPartState(
                status=status,
                input=tool_args or {},
                title=result.get("title"),
                output=result,
                error=self.tool_error(result) if status == "error" else None,
                time={"start": utc_now_iso(), "end": utc_now_iso()},
                attachments=result.get("attachments") or [],
            ),
        )

    def missing_tool_result(self, tool_name: str) -> dict[str, Any]:
        return self.error_result(tool_name, "ToolNotFoundError", f"工具不存在：{tool_name}")

    def error_result(self, tool_name: str, error_type: str, error_message: str) -> dict[str, Any]:
        return {
            "status": "error",
            "tool_name": tool_name,
            "error_type": error_type,
            "error_message": error_message,
            "recoverable": True,
        }

    def tool_error(self, result: dict[str, Any]) -> AssistantMessageError:
        return AssistantMessageError(
            code=str(result.get("error_type") or "ToolError"),
            message=str(result.get("error_message") or "工具执行失败"),
        )


class ToolEventPublisher:
    """统一发布工具执行阶段的前端流事件。"""

    async def publish_started(
        self,
        *,
        session: SessionState,
        runtime: RuntimeHandles,
        agent: AgentState,
        tool_name: str,
        tool_call_id: str,
        tool_args: dict[str, Any],
    ) -> None:
        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="tool_call_started",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={
                    **self._agent_event_data(agent),
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "args": tool_args,
                },
            )
        )

    async def publish_finished(
        self,
        *,
        session: SessionState,
        runtime: RuntimeHandles,
        agent: AgentState,
        tool_name: str,
        tool_call_id: str,
        result: dict[str, Any],
    ) -> None:
        await runtime.event_bus.publish_stream_event(
            StreamEvent(
                event_type="tool_call_finished" if result.get("status") != "error" else "tool_call_failed",
                session_id=session.session_id,
                created_at=utc_now_iso(),
                data={
                    **self._agent_event_data(agent),
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "result": result,
                },
            )
        )

    def _agent_event_data(self, agent: AgentState) -> dict[str, Any]:
        return {
            "agent": getattr(agent, "name", None),
            "agent_kind": getattr(agent, "kind", "agent"),
            "context_id": getattr(agent, "context_id", None),
            "parent_call_id": getattr(agent, "parent_call_id", None),
        }
