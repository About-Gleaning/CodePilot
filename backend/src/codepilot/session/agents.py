from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_system_prompt(agent_name: str) -> str:
    prompt_path = _PROMPT_DIR / f"{agent_name}.md"
    return prompt_path.read_text(encoding="utf-8").strip()


class AgentProfile(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    kind: Literal["agent", "subagent"] = "agent"
    allowed_tools: list[str] = Field(default_factory=list)
    readonly: bool = False
    max_iterations: int = 50
    can_call_subagent: bool = False


def build_agent_profiles(max_iterations: int, subagent_max_iterations: int = 8) -> dict[str, AgentProfile]:
    return {
        "build": AgentProfile(
            name="build",
            description="自主完成代码开发、修复和验证任务。",
            system_prompt=_load_system_prompt("build"),
            kind="agent",
            allowed_tools=[
                "bash_tool",
                "read_file",
                "write_file",
                "edit_file",
                "load_skill",
                "webfetch",
                "todo_write",
                "todo_read",
                "question",
                "task",
            ],
            readonly=False,
            max_iterations=max_iterations,
            can_call_subagent=True,
        ),
        "plan": AgentProfile(
            name="plan",
            description="制定只读执行计划，并在计划模式下沉淀方案。",
            system_prompt=_load_system_prompt("plan"),
            kind="agent",
            allowed_tools=[
                "bash_tool",
                "read_file",
                "write_plan",
                "load_skill",
                "webfetch",
                "todo_write",
                "todo_read",
                "question",
                "task",
            ],
            readonly=True,
            max_iterations=max_iterations,
            can_call_subagent=True,
        ),
        "life": AgentProfile(
            name="life",
            description="用户的生活助手",
            system_prompt=_load_system_prompt("life"),
            kind="agent",
            allowed_tools=[
                "bash_tool",
                "read_file",
                "write_file",
                "edit_file",
                "load_skill",
                "webfetch",
                "todo_write",
                "todo_read",
                "question",
                "task",
            ],
            readonly=False,
            max_iterations=max_iterations,
            can_call_subagent=True,
        ),
        "explore": AgentProfile(
            name="explore",
            description="只读文件搜索、代码定位和上下文探查专家。",
            system_prompt=_load_system_prompt("explore"),
            kind="subagent",
            allowed_tools=["bash_tool", "read_file", "load_skill", "webfetch"],
            readonly=True,
            max_iterations=subagent_max_iterations,
            can_call_subagent=False,
        ),
    }
