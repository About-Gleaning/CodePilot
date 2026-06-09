from __future__ import annotations


"""会话运行编排器。

负责接收网关输入，管理 session 生命周期，串联 Hook、事件总线与 AgentLoop，
并处理停止、人工审批回复等会话级控制逻辑。
"""

import asyncio
from typing import Any

from codepilot.config.settings import resolve_llm_selection, resolve_thinking_value
from codepilot.events import MessageCreatedEvent, SessionLifecycleEvent, SessionMetaEvent, StreamEvent
from codepilot.gateway import GatewayInput, GatewayInputType
from codepilot.hooks import HookManager, RuntimeHandles
from codepilot.session.agents import AgentProfile
from codepilot.session.message import Message, TextPart, build_user_message_info
from codepilot.session.session import AgentLoop
from codepilot.session.state import ApprovalResult, QuestionResult, SessionState, SessionStatus
from codepilot.session.title import SessionTitleService
from codepilot.utils import new_message_id, new_session_id, utc_now_iso, utc_now_millis


class SessionRunner:
    """负责单个工作区当前会话的创建、运行、停止与状态流转。"""

    def __init__(
        self,
        workspace: Any,
        config: Any,
        event_bus: Any,
        hook_manager: HookManager,
        agent_loop: AgentLoop,
        agent_profiles: dict[str, AgentProfile],
        title_service: SessionTitleService | None = None,
    ) -> None:
        self._workspace = workspace
        self._config = config
        self._event_bus = event_bus
        self._hook_manager = hook_manager
        self._agent_loop = agent_loop
        self._agent_profiles = agent_profiles
        self._session: SessionState | None = None
        self._task: asyncio.Task[SessionState] | None = None
        self._title_service = title_service or SessionTitleService()
        self._title_tasks: set[asyncio.Task[None]] = set()
        self._stop_event = asyncio.Event()
        self._approval_event = asyncio.Event()
        self._approval_result_holder: dict[str, ApprovalResult | None] = {"result": None}
        self._question_event = asyncio.Event()
        self._question_result_holder: dict[str, QuestionResult | None] = {"result": None}

    async def handle_input(self, gateway_input: GatewayInput) -> SessionState | None:
        """按输入类型路由到对应的会话处理分支。"""
        if gateway_input.type == GatewayInputType.STOP:
            return await self._handle_stop()
        if gateway_input.type == GatewayInputType.HUMAN_REPLY:
            return await self._handle_human_reply(gateway_input)
        if gateway_input.type == GatewayInputType.QUESTION_REPLY:
            return await self._handle_question_reply(gateway_input)
        if gateway_input.type == GatewayInputType.QUESTION_DECLINE:
            return await self._handle_question_decline(gateway_input)
        return await self._handle_user_message(gateway_input)

    def get_status_snapshot(self) -> dict[str, Any]:
        """返回当前会话的轻量状态快照，供外部轮询查看。"""
        return {
            "workspace_id": self._workspace.workspace_id,
            "workspace_path": str(self._workspace.workspace_path),
            "session_id": self._session.session_id if self._session else None,
            "status": self._session.status.value if self._session else SessionStatus.IDLE.value,
            "agent_name": self._session.agent_name if self._session else self._config.agent.default_agent_name,
            "provider": self._session.provider if self._session else None,
            "model": self._session.model if self._session else None,
            "thinking_enabled": bool(self._session.metadata.get("thinking_enabled")) if self._session else False,
            "thinking_value": self._session.metadata.get("thinking_value") if self._session else None,
        }

    def current_session_id(self) -> str | None:
        """返回当前内存中持有的 session_id；没有会话时返回 None。"""
        return self._session.session_id if self._session else None

    def load_session(self, session_id: str, replay: dict[str, Any]) -> SessionState:
        """把已持久化的历史会话恢复为当前可继续对话的内存会话。"""
        if self._session and self._session.status in {
            SessionStatus.RUNNING,
            SessionStatus.STOPPING,
            SessionStatus.WAITING_HUMAN,
        }:
            raise ValueError("当前 session 仍在运行或等待确认，不能加载历史会话")
        session_record = replay.get("session")
        if not isinstance(session_record, dict):
            raise ValueError(f"session `{session_id}` 不存在或无法回放")
        session_data = session_record.get("data")
        if not isinstance(session_data, dict):
            raise ValueError(f"session `{session_id}` 缺少可恢复的状态快照")
        messages = [Message.model_validate(message) for message in replay.get("messages") or []]
        loaded = SessionState.model_validate({**session_data, "messages": messages})
        if loaded.session_id != session_id:
            raise ValueError(f"session `{session_id}` 与持久化数据不一致")
        if loaded.status in {SessionStatus.RUNNING, SessionStatus.STOPPING, SessionStatus.WAITING_HUMAN}:
            # 服务重启或手动加载历史时，磁盘上的运行中状态已经不再对应真实后台任务。
            loaded.status = SessionStatus.CANCELLED
            loaded.updated_at = utc_now_iso()
        self._session = loaded
        self._task = None
        self._stop_event = asyncio.Event()
        self._approval_event = asyncio.Event()
        self._approval_result_holder = {"result": None}
        self._question_event = asyncio.Event()
        self._question_result_holder = {"result": None}
        return self._session

    async def shutdown(self) -> None:
        """在服务关闭时等待正在执行的会话安全结束。"""
        if self._task and not self._task.done():
            self._stop_event.set()
            await self._task

    async def _handle_user_message(self, gateway_input: GatewayInput) -> SessionState:
        """处理新的用户消息，并在必要时启动一轮新的会话执行。"""
        if self._session and self._session.status == SessionStatus.RUNNING:
            raise ValueError("当前 session 正在运行，只允许 stop")
        if self._session and self._session.status == SessionStatus.STOPPING:
            raise ValueError("当前 session 正在停止中，不接受新的 user_message")
        if self._session and self._session.status == SessionStatus.WAITING_HUMAN:
            raise ValueError("当前 session 正在等待人工确认，不接受新的 user_message")

        is_new_session = not gateway_input.session_id
        if gateway_input.session_id:
            if self._session is None:
                raise ValueError(f"session `{gateway_input.session_id}` 不存在或未加载")
            if self._session.session_id != gateway_input.session_id:
                raise ValueError(f"session `{gateway_input.session_id}` 不是当前可用会话")
            self._ensure_agent_supported(gateway_input.agent_name)
            activated_provider, selected_model = resolve_llm_selection(
                settings=self._config,
                requested_provider=gateway_input.provider,
                requested_model=gateway_input.model,
            )
            user_metadata = self._build_user_metadata(activated_provider.provider, selected_model, gateway_input)
            # 同一 session 继续执行时，允许显式切换 agent/provider/model；
            # session 顶层仅保存“当前最新执行配置”，历史配置仍由消息元数据承载。
            self._session.agent_name = gateway_input.agent_name
            self._session.provider = activated_provider.provider
            self._session.model = selected_model
            self._apply_user_metadata(user_metadata)
            self._session.status = SessionStatus.RUNNING
            self._session.updated_at = utc_now_iso()
        else:
            self._session = self._new_session(gateway_input)

        # 用户输入先落入 session 内存，再交给 AgentLoop 负责完整的一次 session 执行。
        message = self._build_user_message(gateway_input)
        self._session.messages.append(message)
        self._session.updated_at = utc_now_iso()

        if is_new_session:
            self._session.title = self._default_title(gateway_input.content, self._session.session_id)
            await self._event_bus.publish_domain_event(
                SessionMetaEvent(
                    session_id=self._session.session_id,
                    created_at=self._session.created_at,
                    data={
                        "title": self._session.title,
                        "workspace_id": self._session.workspace_id,
                        "workspace_path": self._session.workspace_path,
                        "initial_user_message_id": message.info.id,
                        "updated_at": self._session.updated_at,
                    },
                )
            )
            await self._event_bus.publish_domain_event(
                SessionLifecycleEvent(
                    session_id=self._session.session_id,
                    status=self._session.status.value,
                    created_at=utc_now_iso(),
                    data=self._session.model_dump(exclude={"messages"}),
                )
            )
            await self._event_bus.publish_stream_event(
                StreamEvent(
                    event_type="session_started",
                    session_id=self._session.session_id,
                    created_at=utc_now_iso(),
                    data=self._session.model_dump(exclude={"messages"}),
                )
            )
            self._schedule_title_generation(self._session)

        runtime = RuntimeHandles(event_bus=self._event_bus)
        profile = self._agent_profiles[self._session.agent_name]
        await self._event_bus.publish_domain_event(
            MessageCreatedEvent(
                session_id=self._session.session_id,
                created_at=utc_now_iso(),
                data={"record_type": "message"},
                message=message,
            )
        )
        await self._event_bus.publish_stream_event(
            StreamEvent(
                event_type="user_message_created",
                session_id=self._session.session_id,
                created_at=utc_now_iso(),
                data={"message": message.model_dump()},
            )
        )

        # 每次新一轮执行前重置 stop/approval 状态，避免上一个 session 的控制信号泄漏到本轮。
        self._stop_event = asyncio.Event()
        self._approval_event = asyncio.Event()
        self._approval_result_holder = {"result": None}
        self._question_event = asyncio.Event()
        self._question_result_holder = {"result": None}
        self._task = asyncio.create_task(
            self._run_loop(runtime=runtime, profile=profile),
            name=f"codepilot-session-{self._session.session_id}",
        )
        return self._session

    def _schedule_title_generation(self, session: SessionState) -> None:
        """新会话创建后异步刷新 LLM 标题，默认标题已先保证历史列表可展示。"""
        task = asyncio.create_task(
            self._title_service.generate_for_session(session, self._event_bus),
            name=f"codepilot-session-title-{session.session_id}",
        )
        self._title_tasks.add(task)
        task.add_done_callback(self._title_tasks.discard)

    async def _run_loop(self, runtime: RuntimeHandles, profile: AgentProfile) -> SessionState:
        """执行 AgentLoop，并在异常时补发失败事件。"""
        assert self._session is not None
        try:
            session = await self._agent_loop.run(
                session=self._session,
                workspace=self._workspace,
                agent_profile=profile,
                runtime=runtime,
                config=self._config,
                approval_event=self._approval_event,
                approval_result_holder=self._approval_result_holder,
                question_event=self._question_event,
                question_result_holder=self._question_result_holder,
                stop_event=self._stop_event,
            )
        except Exception as exc:  # noqa: BLE001
            # 运行异常时显式写入失败状态并发出错误事件，便于前端与日志系统感知失败原因。
            self._session.status = SessionStatus.FAILED
            self._session.updated_at = utc_now_iso()
            await self._event_bus.publish_stream_event(
                StreamEvent(
                    event_type="error",
                    session_id=self._session.session_id,
                    created_at=utc_now_iso(),
                    data={"message": str(exc)},
                )
            )
            await self._event_bus.publish_domain_event(
                SessionLifecycleEvent(
                    session_id=self._session.session_id,
                    status=self._session.status.value,
                    created_at=utc_now_iso(),
                    data=self._session.model_dump(exclude={"messages"}),
                )
            )
            raise
        return session

    async def _handle_stop(self) -> SessionState | None:
        """处理停止请求，并根据当前状态选择终止运行或中断审批等待。"""
        if self._session is None:
            return None
        if self._session.status == SessionStatus.WAITING_HUMAN:
            # 等待人工确认时没有运行中的 Loop 可中断，需要通过审批结果唤醒等待协程。
            self._session.status = SessionStatus.CANCELLED
            self._approval_result_holder["result"] = ApprovalResult(
                approval_id="stop_during_waiting",
                approved=False,
                comment="用户停止任务",
                created_at=utc_now_iso(),
            )
            self._approval_event.set()
            self._question_result_holder["result"] = QuestionResult(
                question_id="stop_during_waiting",
                declined=True,
                comment="用户停止任务",
                created_at=utc_now_iso(),
            )
            self._question_event.set()
        elif self._session.status == SessionStatus.RUNNING:
            # 正常运行中的会话使用 stop_event 协作式停止，避免强杀任务造成状态不一致。
            self._session.status = SessionStatus.STOPPING
            self._stop_event.set()
        self._session.updated_at = utc_now_iso()
        await self._event_bus.publish_domain_event(
            SessionLifecycleEvent(
                session_id=self._session.session_id,
                status=self._session.status.value,
                created_at=utc_now_iso(),
                data=self._session.model_dump(exclude={"messages"}),
            )
        )
        await self._event_bus.publish_stream_event(
            StreamEvent(
                event_type="session_status_changed",
                session_id=self._session.session_id,
                created_at=utc_now_iso(),
                data={"status": self._session.status.value},
            )
        )
        return self._session

    async def _handle_human_reply(self, gateway_input: GatewayInput) -> SessionState | None:
        """接收人工审批结果，并唤醒等待审批的执行流程。"""
        if self._session is None or self._session.status != SessionStatus.WAITING_HUMAN:
            raise ValueError("当前没有等待人工确认的 session")
        if self._session.metadata.get("pending_human_type") != "approval":
            raise ValueError("当前 session 等待的不是人工审批")
        self._approval_result_holder["result"] = ApprovalResult(
            approval_id=gateway_input.approval_id or "",
            approved=bool(gateway_input.approved),
            comment=gateway_input.comment,
            created_at=utc_now_iso(),
        )
        self._approval_event.set()
        return self._session

    async def _handle_question_reply(self, gateway_input: GatewayInput) -> SessionState | None:
        """接收 question 工具答案，并唤醒等待中的执行流程。"""
        if self._session is None or self._session.status != SessionStatus.WAITING_HUMAN:
            raise ValueError("当前没有等待用户回答的 session")
        if self._session.metadata.get("pending_human_type") != "question":
            raise ValueError("当前 session 等待的不是用户回答")
        self._question_result_holder["result"] = QuestionResult(
            question_id=gateway_input.question_id or "",
            answers=gateway_input.answers or {},
            declined=False,
            created_at=utc_now_iso(),
        )
        self._question_event.set()
        return self._session

    async def _handle_question_decline(self, gateway_input: GatewayInput) -> SessionState | None:
        """接收 question 拒答信号；会话循环会记录拒答消息并结束当前 run。"""
        if self._session is None or self._session.status != SessionStatus.WAITING_HUMAN:
            raise ValueError("当前没有等待用户回答的 session")
        if self._session.metadata.get("pending_human_type") != "question":
            raise ValueError("当前 session 等待的不是用户回答")
        self._question_result_holder["result"] = QuestionResult(
            question_id=gateway_input.question_id or "",
            answers={},
            declined=True,
            comment=gateway_input.comment,
            created_at=utc_now_iso(),
        )
        self._question_event.set()
        return self._session

    def _new_session(self, gateway_input: GatewayInput) -> SessionState:
        """基于当前输入创建新的 session，并解析本次会话使用的 LLM 选择。"""
        now = utc_now_iso()
        self._ensure_agent_supported(gateway_input.agent_name)
        activated_provider, selected_model = resolve_llm_selection(
            settings=self._config,
            requested_provider=gateway_input.provider,
            requested_model=gateway_input.model,
        )
        return SessionState(
            session_id=new_session_id(),
            workspace_id=self._workspace.workspace_id,
            workspace_path=str(self._workspace.workspace_path),
            agent_name=gateway_input.agent_name or self._config.agent.default_agent_name,
            provider=activated_provider.provider,
            model=selected_model,
            status=SessionStatus.RUNNING,
            created_at=now,
            updated_at=now,
            metadata=self._build_user_metadata(activated_provider.provider, selected_model, gateway_input),
        )

    def _apply_user_metadata(self, user_metadata: dict[str, Any]) -> None:
        """同步本轮用户显式设置；只保留已校验的思考档位。"""
        assert self._session is not None
        self._session.metadata.update(user_metadata)

    def _build_user_metadata(self, provider: str, model: str, gateway_input: GatewayInput) -> dict[str, Any]:
        thinking_value = resolve_thinking_value(
            settings=self._config,
            provider=provider,
            model=model,
            metadata=gateway_input.metadata,
        )
        return {
            "thinking_enabled": thinking_value is not None,
            "thinking_value": thinking_value,
        }

    def _ensure_agent_supported(self, agent_name: str | None) -> None:
        """在进入执行链前显式校验 agent，避免后续字典取值抛出不友好的 KeyError。"""
        if not agent_name or agent_name not in self._agent_profiles:
            raise ValueError(f"agent `{agent_name}` 不存在或不可用")
        if getattr(self._agent_profiles[agent_name], "kind", "agent") != "agent":
            raise ValueError(f"agent `{agent_name}` 不能直接从前端选择")

    def _build_user_message(self, gateway_input: GatewayInput) -> Message:
        """把网关输入转换为统一的用户消息结构，写入 session 消息列表。"""
        assert self._session is not None
        return Message(
            info=build_user_message_info(
                message_id=new_message_id(),
                session_id=self._session.session_id,
                created_at_ms=utc_now_millis(),
                agent=self._session.agent_name,
                provider_id=self._session.provider,
                model_id=self._session.model,
            ),
            parts=[TextPart(text=gateway_input.content or "")],
        )

    def _default_title(self, content: str, session_id: str) -> str:
        """用首条用户输入生成默认标题，避免新会话历史列表出现空标题。"""
        normalized = " ".join(str(content or "").split())
        return normalized[:15] or session_id
