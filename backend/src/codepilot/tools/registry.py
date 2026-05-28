from __future__ import annotations

from typing import Any

from codepilot.tools.base import BaseTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.spec.name] = tool

    def get(self, tool_name: str) -> BaseTool | None:
        return self._tools.get(tool_name)

    def get_llm_tool_schemas(
        self,
        allowed_tools: list[str] | None = None,
        *,
        agent_profile: Any | None = None,
    ) -> list[dict[str, object]]:
        schemas: list[dict[str, object]] = []
        agent_name = getattr(agent_profile, "name", None)
        for name, tool in self._tools.items():
            if allowed_tools is not None and name not in allowed_tools:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.spec.name,
                        "description": tool.get_llm_description(agent_name=agent_name),
                        "parameters": tool.spec.input_schema,
                    },
                }
            )
        return schemas
