"""会话消息的数据模型，定义前后端持久化和流式事件共享的消息结构。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MessageRole = Literal["user", "assistant"]
HookMessageRole = Literal["user", "assistant"]
ToolPartStatus = Literal["pending", "running", "completed", "error"]


class UserMessageTimeInfo(BaseModel):
    """用户消息的时间信息，created 使用毫秒时间戳。"""

    model_config = ConfigDict(extra="forbid")

    created: int


class AssistantMessageTimeInfo(BaseModel):
    """助手消息的时间信息，completed 为空表示响应尚未结束。"""

    model_config = ConfigDict(extra="forbid")

    created: int
    completed: int | None = None


class PartTimeInfo(BaseModel):
    """消息片段的时间信息，兼容工具流式执行和历史事件回放。"""

    start: str | None = None
    end: str | None = None
    created: str | None = None


class MessageTokenCache(BaseModel):
    """模型缓存命中和写入的 token 统计。"""

    model_config = ConfigDict(extra="forbid")

    read: int | None = None
    write: int | None = None


class AssistantMessageTokens(BaseModel):
    """助手响应的 token 用量统计，字段为空表示对应供应商未返回该指标。"""

    model_config = ConfigDict(extra="forbid")

    input: int | None = None
    output: int | None = None
    reasoning: int | None = None
    cache: MessageTokenCache | None = None


class AssistantMessageError(BaseModel):
    """助手响应或工具调用失败时的结构化错误。"""

    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class FileSource(BaseModel):
    """文件片段的来源定位，可指向文件、符号或外部资源。"""

    type: Literal["file", "symbol", "resource"]
    value: str
    start: int | None = None
    end: int | None = None


class MessageModelRef(BaseModel):
    """生成消息时使用的模型引用。"""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str


class AssistantMessagePath(BaseModel):
    """助手响应所在的工作路径，用于恢复执行上下文。"""

    model_config = ConfigDict(extra="forbid")

    cwd: str
    root: str


class BaseMessageInfo(BaseModel):
    """消息级公共元信息，所有消息都必须绑定会话和消息 ID。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str


class UserMessageInfo(BaseMessageInfo):
    """用户消息元信息。"""

    role: Literal["user"] = "user"
    time: UserMessageTimeInfo
    agent: str
    model: MessageModelRef


class AssistantMessageInfo(BaseMessageInfo):
    """助手消息元信息，包含父消息、模型、路径和最终统计。"""

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
    """消息片段的公共元信息，用于把内容、工具和附件统一挂到一条消息下。"""

    id: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TextPart(BaseMessagePart):
    """文本内容片段，用于承载用户输入、助手回复以及系统合成的可展示文本。"""

    type: Literal["text"] = "text"
    text: str
    synthetic: bool = False
    ignored: bool = False
    time: PartTimeInfo = Field(default_factory=PartTimeInfo)


class ReasoningPart(BaseMessagePart):
    """推理过程片段，用于记录模型的思考摘要或推理文本，便于前端分区展示。"""

    type: Literal["reasoning"] = "reasoning"
    text: str
    time: PartTimeInfo = Field(default_factory=PartTimeInfo)


class ToolPartState(BaseModel):
    """工具调用状态，承载输入、输出、错误和附件等运行期信息。"""

    status: ToolPartStatus
    input: dict[str, Any] = Field(default_factory=dict)
    raw: str | None = None
    title: str | None = None
    output: dict[str, Any] | None = None
    error: AssistantMessageError | None = None
    time: PartTimeInfo = Field(default_factory=PartTimeInfo)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class ToolPart(BaseMessagePart):
    """工具调用片段，用于记录一次工具请求从发起、运行到完成或失败的完整状态。"""

    type: Literal["tool"] = "tool"
    call_id: str
    tool: str
    state: ToolPartState


class FilePart(BaseMessagePart):
    """文件附件片段，用于关联用户上传、工具生成或代码符号定位到的文件资源。"""

    type: Literal["file"] = "file"
    mime: str
    filename: str
    url: str | None = None
    source: FileSource | None = None


