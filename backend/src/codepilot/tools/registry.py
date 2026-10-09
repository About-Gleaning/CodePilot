from __future__ import annotations

from typing import Any

from codepilot.tools.base import BaseTool
from codepilot.session.agent_config import AgentConfigError


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        existing = self._tools.get(tool.spec.name)
        # 两种注册顺序都必须保护代码工具；保留原平台工具之间的注册语义。
        if existing is not None and (getattr(tool, "code_tool_id", None) or getattr(existing, "code_tool_id", None)):
            raise AgentConfigError(f"工具调用名称 {tool.spec.name} 冲突，请修改代码工具的调用名称", code="tool_name_conflict", status=409)
        self._tools[tool.spec.name] = tool

    def get(self, tool_name: str) -> BaseTool | None:
        return self._tools.get(tool_name)

    def disable_all_approvals(self) -> None:
        """worker 无人值守运行时统一关闭工具级人工审批。"""
        for tool in self._tools.values():
            tool.spec.requires_approval = False

    def get_llm_tool_schemas(
        self,
        allowed_tools: list[str] | None = None,
        *,
        agent_profile: Any | None = None,
    ) -> list[dict[str, object]]:
        schemas: list[dict[str, object]] = []
        agent_name = getattr(agent_profile, "name", None)
        agent_readonly = getattr(agent_profile, "readonly", None)
        for name, tool in self._tools.items():
            code_id = getattr(tool, "code_tool_id", None)
            if code_id:
                if code_id not in (getattr(agent_profile, "tool_ids", []) or []):
                    continue
            mcp_server_name = getattr(tool, "mcp_server_name", None)
            mcp_permission = f"mcp:{mcp_server_name}" if mcp_server_name else None
            if allowed_tools is not None:
                if mcp_server_name and mcp_permission not in allowed_tools:
                    continue
                if not mcp_server_name and not code_id and name not in allowed_tools:
                    continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.spec.name,
                        "description": tool.get_llm_description(agent_name=agent_name, agent_readonly=agent_readonly),
                        "parameters": tool.spec.input_schema,
                    },
                }
            )
        return schemas
