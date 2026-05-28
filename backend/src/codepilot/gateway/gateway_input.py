from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class GatewayInputType(str, Enum):
    """Gateway 层支持的输入类型，用于把前端操作分发到不同会话控制分支。"""

    USER_MESSAGE = "user_message"
    HUMAN_REPLY = "human_reply"
    STOP = "stop"


class GatewayInput(BaseModel):
    """前端进入后端运行时的统一输入协议。

    GatewayInput 只描述“用户想做什么”和必要上下文，不直接承载运行结果。
    真正的状态流转由 SessionRunner 处理，运行过程通过 SSE 事件回传前端。
    """

    type: GatewayInputType = Field(description="输入类型，决定本次请求是用户消息、人工审批回复还是停止任务。")
    session_id: str | None = Field(default=None, description="目标会话 ID；为空表示创建新会话，非空表示继续当前已加载会话。")
    content: str | None = Field(default=None, description="用户输入的任务文本，仅 user_message 类型必填。")
    agent_name: str | None = Field(default=None, description="本次执行使用的 Agent 名称，仅 user_message 类型必填。")
    provider: str | None = Field(default=None, description="本次执行使用的 LLM 厂商 ID，仅 user_message 类型必填。")
    model: str | None = Field(default=None, description="本次执行使用的模型 ID，仅 user_message 类型必填。")
    approval_id: str | None = Field(default=None, description="待处理的人工审批请求 ID，仅 human_reply 类型必填。")
    approved: bool | None = Field(default=None, description="人工审批结果；true 表示同意，false 表示拒绝。")
    comment: str | None = Field(default=None, description="人工审批备注，用于记录同意或拒绝的补充说明。")
    metadata: dict[str, Any] = Field(default_factory=dict, description="预留扩展元数据，传递非核心输入上下文。")

    @model_validator(mode="after")
    def validate_by_type(self) -> "GatewayInput":
        """按输入类型校验必填字段，避免无效请求进入会话编排层。"""
        if self.type == GatewayInputType.USER_MESSAGE:
            if not self.content:
                raise ValueError("user_message 必须提供 content")
            if not self.agent_name:
                raise ValueError("user_message 必须提供 agent_name")
            if not self.provider:
                raise ValueError("user_message 必须提供 provider")
            if not self.model:
                raise ValueError("user_message 必须提供 model")
        if self.type == GatewayInputType.HUMAN_REPLY:
            if not self.approval_id:
                raise ValueError("human_reply 必须提供 approval_id")
            if self.approved is None:
                raise ValueError("human_reply 必须提供 approved")
        return self
