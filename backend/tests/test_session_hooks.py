from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codepilot.config import AppSettings, build_llm_runtime_settings
from codepilot.config.settings import LLMProviderSettings, LLMSettings
from codepilot.context import CompressionResult
from codepilot.events import EventBus
from codepilot.hooks import BaseHook, HookContext, HookManager, HookResult, HookType, RuntimeHandles
from codepilot.main import _build_hook_manager
from codepilot.session import (
    AgentLoop,
    ApprovalResult,
    Message,
    QuestionResult,
    SessionState,
    SessionStatus,
    TextPart,
    ToolPart,
    build_user_message_info,
)
from codepilot.session.agents import AgentProfile
from codepilot.session.message import ToolPartState
from codepilot.session.session import summarize_question_answers
from codepilot.session.title import SessionTitleService
from codepilot.tools import BaseTool, QuestionTool, ToolDispatcher, ToolRegistry, ToolSpec
from codepilot.tools.base import ToolExecutionContext


def build_settings() -> AppSettings:
    llm_settings = LLMSettings(
        providers={
            "openai": LLMProviderSettings(
                label="OpenAI",
                models=["gpt-5.3-codex"],
            )
        }
    )
    settings = AppSettings(llm=llm_settings)
    runtime = build_llm_runtime_settings(settings.llm, environ={"OPENAI_API_KEY": "sk-openai"})
    return settings.model_copy(update={"llm_runtime": runtime})


def build_session() -> SessionState:
    return SessionState(
        session_id="session_1",
        workspace_id="ws_1",
        workspace_path="/tmp/codepilot",
        agent_name="build",
        provider="openai",
        model="gpt-5.3-codex",
        status=SessionStatus.RUNNING,
        created_at="2026-04-30T00:00:00Z",
        updated_at="2026-04-30T00:00:00Z",
        messages=[
            Message(
                info=build_user_message_info(
                    message_id="msg_user_1",
                    session_id="session_1",
                    created_at_ms=1_746_000_000_000,
                    agent="build",
                    provider_id="openai",
                    model_id="gpt-5.3-codex",
                ),
                parts=[TextPart(text="hello")],
            )
        ],
    )


def test_summarize_question_answers_uses_labels_and_notes() -> None:
    questions = [
        {
            "id": "target",
            "question": "请选择目标",
            "multiple": False,
            "options": [
                {"value": "backend", "label": "后端"},
                {"value": "none", "label": "不是以上任何选项"},
            ],
        },
        {
            "id": "compat",
            "question": "是否需要兼容旧数据？",
            "multiple": True,
            "options": [
                {"value": "yes", "label": "需要兼容"},
                {"value": "docs", "label": "同步文档"},
            ],
        },
    ]
    answers = {
        "target": {"values": ["none"], "note": ""},
        "compat": {"values": ["yes", "docs"], "note": "保留历史字段读取"},
    }

    summary = summarize_question_answers(questions, answers)

    assert '"values"' not in summary
    assert "回答：不是以上任何选项。" in summary
    assert "回答：需要兼容、同步文档。备注：保留历史字段读取。" in summary
    assert "用户备注" not in summary


class RecordingHook(BaseHook):
    record_key: str
    appended_text: str | None = None
    context_patch: dict[str, Any] = {}

    async def execute(self, ctx: HookContext) -> HookResult:
        messages_to_append: list[Message] = []
        if self.appended_text:
            messages_to_append.append(
                Message(
                    info=build_user_message_info(
                        message_id=f"msg_{self.record_key}",
                        session_id=ctx.session.session_id,
                        created_at_ms=1_746_000_000_000,
                        agent=ctx.agent.name,
                        provider_id=ctx.session.provider,
                        model_id=ctx.session.model,
                    ),
                    parts=[TextPart(text=self.appended_text, synthetic=True)],
                )
            )
        return HookResult(messages_to_append=messages_to_append, context_patch=self.context_patch)


