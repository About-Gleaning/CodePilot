from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MessageRole = Literal["user", "assistant"]
HookMessageRole = Literal["user", "assistant"]
ToolPartStatus = Literal["pending", "running", "completed", "error"]


class UserMessageTimeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: int


class AssistantMessageTimeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: int
    completed: int | None = None


class PartTimeInfo(BaseModel):
    start: str | None = None
    end: str | None = None
    created: str | None = None


class MessageTokenCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read: int | None = None
    write: int | None = None


class AssistantMessageTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: int | None = None
    output: int | None = None
    reasoning: int | None = None
    cache: MessageTokenCache | None = None


class AssistantMessageError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class FileSource(BaseModel):
    type: Literal["file", "symbol", "resource"]
    value: str
    start: int | None = None
    end: int | None = None


class MessageModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str


class AssistantMessagePath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str
    root: str


class BaseMessageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str


class UserMessageInfo(BaseMessageInfo):
    role: Literal["user"] = "user"
    time: UserMessageTimeInfo
    agent: str
    model: MessageModelRef


class AssistantMessageInfo(BaseMessageInfo):
    role: Literal["assistant"] = "assistant"
    time: AssistantMessageTimeInfo
    error: AssistantMessageError | None = None
    parent_id: str
    model: MessageModelRef
    agent: str
    path: AssistantMessagePath
    cost: float | None = None
    tokens: AssistantMessageTokens | None = None
    finish: str | None = None


class BaseMessagePart(BaseModel):
    id: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TextPart(BaseMessagePart):
    type: Literal["text"] = "text"
    text: str
    synthetic: bool = False
    ignored: bool = False
    time: PartTimeInfo = Field(default_factory=PartTimeInfo)


class ReasoningPart(BaseMessagePart):
    type: Literal["reasoning"] = "reasoning"
    text: str
    time: PartTimeInfo = Field(default_factory=PartTimeInfo)


class ToolPartState(BaseModel):
    status: ToolPartStatus
    input: dict[str, Any] = Field(default_factory=dict)
    raw: str | None = None
    title: str | None = None
    output: dict[str, Any] | None = None
    error: AssistantMessageError | None = None
    time: PartTimeInfo = Field(default_factory=PartTimeInfo)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class ToolPart(BaseMessagePart):
    type: Literal["tool"] = "tool"
    call_id: str
    tool: str
    state: ToolPartState


class FilePart(BaseMessagePart):
    type: Literal["file"] = "file"
    mime: str
    filename: str
    url: str | None = None
    source: FileSource | None = None


class StepStartPart(BaseMessagePart):
    type: Literal["step-start"] = "step-start"
    snapshot: str | None = None


class StepFinishPart(BaseMessagePart):
    type: Literal["step-finish"] = "step-finish"
    reason: str | None = None
    snapshot: str | None = None
    cost: float | None = None
    tokens: AssistantMessageTokens | None = None


class SnapshotPart(BaseMessagePart):
    type: Literal["snapshot"] = "snapshot"
    snapshot: str


class PatchPart(BaseMessagePart):
    type: Literal["patch"] = "patch"
    hash: str | None = None
    files: list[str] = Field(default_factory=list)


class AgentMentionSource(BaseModel):
    value: str
    start: int | None = None
    end: int | None = None


class AgentPart(BaseMessagePart):
    type: Literal["agent"] = "agent"
    name: str
    source: AgentMentionSource


class SubtaskPart(BaseMessagePart):
    type: Literal["subtask"] = "subtask"
    prompt: str
    description: str | None = None
    agent: str | None = None
    model: MessageModelRef | None = None
    command: str | None = None


class RetryPart(BaseMessagePart):
    type: Literal["retry"] = "retry"
    attempt: int
    error: AssistantMessageError | None = None
    time: PartTimeInfo = Field(default_factory=PartTimeInfo)


class CompactionPart(BaseMessagePart):
    type: Literal["compaction"] = "compaction"
    auto: bool = False


MessagePart = Annotated[
    TextPart
    | ReasoningPart
    | ToolPart
    | FilePart
    | StepStartPart
    | StepFinishPart
    | SnapshotPart
    | PatchPart
    | AgentPart
    | SubtaskPart
    | RetryPart
    | CompactionPart,
    Field(discriminator="type"),
]


MessageInfo = Annotated[UserMessageInfo | AssistantMessageInfo, Field(discriminator="role")]


class Message(BaseModel):
    info: MessageInfo
    parts: list[MessagePart] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hook_message_role(self) -> "Message":
        if self.info.role not in {"user", "assistant"}:
            raise ValueError("message role 仅允许 user 或 assistant")
        return self

    def text_content(self) -> str:
        texts = [
            part.text
            for part in self.parts
            if isinstance(part, TextPart) and not part.ignored and part.text
        ]
        return "\n".join(texts)

    def iter_parts(self, part_type: str) -> list[MessagePart]:
        return [part for part in self.parts if getattr(part, "type", None) == part_type]

    def tool_parts(self) -> list[ToolPart]:
        return [part for part in self.parts if isinstance(part, ToolPart)]


def build_user_message_info(
    *,
    message_id: str,
    session_id: str,
    created_at_ms: int,
    agent: str,
    provider_id: str,
    model_id: str,
) -> UserMessageInfo:
    return UserMessageInfo(
        id=message_id,
        session_id=session_id,
        time=UserMessageTimeInfo(created=created_at_ms),
        agent=agent,
        model=MessageModelRef(provider_id=provider_id, model_id=model_id),
    )


def build_assistant_message_info(
    *,
    message_id: str,
    session_id: str,
    created_at_ms: int,
    parent_id: str,
    agent: str,
    provider_id: str,
    model_id: str,
    cwd: str,
    root: str,
) -> AssistantMessageInfo:
    return AssistantMessageInfo(
        id=message_id,
        session_id=session_id,
        time=AssistantMessageTimeInfo(created=created_at_ms),
        parent_id=parent_id,
        agent=agent,
        model=MessageModelRef(provider_id=provider_id, model_id=model_id),
        path=AssistantMessagePath(cwd=cwd, root=root),
    )
