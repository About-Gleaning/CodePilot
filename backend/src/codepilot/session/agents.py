from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_system_prompt(agent_name: str) -> str:
    prompt_path = _PROMPT_DIR / f"{agent_name}.md"
    return prompt_path.read_text(encoding="utf-8").strip()


class AgentProfile(BaseModel):
    name: str
    system_prompt: str
    allowed_tools: list[str] = Field(default_factory=list)
    readonly: bool = False
    max_iterations: int = 50
    can_call_subagent: bool = False


def build_agent_profiles(max_iterations: int) -> dict[str, AgentProfile]:
    return {
        "build": AgentProfile(
            name="build",
            system_prompt=_load_system_prompt("build"),
            allowed_tools=["bash_tool", "read_file", "write_file", "edit_file", "load_skill", "todo_write", "todo_read", "question"],
            readonly=False,
            max_iterations=max_iterations,
            can_call_subagent=True,
        ),
        "plan": AgentProfile(
            name="plan",
            system_prompt=_load_system_prompt("plan"),
            allowed_tools=["bash_tool", "read_file", "write_plan", "load_skill", "todo_write", "todo_read", "question"],
            readonly=True,
            max_iterations=max_iterations,
            can_call_subagent=True,
        ),
        "explore": AgentProfile(
            name="explore",
            system_prompt=_load_system_prompt("explore"),
            allowed_tools=["bash_tool", "read_file", "load_skill", "question"],
            readonly=True,
            max_iterations=max_iterations,
            can_call_subagent=False,
        ),
    }