class StopHook(RecordingHook):
    async def execute(self, ctx: HookContext) -> HookResult:
        result = await super().execute(ctx)
        return result.model_copy(update={"stop_loop": True})


class TraceHook(BaseHook):
    trace_label: str

    async def execute(self, ctx: HookContext) -> HookResult:
        ctx.session.metadata.setdefault("trace", []).append(self.trace_label)
        return HookResult()


class TraceContextCompressor:
    async def compress(self, *, session: SessionState, **_kwargs: Any) -> CompressionResult:
        session.metadata.setdefault("trace", []).append("context_compression")
        return CompressionResult()


class StubLiteLLMClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.last_provider_messages: list[Any] = []

    def build_provider_messages(self, messages: list[Message], system_prompt: str | None = None) -> list[Any]:
        provider_messages: list[Any] = []
        if system_prompt:
            provider_messages.append({"role": "system", "content": system_prompt})
        provider_messages.extend(messages)
        return provider_messages

    async def stream_chat(
        self,
        session: SessionState,
        llm_state: Any,
        provider_messages: list[Any],
        tools: list[dict[str, Any]],
        event_bus: EventBus,
    ) -> Any:
        self.calls += 1
        self.last_provider_messages = provider_messages
        if self.error:
            raise self.error
        return SimpleNamespace(text="done", reasoning="", tool_calls=[])


class StubLLMAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class StubTitleLLMClient:
    def __init__(self, *, response: str = "修复会话标题展示", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}

    async def complete_text(self, **kwargs: object) -> str:
        self.calls += 1
        self.last_kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


class RecordingEventBus:
    def __init__(self) -> None:
        self.domain_events: list[Any] = []
        self.stream_events: list[Any] = []

    async def publish_domain_event(self, event: Any) -> None:
        self.domain_events.append(event)

    async def publish_stream_event(self, event: Any) -> None:
        self.stream_events.append(event)


class StubToolRegistry:
    def get_llm_tool_schemas(self, allowed_tools: list[str], *, agent_profile: Any | None = None) -> list[dict[str, Any]]:
        return []


class StubToolDispatcher:
    async def execute_tool_calls(self, **kwargs: Any) -> Any:
        raise AssertionError("当前测试不应执行工具调用")

    async def execute_approved_tool_call(self, **kwargs: Any) -> Any:
        raise AssertionError("当前测试不应执行审批后的工具调用")


def test_session_title_service_generates_title_for_first_user_message(monkeypatch: Any) -> None:
    client = StubTitleLLMClient(response="《这是一个非常非常长的会话标题应该被截断并继续超过限制》")
    monkeypatch.setattr("codepilot.session.title.LiteLLMClient", lambda: client)
    session = build_session()
    event_bus = RecordingEventBus()
    service = SessionTitleService()

    asyncio.run(service.generate_for_session(session, event_bus))

    assert client.calls == 1
    assert session.title == "这是一个非常非常长的会话标题应"
    assert len(session.title) == 15
    assert event_bus.domain_events[0].data["title"] == "这是一个非常非常长的会话标题应"
    assert event_bus.domain_events[0].event_type.value == "session_meta"
    assert event_bus.stream_events[0].event_type == "session_title_updated"


def test_session_title_service_uses_fixed_qwen_model(monkeypatch: Any) -> None:
    client = StubTitleLLMClient()
    monkeypatch.setattr("codepilot.session.title.LiteLLMClient", lambda: client)
    session = build_session()
    service = SessionTitleService()

    asyncio.run(service.generate_for_session(session, RecordingEventBus()))

    llm_state = client.last_kwargs["llm_state"]
    assert llm_state.provider == "qwen"
    assert llm_state.model == "qwen3.5-flash"
    assert llm_state.metadata["litellm_model_prefix"] == "openai/"


