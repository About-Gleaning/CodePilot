from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codepilot.config import AppSettings, build_llm_runtime_settings
from codepilot.config.settings import BashToolSettings, LLMProviderSettings, LLMSettings
from codepilot.context import CompressionResult
from codepilot.events import EventBus
from codepilot.hooks import BaseHook, HookContext, HookManager, HookResult, HookType, RuntimeHandles
from codepilot.llm import LiteLLMClient
from codepilot.main import _build_hook_manager
from codepilot.session import (
    AgentLoop,
    AgentState,
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
from codepilot.tools import BaseTool, BashTool, QuestionTool, ToolDispatcher, ToolRegistry, ToolSpec
from codepilot.tools.base import ToolExecutionContext, ToolPreflightResult


def build_settings() -> AppSettings:
    llm_settings = LLMSettings(
        providers={
            "openai": LLMProviderSettings(
                label="OpenAI",
                models=[
                    {
                        "id": "gpt-5.3-codex",
                        "thinking": {
                            "kind": "reasoning_effort",
                            "allowed_values": ["low", "medium", "high"],
                            "default_value": "medium",
                        },
                    }
                ],
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
        self.provider_message_calls: list[list[Any]] = []

    def build_provider_messages(
        self,
        messages: list[Message],
        system_prompt: str | None = None,
        runtime_context: str | None = None,
    ) -> list[Any]:
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
        self.provider_message_calls.append(provider_messages)
        if self.error:
            raise self.error
        return SimpleNamespace(text="done", reasoning="", tool_calls=[])


class SequencedLiteLLMClient(StubLiteLLMClient):
    def __init__(self, results: list[Any]) -> None:
        super().__init__()
        self.results = results

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
        self.provider_message_calls.append(provider_messages)
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


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


def test_subagent_approval_request_fails_without_human_waiting() -> None:
    settings = build_settings()
    parent_session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = RecordingEventBus()
    hook_manager = _build_hook_manager(settings)
    llm_client = StubLiteLLMClient()
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run_subagent(
            parent_session=parent_session,
            workspace=workspace,
            agent_profile=AgentProfile(name="explore", system_prompt="test", kind="subagent", max_iterations=3),
            task="读取文件前先 [[approve]]",
            parent_call_id="call_task_1",
            runtime=RuntimeHandles(event_bus=event_bus),
            config=settings,
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.FAILED
    assert "subagent 不支持人工审批" in result.metadata["subagent_error"]
    assert llm_client.calls == 0
    assert "human_approval_required" not in [event.event_type for event in event_bus.stream_events]
    assert [event.event_type for event in event_bus.stream_events].count("error") == 1


def test_agent_loop_llm_state_carries_thinking_enabled_metadata() -> None:
    settings = build_settings()
    session = build_session()
    session.metadata["thinking_enabled"] = True
    loop = AgentLoop(
        llm_client=StubLiteLLMClient(),
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=_build_hook_manager(settings),
    )

    llm_state = loop._build_llm_state(session, settings)

    assert llm_state.metadata["thinking_enabled"] is True
    assert llm_state.metadata["thinking"]["default_value"] == "medium"


def test_subagent_inherits_parent_thinking_enabled_metadata() -> None:
    settings = build_settings()
    parent_session = build_session()
    parent_session.metadata["thinking_enabled"] = True
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = RecordingEventBus()
    llm_client = StubLiteLLMClient()
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=StubToolRegistry(),
        tool_dispatcher=StubToolDispatcher(),
        hook_manager=_build_hook_manager(settings),
    )

    result = asyncio.run(
        loop.run_subagent(
            parent_session=parent_session,
            workspace=workspace,
            agent_profile=AgentProfile(name="explore", system_prompt="test", kind="subagent", max_iterations=1),
            task="读取文件",
            parent_call_id="call_task_1",
            runtime=RuntimeHandles(event_bus=event_bus),
            config=settings,
            stop_event=asyncio.Event(),
        )
    )

    assert result.metadata["thinking_enabled"] is True


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
    assert assistant_message.info.path.cwd == "/tmp/codepilot"
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


def test_agent_loop_stops_when_llm_auth_error() -> None:
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
        llm_client=StubLiteLLMClient(error=StubLLMAPIError("invalid api key", 401)),
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
    assert error_message.info.error.code == "llm_auth_error"
    assert error_message.info.error.detail["status_code"] == 401
    assert error_message.info.finish == "llm_error"
    assert "LLM 调用失败，AgentLoop 已停止：invalid api key" == error_message.text_content()
    assert [event.event_type for event in stream_events].count("assistant_message_completed") == 1
    loop_finished_events = [event for event in stream_events if event.event_type == "loop_finished"]
    assert loop_finished_events[-1].data["status"] == SessionStatus.FAILED.value
    assert stream_events[-1].event_type == "session_failed"


def test_agent_loop_wraps_nonfatal_llm_error_for_next_iteration() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    llm_client = SequencedLiteLLMClient(
        [
            StubLLMAPIError("unknown variant image_url, expected text", 400),
            SimpleNamespace(text="当前模型不支持图片输入。", reasoning="", tool_calls=[]),
        ]
    )
    loop = AgentLoop(
        llm_client=llm_client,
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

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.calls == 2
    recoverable_message = result.messages[-2]
    assert recoverable_message.info.role == "assistant"
    assert recoverable_message.info.error is not None
    assert recoverable_message.info.error.code == "llm_bad_request"
    assert recoverable_message.info.finish == "llm_error_recoverable"
    assert "失败报文（已脱敏）" in recoverable_message.text_content()
    second_call_messages = llm_client.provider_message_calls[1]
    assert any(
        isinstance(message, Message) and "unknown variant image_url, expected text" in message.text_content()
        for message in second_call_messages
    )
    assert result.messages[-1].text_content() == "当前模型不支持图片输入。"


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


def test_agent_loop_stops_when_llm_service_unavailable() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    loop = AgentLoop(
        llm_client=StubLiteLLMClient(error=StubLLMAPIError("upstream service unavailable", 503)),
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
    error_message = result.messages[-1]
    assert error_message.info.error is not None
    assert error_message.info.error.code == "llm_service_unavailable"
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


class PreflightApprovalTool(BaseTool):
    spec = ToolSpec(
        name="preflight_approval_tool",
        description="preflight 需要审批的测试工具",
        input_schema={"type": "object", "properties": {}},
        timeout_seconds=1,
    )

    async def preflight(self, args: dict[str, Any], context: ToolExecutionContext) -> ToolPreflightResult:
        return ToolPreflightResult(status="requires_approval", reason="需要审批")

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


def test_non_interactive_session_blocks_tool_spec_approval() -> None:
    settings = build_settings()
    session = build_session()
    session.metadata["allow_human_interaction"] = False
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"), workspace_dir=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(ApprovalTool())
    dispatcher = ToolDispatcher(tool_registry, hook_manager)

    batch = asyncio.run(
        dispatcher.execute_tool_calls(
            session=session,
            workspace=workspace,
            agent=AgentState(name="build", allowed_tools=["approval_tool"]),
            tool_calls=[{"tool_call_id": "call_1", "tool_name": "approval_tool", "arguments": {}}],
            runtime=runtime,
            config=settings,
        )
    )

    assert batch.pending_approval is not None
    assert batch.pending_approval.request.approval_id == "approval_tool_call_1"
    assert batch.pending_approval.resume_item is not None
    assert batch.pending_approval.resume_item["tool_call_id"] == "call_1"
    assert batch.tool_parts == []


def test_non_interactive_session_blocks_tool_preflight_approval() -> None:
    settings = build_settings()
    session = build_session()
    session.metadata["allow_human_interaction"] = False
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"), workspace_dir=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(PreflightApprovalTool())
    dispatcher = ToolDispatcher(tool_registry, hook_manager)

    batch = asyncio.run(
        dispatcher.execute_tool_calls(
            session=session,
            workspace=workspace,
            agent=AgentState(name="build", allowed_tools=["preflight_approval_tool"]),
            tool_calls=[{"tool_call_id": "call_1", "tool_name": "preflight_approval_tool", "arguments": {}}],
            runtime=runtime,
            config=settings,
        )
    )

    assert batch.pending_approval is not None
    assert batch.pending_approval.request.approval_id == "approval_tool_call_1"
    assert batch.pending_approval.request.reason == "需要审批"
    assert batch.pending_approval.resume_item is not None
    assert batch.pending_approval.resume_item["tool_call_id"] == "call_1"
    assert batch.tool_parts == []


def test_manual_approval_disabled_auto_executes_preflight_approval() -> None:
    settings = build_settings()
    settings.human_in_the_loop.enabled = False
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"), workspace_dir=Path("/tmp/codepilot"))
    runtime = RuntimeHandles(event_bus=EventBus())
    tool_registry = ToolRegistry()
    tool_registry.register(PreflightApprovalTool())
    dispatcher = ToolDispatcher(tool_registry, HookManager())

    batch = asyncio.run(
        dispatcher.execute_tool_calls(
            session=session,
            workspace=workspace,
            agent=AgentState(name="build", allowed_tools=["preflight_approval_tool"]),
            tool_calls=[{"tool_call_id": "call_1", "tool_name": "preflight_approval_tool", "arguments": {}}],
            runtime=runtime,
            config=settings,
        )
    )

    assert batch.pending_approval is None
    assert batch.tool_parts[0].state.status == "completed"


class NoIdToolCallLiteLLMClient(StubLiteLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.tool_call = {"tool_name": "approval_tool", "arguments": {}}

    async def stream_chat(
        self,
        session: SessionState,
        llm_state: Any,
        provider_messages: list[Any],
        tools: list[dict[str, Any]],
        event_bus: EventBus,
    ) -> Any:
        self.calls += 1
        if self.calls > 1:
            return SimpleNamespace(text="工具执行完成", reasoning="", tool_calls=[])
        return SimpleNamespace(text="需要调用工具", reasoning="", tool_calls=[self.tool_call])


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


class BashParseErrorLiteLLMClient(StubLiteLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self._message_builder = LiteLLMClient()

    def build_provider_messages(
        self,
        messages: list[Message],
        system_prompt: str | None = None,
        runtime_context: str | None = None,
    ) -> list[Any]:
        provider_messages = self._message_builder.build_provider_messages(
            messages,
            system_prompt=system_prompt,
            runtime_context=runtime_context,
        )
        self.last_provider_messages = provider_messages
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
        if self.calls == 1:
            return SimpleNamespace(
                text="需要执行命令",
                reasoning="",
                tool_calls=[
                    {
                        "tool_call_id": "call_bad_quote",
                        "tool_name": "bash_tool",
                        "arguments": {"command": "echo 'abc"},
                    }
                ],
            )
        return SimpleNamespace(text="已收到工具错误并修正后继续。", reasoning="", tool_calls=[])


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


def test_agent_loop_reuses_generated_tool_call_id_for_execution() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    llm_client = NoIdToolCallLiteLLMClient()
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

    assistant_message = result.messages[1]
    tool_parts = assistant_message.tool_parts()
    assert len(tool_parts) == 1
    assert tool_parts[0].call_id.startswith(f"call_{assistant_message.info.id}_")
    assert tool_parts[0].state.status == "completed"
    assert llm_client.tool_call["tool_call_id"] == tool_parts[0].call_id


def test_agent_loop_continues_after_preflight_tool_error(tmp_path: Path) -> None:
    settings = build_settings()
    session = build_session()
    session.workspace_path = str(tmp_path)
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=tmp_path, workspace_dir=tmp_path / ".codepilot")
    event_bus = EventBus()
    stream_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(BashTool(settings=BashToolSettings(approval_mode="none"), timeout_seconds=5))
    llm_client = BashParseErrorLiteLLMClient()
    loop = AgentLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        tool_dispatcher=ToolDispatcher(tool_registry, hook_manager),
        hook_manager=hook_manager,
    )

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(
                name="build",
                system_prompt="test",
                allowed_tools=["bash_tool"],
                max_iterations=3,
            ),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert llm_client.calls == 2
    failed_tool = result.messages[1].tool_parts()[0]
    assert failed_tool.state.status == "error"
    assert failed_tool.state.error is not None
    assert failed_tool.state.error.code == "BashCommandParseError"
    assert result.messages[1].info.finish == "tool_completed"
    tool_messages = [message for message in llm_client.last_provider_messages if isinstance(message, dict) and message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "BashCommandParseError" in str(tool_messages[0].get("content"))
    assert [event.event_type for event in stream_events].count("tool_call_failed") == 1
    assert stream_events[-1].event_type == "session_finished"


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
                agent_profile=AgentProfile(name="build", system_prompt="test", allowed_tools=["approval_tool"], max_iterations=3),
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
    tool_part = session.messages[-2].tool_parts()[0]
    assert tool_part.state.status == "error"
    assert tool_part.state.error is not None
    assert tool_part.state.error.code == "ToolApprovalRejected"
    assert tool_part.state.output is not None
    assert tool_part.state.output["error_type"] == "ToolApprovalRejected"
    assert "拒绝执行" in tool_part.state.output["error_message"]
    assert interactions[1].data["message_id"] == session.messages[-2].info.id
    assert interactions[1].data["call_id"] == "call_1"


def test_agent_loop_does_not_emit_approved_tool_completion_when_snapshot_persistence_fails() -> None:
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
    event_bus.subscribe_stream(stream_events.append)

    def fail_completed_tool_snapshot(event: Any) -> None:
        if event.event_type.value != "message_created" or event.message.info.finish != "tool_completed":
            return
        raise RuntimeError("消息快照写入失败")

    event_bus.subscribe_domain(fail_completed_tool_snapshot, critical=True)

    async def resolve_approval() -> None:
        task = asyncio.create_task(
            loop.run(
                session=session,
                workspace=workspace,
                agent_profile=AgentProfile(name="build", system_prompt="test", allowed_tools=["approval_tool"], max_iterations=3),
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
            approved=True,
            created_at="2026-04-30T00:00:01Z",
        )
        approval_event.set()
        await task

    try:
        asyncio.run(resolve_approval())
    except RuntimeError as exc:
        assert str(exc) == "消息快照写入失败"
    else:
        raise AssertionError("快照持久化失败必须中断审批恢复")

    assert not any(
        event.event_type == "assistant_message_completed" and event.data["message"]["info"]["finish"] == "tool_completed"
        for event in stream_events
    )


def test_agent_loop_auto_approves_tool_when_manual_approval_disabled() -> None:
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
    stream_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", allowed_tools=["approval_tool"], max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
            allow_manual_approval=False,
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert "human_approval_required" not in [event.event_type for event in stream_events]
    assert "error" not in [event.event_type for event in stream_events]


def test_agent_loop_fast_approval_reply_does_not_lose_wakeup() -> None:
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

    def reply_immediately(event: Any) -> None:
        if event.event_type.value != "human_interaction" or event.data.get("kind") != "approval":
            return
        if event.data.get("status") != "pending":
            return
        approval_result_holder["result"] = ApprovalResult(
            approval_id=event.data["request"]["approval_id"],
            approved=False,
            comment="同步拒绝",
            created_at="2026-04-30T00:00:01Z",
        )
        approval_event.set()

    event_bus.subscribe_domain(reply_immediately)

    result = asyncio.run(
        asyncio.wait_for(
            loop.run(
                session=session,
                workspace=workspace,
                agent_profile=AgentProfile(name="build", system_prompt="test", allowed_tools=["approval_tool"], max_iterations=3),
                runtime=runtime,
                config=settings,
                approval_event=approval_event,
                approval_result_holder=approval_result_holder,
                stop_event=asyncio.Event(),
            ),
            timeout=1,
        )
    )

    assert result.status == SessionStatus.CANCELLED
    assert result.messages[-1].text_content() == "人工拒绝继续执行：同步拒绝"


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
                allow_manual_approval=False,
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
    completed_event = next(
        event for event in reversed(stream_events)
        if event.event_type == "assistant_message_completed" and event.data["message"]["info"]["finish"] == "tool_completed"
    )
    assert completed_event.data["agent"] == "build"
    assert completed_event.data["agent_kind"] == "agent"
    assert completed_event.data["context_id"] == "main"
    assert completed_event.data["parent_call_id"] is None
    question_message = result.messages[-2]
    question_part = next(part for part in question_message.parts if isinstance(part, ToolPart) and part.tool == "question")
    assert question_part.state.input["question_id"].startswith("question_")
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


def test_non_interactive_agent_loop_fails_before_question_wait() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(QuestionTool(timeout_seconds=1))
    dispatcher = ToolDispatcher(tool_registry, hook_manager)
    loop = AgentLoop(
        llm_client=QuestionToolCallLiteLLMClient(),
        tool_registry=tool_registry,
        tool_dispatcher=dispatcher,
        hook_manager=hook_manager,
    )
    stream_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)
    session.metadata["source"] = "schedule"

    result = asyncio.run(
        loop.run(
            session=session,
            workspace=workspace,
            agent_profile=AgentProfile(name="build", system_prompt="test", allowed_tools=["question"], max_iterations=3),
            runtime=runtime,
            config=settings,
            approval_event=asyncio.Event(),
            approval_result_holder={"result": None},
            stop_event=asyncio.Event(),
            question_event=asyncio.Event(),
            question_result_holder={"result": None},
            allow_question_interaction=False,
        )
    )

    assert result.status == SessionStatus.FAILED
    assert result.metadata["non_interactive_error"] == "定时任务为无人值守运行，不能请求人工输入。"
    assert "human_question_required" not in [event.event_type for event in stream_events]
    assert any(event.event_type == "error" for event in stream_events)


def test_subagent_rejects_question_without_user_answer_panel() -> None:
    settings = build_settings()
    parent_session = build_session()
    parent_session.metadata["allow_question_interaction"] = True
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(QuestionTool(timeout_seconds=1))
    dispatcher = ToolDispatcher(tool_registry, hook_manager)
    loop = AgentLoop(
        llm_client=QuestionToolCallLiteLLMClient(),
        tool_registry=tool_registry,
        tool_dispatcher=dispatcher,
        hook_manager=hook_manager,
    )
    stream_events: list[Any] = []
    event_bus.subscribe_stream(stream_events.append)

    result = asyncio.run(
        loop.run_subagent(
            parent_session=parent_session,
            workspace=workspace,
            agent_profile=AgentProfile(
                name="explore",
                system_prompt="test",
                kind="subagent",
                allowed_tools=["question"],
                max_iterations=3,
            ),
            task="需要澄清目标",
            parent_call_id="call_task_1",
            runtime=runtime,
            config=settings,
            stop_event=asyncio.Event(),
        )
    )

    assert result.status == SessionStatus.FAILED
    assert result.metadata["subagent_error"] == "subagent 不支持向用户提问，请由父 Agent 收集所需信息。"
    assert "human_question_required" not in [event.event_type for event in stream_events]
    assert any(event.event_type == "error" for event in stream_events)


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
    tool_part = result.messages[-2].tool_parts()[0]
    assert tool_part.state.status == "error"
    assert tool_part.state.error is not None
    assert tool_part.state.error.code == "QuestionDeclined"
    assert tool_part.state.output is not None
    assert tool_part.state.output["error_type"] == "QuestionDeclined"
    resolved = next(event for event in stream_events if event.event_type == "human_question_resolved")
    assert resolved.data["declined"] is True
    assert resolved.data["continue_loop"] is False
    LiteLLMClient().build_provider_messages(result.messages[-2:])


def test_agent_loop_fast_question_reply_does_not_lose_wakeup() -> None:
    settings = build_settings()
    session = build_session()
    workspace = SimpleNamespace(workspace_id="ws_1", workspace_path=Path("/tmp/codepilot"))
    event_bus = EventBus()
    runtime = RuntimeHandles(event_bus=event_bus)
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    tool_registry.register(QuestionTool(timeout_seconds=1))
    dispatcher = ToolDispatcher(tool_registry, hook_manager)
    loop = AgentLoop(
        llm_client=QuestionToolCallLiteLLMClient(),
        tool_registry=tool_registry,
        tool_dispatcher=dispatcher,
        hook_manager=hook_manager,
    )
    question_event = asyncio.Event()
    question_result_holder = {"result": None}

    def reply_immediately(event: Any) -> None:
        if event.event_type.value != "human_interaction" or event.data.get("kind") != "question":
            return
        if event.data.get("status") != "pending":
            return
        question_result_holder["result"] = QuestionResult(
            question_id=event.data["request"]["question_id"],
            answers={"target": {"values": ["backend"], "note": ""}},
            created_at="2026-04-30T00:00:01Z",
        )
        question_event.set()

    event_bus.subscribe_domain(reply_immediately)

    result = asyncio.run(
        asyncio.wait_for(
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
            ),
            timeout=1,
        )
    )

    assert result.status == SessionStatus.COMPLETED
    assert result.messages[-2].tool_parts()[0].state.status == "completed"
    assert result.messages[-1].text_content() == "收到答案"


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
