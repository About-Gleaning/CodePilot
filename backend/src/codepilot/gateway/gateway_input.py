from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from codepilot.session.attachments import (
    MAX_IMAGE_ATTACHMENT_BASE64_CHARS,
    MAX_IMAGE_ATTACHMENTS_PER_MESSAGE,
    SUPPORTED_IMAGE_MIMES,
)

MAX_USER_CONTENT_CHARS = 100_000
MAX_STRUCTURED_INPUT_CHARS = 100_000


class GatewayInputType(str, Enum):
    """Gateway 层支持的输入类型，用于把前端操作分发到不同会话控制分支。"""

    USER_MESSAGE = "user_message"
    HUMAN_REPLY = "human_reply"
    QUESTION_REPLY = "question_reply"
    QUESTION_DECLINE = "question_decline"
    STOP = "stop"


class UploadedAttachmentInput(BaseModel):
    """前端上传的附件输入；首期只允许图片。"""

    filename: str = Field(min_length=1, max_length=255, description="原始文件名，仅用于展示和生成安全落盘文件名。")
    mime: str = Field(min_length=1, max_length=64, description="浏览器识别到的 MIME 类型。")
    data_base64: str = Field(
        min_length=1,
        max_length=MAX_IMAGE_ATTACHMENT_BASE64_CHARS,
        description="附件内容的 base64 编码，不应写入会话持久化记录。",
    )


class GatewayInput(BaseModel):
    """前端进入后端运行时的统一输入协议。

    GatewayInput 只描述“用户想做什么”和必要上下文，不直接承载运行结果。
    真正的状态流转由 SessionRunner 处理，运行过程通过 SSE 事件回传前端。
    """

    type: GatewayInputType = Field(description="输入类型，决定本次请求是用户消息、人工审批回复还是停止任务。")
    session_id: str | None = Field(default=None, description="目标会话 ID；为空表示创建新会话，非空表示继续当前已加载会话。")
    content: str | None = Field(
        default=None,
        max_length=MAX_USER_CONTENT_CHARS,
        description="用户输入的任务文本，仅 user_message 类型必填。",
    )
    agent_name: str | None = Field(default=None, description="本次执行使用的 Agent 名称，仅 user_message 类型必填。")
    provider: str | None = Field(default=None, description="本次执行使用的 LLM 厂商 ID，仅 user_message 类型必填。")
    model: str | None = Field(default=None, description="本次执行使用的模型 ID，仅 user_message 类型必填。")
    approval_id: str | None = Field(default=None, description="待处理的人工审批请求 ID，仅 human_reply 类型必填。")
    approved: bool | None = Field(default=None, description="人工审批结果；true 表示同意，false 表示拒绝。")
    question_id: str | None = Field(default=None, description="待处理的用户问题请求 ID，仅 question_reply/question_decline 类型必填。")
    answers: dict[str, Any] | None = Field(default=None, description="用户对 question 工具问题的回答。")
    comment: str | None = Field(
        default=None,
        max_length=2_000,
        description="人工审批备注，用于记录同意或拒绝的补充说明。",
    )
    attachments: list[UploadedAttachmentInput] = Field(
        default_factory=list,
        max_length=MAX_IMAGE_ATTACHMENTS_PER_MESSAGE,
        description="用户消息携带的附件，首期仅支持图片。",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="预留扩展元数据，传递非核心输入上下文。")

    @model_validator(mode="after")
    def validate_by_type(self) -> "GatewayInput":
        """按输入类型校验必填字段，避免无效请求进入会话编排层。"""
        if self.type == GatewayInputType.USER_MESSAGE:
            if not self.content:
                raise ValueError("user_message 必须提供 content")
            if not self.agent_name:
                raise ValueError("user_message 必须提供 agent_name")
            if bool(self.provider) != bool(self.model):
                raise ValueError("provider 与 model 必须同时提供")
            if len(self.attachments) > MAX_IMAGE_ATTACHMENTS_PER_MESSAGE:
                raise ValueError(f"单条消息最多上传 {MAX_IMAGE_ATTACHMENTS_PER_MESSAGE} 张图片")
            for attachment in self.attachments:
                if attachment.mime not in SUPPORTED_IMAGE_MIMES:
                    raise ValueError("当前仅支持 png/jpeg/webp/gif 图片附件")
        elif self.attachments:
            raise ValueError("只有 user_message 支持 attachments")
        if self.type == GatewayInputType.HUMAN_REPLY:
            if not self.approval_id:
                raise ValueError("human_reply 必须提供 approval_id")
            if self.approved is None:
                raise ValueError("human_reply 必须提供 approved")
        if self.type == GatewayInputType.QUESTION_REPLY:
            if not self.question_id:
                raise ValueError("question_reply 必须提供 question_id")
            if self.answers is None:
                raise ValueError("question_reply 必须提供 answers")
        if self.type == GatewayInputType.QUESTION_DECLINE and not self.question_id:
            raise ValueError("question_decline 必须提供 question_id")
        if _json_size(self.metadata) > MAX_STRUCTURED_INPUT_CHARS:
            raise ValueError("metadata 内容过大")
        if self.answers is not None and _json_size(self.answers) > MAX_STRUCTURED_INPUT_CHARS:
            raise ValueError("answers 内容过大")
        return self


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