def test_session_title_service_runs_only_on_first_user_message(monkeypatch: Any) -> None:
    client = StubTitleLLMClient()
    monkeypatch.setattr("codepilot.session.title.LiteLLMClient", lambda: client)
    session = build_session()
    session.messages.append(
        Message(
            info=build_user_message_info(
                message_id="msg_user_2",
                session_id="session_1",
                created_at_ms=1_746_000_000_001,
                agent="build",
                provider_id="openai",
                model_id="gpt-5.3-codex",
            ),
            parts=[TextPart(text="继续补充")],
        )
    )
    service = SessionTitleService()

    asyncio.run(service.generate_for_session(session, RecordingEventBus()))

    assert client.calls == 0
    assert session.title is None


def test_session_title_service_failure_does_not_block_session(monkeypatch: Any) -> None:
    client = StubTitleLLMClient(error=RuntimeError("title failed"))
    monkeypatch.setattr("codepilot.session.title.LiteLLMClient", lambda: client)
    session = build_session()
    service = SessionTitleService()

    asyncio.run(service.generate_for_session(session, RecordingEventBus()))

    assert client.calls == 1
    assert session.title is None
    assert session.status == SessionStatus.RUNNING


def test_builtin_session_title_hook_is_not_registered() -> None:
    settings = AppSettings()
    manager = _build_hook_manager(settings)

    title_hooks = [hook for hook in manager.get_hooks(HookType.SESSION_BEFORE) if hook.hook_id == "session-title-hook"]

    assert title_hooks == []


def test_agent_loop_runs_session_hooks_around_loop() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    hook_manager.register(
        RecordingHook(
            hook_id="session-before",
            hook_type=HookType.SESSION_BEFORE,
            name="session_before",
            record_key="session.before",
            appended_text="session before message",
            context_patch={"session_before": True},
            order=10,
        )
    )
    hook_manager.register(
        RecordingHook(
            hook_id="loop-before",
            hook_type=HookType.LOOP_BEFORE,
            name="loop_before",
            record_key="loop.before",
            appended_text="loop before message",
            order=20,
        )
    )
    hook_manager.register(
        RecordingHook(
            hook_id="session-after",
            hook_type=HookType.SESSION_AFTER,
            name="session_after",
            record_key="session.after",
            appended_text="session after message",
            context_patch={"session_after": True},
            order=30,
        )
    )
    loop = AgentLoop(
        llm_client=StubLiteLLMClient(),
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert result.metadata["session_before"] is True
    assert result.metadata["session_after"] is True
    assert [message.text_content() for message in result.messages[-4:]] == [
        "session before message",
        "loop before message",
        "done",
        "session after message",
    ]
    assistant_message = result.messages[-2]
    assert assistant_message.info.role == "assistant"
    assert assistant_message.info.parent_id == "msg_loop.before"
    assert assistant_message.info.agent == "build"
    assert assistant_message.info.model.provider_id == "openai"
    assert assistant_message.info.model.model_id == "gpt-5.3-codex"
    assert assistant_message.info.path.root == "/tmp/codepilot"
    assert isinstance(assistant_message.info.path.cwd, str)
    assert isinstance(assistant_message.info.time.created, int)
    assert isinstance(assistant_message.info.time.completed, int)
    assert assistant_message.info.finish == "completed"


def test_agent_loop_injects_layered_system_prompt_into_provider_messages(tmp_path: Path) -> None:
    settings = build_settings()
    session = build_session()
    (tmp_path / "AGENTS.md").write_text("项目约定：必须使用中文沟通。", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=tmp_path)
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    llm_client = StubLiteLLMClient()
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="构建 Agent 角色说明", max_iterations=1),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.last_provider_messages[0]["role"] == "system"
    system_prompt = llm_client.last_provider_messages[0]["content"]
    assert "构建 Agent 角色说明" in system_prompt
    assert "项目约定：必须使用中文沟通。" in system_prompt
    assert "Skills 和领域知识采用按需加载策略" in system_prompt
    assert "- 是否使用 Git：是" in system_prompt
    assert llm_client.last_provider_messages[1].text_content() == "hello"