class StepStartPart(BaseMessagePart):
    """步骤开始片段，用于标记一次助手执行步骤的边界。"""

    type: Literal["step-start"] = "step-start"
    snapshot: str | None = None


class StepFinishPart(BaseMessagePart):
    """步骤结束片段，用于记录步骤完成原因、成本和 token 统计。"""

    type: Literal["step-finish"] = "step-finish"
    reason: str | None = None
    snapshot: str | None = None
    cost: float | None = None
    tokens: AssistantMessageTokens | None = None


class SnapshotPart(BaseMessagePart):
    """快照片段，用于保存会话状态或上下文压缩前后的文本快照。"""

    type: Literal["snapshot"] = "snapshot"
    snapshot: str


class PatchPart(BaseMessagePart):
    """补丁片段，用于描述一次代码改动涉及的文件集合及可追踪的补丁哈希。"""

    type: Literal["patch"] = "patch"
    hash: str | None = None
    files: list[str] = Field(default_factory=list)


class AgentMentionSource(BaseModel):
    """用户文本中 Agent 提及的原始位置。"""

    value: str
    start: int | None = None
    end: int | None = None


class AgentPart(BaseMessagePart):
    """Agent 提及片段，用于保留用户消息中指向特定 Agent 的提及位置和名称。"""

    type: Literal["agent"] = "agent"
    name: str
    source: AgentMentionSource


class SubtaskPart(BaseMessagePart):
    """子任务片段，用于描述需要交给指定 Agent、模型或命令继续处理的任务。"""

    type: Literal["subtask"] = "subtask"
    prompt: str
    description: str | None = None
    agent: str | None = None
    model: MessageModelRef | None = None
    command: str | None = None


class RetryPart(BaseMessagePart):
    """重试片段，用于记录助手响应失败后的重试次数、错误原因和时间信息。"""

    type: Literal["retry"] = "retry"
    attempt: int
    error: AssistantMessageError | None = None
    time: PartTimeInfo = Field(default_factory=PartTimeInfo)


class CompactionPart(BaseMessagePart):
    """上下文压缩片段，用于标记一次自动或手动触发的历史消息压缩。"""

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
"""所有可挂载到消息上的片段类型，依赖 type 字段进行反序列化分发。"""


MessageInfo = Annotated[UserMessageInfo | AssistantMessageInfo, Field(discriminator="role")]
"""用户消息和助手消息的元信息联合类型，依赖 role 字段进行反序列化分发。"""


class Message(BaseModel):
    """一条完整会话消息，由消息元信息和有序片段列表组成。"""

    info: MessageInfo
    parts: list[MessagePart] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hook_message_role(self) -> "Message":
        """校验消息角色，避免外部 hook 写入当前协议不支持的角色。"""

        if self.info.role not in {"user", "assistant"}:
            raise ValueError("message role 仅允许 user 或 assistant")
        return self

    def text_content(self) -> str:
        """拼接未忽略的文本片段，供上下文构造和展示摘要复用。"""

        texts = [
            part.text
            for part in self.parts
            if isinstance(part, TextPart) and not part.ignored and part.text
        ]
        return "\n".join(texts)

    def iter_parts(self, part_type: str) -> list[MessagePart]:
        """按片段 type 返回匹配的片段列表。"""

        return [part for part in self.parts if getattr(part, "type", None) == part_type]

    def tool_parts(self) -> list[ToolPart]:
        """返回消息中所有工具调用片段。"""

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
    """构造用户消息元信息，集中保持默认字段和模型引用格式一致。"""

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
    """构造助手消息元信息，集中保持父消息、路径和模型引用格式一致。"""

    return AssistantMessageInfo(
        id=message_id,
        session_id=session_id,
        time=AssistantMessageTimeInfo(created=created_at_ms),
        parent_id=parent_id,
        agent=agent,
        model=MessageModelRef(provider_id=provider_id, model_id=model_id),
        path=AssistantMessagePath(cwd=cwd, root=root),
    )