def test_agent_loop_stops_before_loop_when_session_before_requests_stop() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    stream_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)
    llm_client = StubLiteLLMClient()
    hook_manager.register(
        StopHook(
            hook_id="session-before-stop",
            hook_type=HookType.SESSION_BEFORE,
            name="session_before_stop",
            record_key="session.before.stop",
            appended_text="session before stop",
        )
    )
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.calls == 0
    assert "loop_started" not in [event.event_type for event in stream_events]
    assert result.messages[-1].text_content() == "session before stop"


def test_agent_loop_stops_before_llm_when_loop_before_requests_stop() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    llm_client = StubLiteLLMClient()
    hook_manager.register(
        StopHook(
            hook_id="loop-before-stop",
            hook_type=HookType.LOOP_BEFORE,
            name="loop_before_stop",
            record_key="loop.before.stop",
            appended_text="loop before stop",
        )
    )
    hook_manager.register(
        RecordingHook(
            hook_id="llm-before",
            hook_type=HookType.LLM_BEFORE,
            name="llm_before",
            record_key="llm.before",
            appended_text="llm before message",
        )
    )
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.calls == 0
    assert result.messages[-1].text_content() == "loop before stop"


def test_agent_loop_compresses_context_before_llm_before_hook() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    hook_manager.register(
        TraceHook(
            hook_id="llm-before-trace",
            hook_type=HookType.LLM_BEFORE,
            name="llm_before_trace",
            trace_label="llm_before",
        )
    )
    loop = AgentLoop(
        llm_client=StubLiteLLMClient(),
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=hook_manager,
    )
    loop._turn_executor.context_compressor = TraceContextCompressor()  # type: ignore[assignment]

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert result.metadata["trace"] == ["context_compression", "llm_before"]


def test_agent_loop_converts_llm_error_to_assistant_message() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    stream_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)
    hook_manager.register(
        RecordingHook(
            hook_id="session-before",
            hook_type=HookType.SESSION_BEFORE,
            name="session_before",
            record_key="session.before",
            appended_text="before message",
            order=10,
        )
    )
    hook_manager.register(
        RecordingHook(
            hook_id="session-after",
            hook_type=HookType.SESSION_AFTER,
            name="session_after",
            record_key="session.after",
            appended_text="cleanup message",
            context_patch={"cleanup": "done"},
            order=20,
        )
    )
    loop = AgentLoop(
        llm_client=StubLiteLLMClient(error=StubLLMAPIError("bad request: invalid model", 400)),
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.FAILED
    assert session.metadata["cleanup"] == "done"
    assert session.messages[-3].text_content() == "before message"
    assert session.messages[-1].text_content() == "cleanup message"
    error_message = session.messages[-2]
    assert error_message.info.role == "assistant"
    assert error_message.info.error is not None
    assert error_message.info.error.code == "llm_bad_request"
    assert error_message.info.error.detail["status_code"] == 400
    assert error_message.info.finish == "llm_error"
    assert "LLM 调用失败，AgentLoop 已停止：bad request: invalid model" == error_message.text_content()
    assert [event.event_type for event in stream_events].count("assistant_message_completed") == 1
    loop_finished_events = [event for event in stream_events if event.event_type == "loop_finished"]
    assert loop_finished_events[-1].data["status"] == SessionStatus.FAILED.value
    assert stream_events[-1].event_type == "session_failed"


def test_agent_loop_stops_when_llm_quota_error() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    loop = AgentLoop(
        llm_client=StubLiteLLMClient(error=StubLLMAPIError("账户余额不足，请充值后重试", 400)),
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=HookManager(),
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.FAILED
    assert len([message for message in result.messages if message.info.role == "assistant"]) == 1
    error_message = result.messages[-1]
    assert error_message.info.error is not None
    assert error_message.info.error.code == "llm_insufficient_quota"
    assert error_message.info.finish == "llm_error"


class ApprovalTool(BaseTool):
    spec = ToolSpec(
        name="approval_tool",
        description="需要审批的测试工具",
        input_schema={"type": "object", "properties": {}},
        requires_approval=True,
        timeout_seconds=1,
    )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        return {"status": "ok", "args": args}


class ToolCallLiteLLMClient(StubLiteLLMClient):
    async def stream_chat(
        self,
        session: SessionState,
        llm_state: Any,
        provider_messages: list[Any],
        tools: list[dict[str, Any]],
        event_bus: EventBus,
    ) -> Any:
        self.calls += 1
        return SimpleNamespace(
            text="需要调用工具",
            reasoning="",
            tool_calls=[{"tool_call_id": "call_1", "tool_name": "approval_tool", "arguments": {}}],
        )


class ContinueToolCallLiteLLMClient(StubLiteLLMClient):
    async def stream_chat(
        self,
        session: SessionState,
        llm_state: Any,
        provider_messages: list[Any],
        tools: list[dict[str, Any]],
        event_bus: EventBus,
    ) -> Any:
        self.calls += 1
        return SimpleNamespace(
            text="继续执行",
            reasoning="",
            tool_calls=[
                {
                    "tool_call_id": f"call_{len(session.messages)}",
                    "tool_name": "continue_tool",
                    "arguments": {},
                }
            ],
        )


class QuestionToolCallLiteLLMClient(StubLiteLLMClient):
    async def stream_chat(
        self,
        session: SessionState,
        llm_state: Any,
        provider_messages: list[Any],
        tools: list[dict[str, Any]],
        event_bus: EventBus,
    ) -> Any:
        self.calls += 1
        self.last_provider_messages = provider_messages
        if self.calls == 1:
            return SimpleNamespace(
                text="需要澄清",
                reasoning="",
                tool_calls=[
                    {
                        "tool_call_id": "call_question_1",
                        "tool_name": "question",
                        "arguments": {
                            "questions": [
                                {
                                    "id": "target",
                                    "question": "请选择目标",
                                    "options": [
                                        {"value": "backend", "label": "后端"},
                                        {"value": "none", "label": "不是以上任何选项"},
                                    ],
                                }
                            ]
                        },
                    }
                ],
            )
        return SimpleNamespace(text="收到答案", reasoning="", tool_calls=[])


class ContinueToolDispatcher(StubToolDispatcher):
    async def execute_tool_calls(self, **kwargs: Any) -> Any:
        tool_calls = kwargs["tool_calls"]
        return SimpleNamespace(
            tool_parts=[
                ToolPart(
                    call_id=tool_call["tool_call_id"],
                    tool=tool_call["tool_name"],
                    state=ToolPartState(
                        status="completed",
                        input=tool_call["arguments"],
                        output={"ok": True},
                    ),
                )
                for tool_call in tool_calls
            ],
            pending_approval=None,
            pending_question=None,
        )


def test_agent_loop_appends_assistant_message_and_skips_tools_when_llm_after_requests_stop() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    llm_client = ToolCallLiteLLMClient()
    hook_manager.register(
        StopHook(
            hook_id="llm-after-stop",
            hook_type=HookType.LLM_AFTER,
            name="llm_after_stop",
            record_key="llm.after.stop",
        )
    )
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.calls == 1
    assistant_message = result.messages[-1]
    assert assistant_message.info.role == "assistant"
    assert assistant_message.info.finish == "stopped"
    assert assistant_message.text_content() == "需要调用工具"
    assert assistant_message.tool_parts()[0].state.status == "pending"


def test_agent_loop_stops_after_completed_tools_when_loop_after_requests_stop() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    llm_client = ContinueToolCallLiteLLMClient()
    hook_manager.register(
        StopHook(
            hook_id="loop-after-stop",
            hook_type=HookType.LOOP_AFTER,
            name="loop_after_stop",
            record_key="loop.after.stop",
            appended_text="loop after stop",
        )
    )
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=StubToolRegistry(),
        tool_dispatcher=ContinueToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.calls == 1
    assistant_message = result.messages[-2]
    assert assistant_message.info.finish == "tool_completed"
    assert assistant_message.tool_parts()[0].state.status == "completed"
    assert result.messages[-1].text_content() == "loop after stop"


def test_agent_loop_only_emits_one_human_approval_required_for_tool() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(ApprovalTool())
    dispatcher = ToolDispatcher(tool_registry, hook_manager)
    loop = AgentLoop(
        llm_client=ToolCallLiteLLMClient(),
        tool_registry=tool_registry,
        tool_dispatcher=dispatcher,
        hook_manager=hook_manager,
    )
    approval_event = asyncio.Event()
    approval_result_holder = {"result": None}
    stream_events: list[Any] = []
    domain_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)
    event_bus.subscribe_domain(domain_events.append)

    async def resolve_approval() -> SessionState:
        task = asyncio.create_task(
            loop.run(
                session=session,
                workspace=workspace,
                agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=3),
                runtime=runtime,
                config=settings,
                approval_event=approval_event,
                approval_result_holder=approval_result_holder,
                stop_event=asyncio.Event(),
            )
        )
        await asyncio.sleep(0)
        approval_result_holder["result"] = ApprovalResult(
            approval_id="approval_tool_call_1",
            approved=False,
            comment="拒绝执行",
            created_at="2026-04-30T00:00:01Z",
        )
        approval_event.set()
        return await task

    result = asyncio.run(resolve_approval())

    assert result.status == SessionStatus.CANCELLED
    assert [event.event_type for event in stream_events].count("human_approval_required") == 1
    interactions = [event for event in domain_events if event.event_type.value == "human_interaction"]
    assert [event.data["status"] for event in interactions] == ["pending", "rejected"]
    assert all(event.data["kind"] == "approval" for event in interactions)
    assert interactions[0].data["interaction_id"] == "approval_tool_call_1"
    assert session.messages[-1].info.role == "user"
    assert session.messages[-1].info.agent == "build"
    assert session.messages[-2].tool_parts()[0].state.status == "pending"


def test_agent_loop_question_reply_completes_tool_and_continues() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(QuestionTool(timeout_seconds=1))
    dispatcher = ToolDispatcher(tool_registry, hook_manager)
    llm_client = QuestionToolCallLiteLLMClient()
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        tool_dispatcher=dispatcher,
        hook_manager=hook_manager,
    )
    question_event = asyncio.Event()
    question_result_holder = {"result": None}
    stream_events: list[Any] = []
    domain_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)
    event_bus.subscribe_domain(domain_events.append)

    async def reply_question() -> SessionState:
        task = asyncio.create_task(
            loop.run(
                session=session,
                workspace=workspace,
                agent_profile=AgentProfile(name="build", system_prompt="test", allowed_tools=["question"], max_iterations=3),
                runtime=runtime,
                config=settings,
                approval_event=asyncio.Event(),
                approval_result_holder={"result": None},
                stop_event=asyncio.Event(),
                question_event=question_event,
                question_result_holder=question_result_holder,
            )
        )
        while not any(event.event_type == "human_question_required" for event in stream_events):
            await asyncio.sleep(0)
        request = next(event.data for event in stream_events if event.event_type == "human_question_required")
        question_result_holder["result"] = QuestionResult(
            question_id=request["question_id"],
            answers={"target": {"values": ["backend"], "note": ""}},
            created_at="2026-04-30T00:00:01Z",
        )
        question_event.set()
        return await task

    result = asyncio.run(reply_question())

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.calls == 2
    assert "human_approval_required" not in [event.event_type for event in stream_events]
    assert [event.event_type for event in stream_events].count("human_question_required") == 1
    assert [event.event_type for event in stream_events].count("human_question_resolved") == 1
    interactions = [event for event in domain_events if event.event_type.value == "human_interaction"]
    assert [event.data["status"] for event in interactions] == ["pending", "resolved"]
    assert all(event.data["kind"] == "question" for event in interactions)
    assert any(
        event.event_type.value == "session_lifecycle" and event.status == SessionStatus.RUNNING.value for event in domain_events
    )
    resolved_event = interactions[-1]
    assert resolved_event.data["message_id"] == result.messages[-2].info.id
    assert resolved_event.data["call_id"] == "call_question_1"
    assert resolved_event.data["result"]["answers"]["target"]["values"] == ["backend"]
    question_message = result.messages[-2]
    question_part = question_message.tool_parts()[0]
    assert question_part.tool == "question"
    assert question_part.state.status == "completed"
    assert question_part.state.output["answers"]["target"]["values"] == ["backend"]
    assert any(
        isinstance(message, Message)
        and message.tool_parts()
        and message.tool_parts()[0].state.output["tool_name"] == "question"
        for message in llm_client.last_provider_messages
    )


def test_agent_loop_question_decline_appends_user_message_and_stops() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(QuestionTool(timeout_seconds=1))
    dispatcher = ToolDispatcher(tool_registry, hook_manager)
    llm_client = QuestionToolCallLiteLLMClient()
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        tool_dispatcher=dispatcher,
        hook_manager=hook_manager,
    )
    question_event = asyncio.Event()
    question_result_holder = {"result": None}
    stream_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)

    async def decline_question() -> SessionState:
        task = asyncio.create_task(
            loop.run(
                session=session,
                workspace=workspace,
                agent_profile=AgentProfile(name="build", system_prompt="test", allowed_tools=["question"], max_iterations=3),
                runtime=runtime,
                config=settings,
                approval_event=asyncio.Event(),
                approval_result_holder={"result": None},
                stop_event=asyncio.Event(),
                question_event=question_event,
                question_result_holder=question_result_holder,
            )
        )
        while not any(event.event_type == "human_question_required" for event in stream_events):
            await asyncio.sleep(0)
        request = next(event.data for event in stream_events if event.event_type == "human_question_required")
        question_result_holder["result"] = QuestionResult(
            question_id=request["question_id"],
            declined=True,
            created_at="2026-04-30T00:00:01Z",
        )
        question_event.set()
        return await task

    result = asyncio.run(decline_question())

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.calls == 1
    assert result.messages[-1].info.role == "user"
    assert "用户拒绝回答 question 工具提出的问题" in result.messages[-1].text_content()
    assert result.messages[-2].tool_parts()[0].state.status == "pending"
    resolved = next(event for event in stream_events if event.event_type == "human_question_resolved")
    assert resolved.data["declined"] is True
    assert resolved.data["continue_loop"] is False


def test_agent_loop_appends_assistant_message_when_max_iterations_exceeded() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    stream_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)
    loop = AgentLoop(
        llm_client=ContinueToolCallLiteLLMClient(),
        tool_registry=StubToolRegistry(),
        tool_dispatcher=ContinueToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", max_iterations=1),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    max_iterations_message = result.messages[-1]
    assert result.status == SessionStatus.COMPLETED
    assert max_iterations_message.info.role == "assistant"
    assert max_iterations_message.info.finish == "max_iterations"
    assert "已超过最大轮推理次数限制（1 轮），停止推理。" == max_iterations_message.text_content()
    assert max_iterations_message.iter_parts("step-finish")[-1].reason == "max_iterations"
    completed_messages = [
        event.data["message"]
        for event in stream_events
        if event.event_type == "assistant_message_completed"
    ]
    assert completed_messages[-1]["info"]["finish"] == "max_iterations"
    assert completed_messages[-1]["parts"][1]["text"] == "已超过最大轮推理次数限制（1 轮），停止推理。"
