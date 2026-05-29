from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from codepilot.session.agents import build_agent_profiles
from codepilot.skills import SkillRegistry
from codepilot.tools import EditFileTool, LoadSkillTool, ReadFileTool, ToolRegistry, WriteFileTool, WritePlanTool
from codepilot.tools.base import ToolExecutionContext


def build_context(workspace_path: Path, workspace_dir: Path, *, agent_name: str = "build") -> ToolExecutionContext:
    return ToolExecutionContext(
        session=SimpleNamespace(session_id="session_1"),
        workspace=SimpleNamespace(workspace_path=workspace_path, workspace_dir=workspace_dir),
        agent=SimpleNamespace(name=agent_name),
    )


def run_tool(tool: object, args: dict[str, object], context: ToolExecutionContext) -> dict[str, object]:
    return asyncio.run(tool.execute(args, context=context))  # type: ignore[attr-defined]


def test_read_file_supports_real_absolute_path(tmp_path: Path) -> None:
    target = tmp_path / "backend" / "config.yaml"
    target.parent.mkdir()
    target.write_text("server:\n  port: 8000\n", encoding="utf-8")
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(ReadFileTool(timeout_seconds=1), {"file_path": str(target)}, context)

    assert result["status"] == "ok"
    assert result["file_path"] == str(target)
    assert "port: 8000" in str(result["output"])


def test_read_file_supports_workspace_root_absolute_path(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(ReadFileTool(timeout_seconds=1), {"file_path": "/sample.txt", "offset": 1, "limit": 1}, context)

    assert result["status"] == "ok"
    assert result["output"] == "第二行\n... (1 more lines)"


def test_read_file_rejects_existing_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(ReadFileTool(timeout_seconds=1), {"file_path": str(outside)}, context)

    assert result["status"] == "error"
    assert result["error_type"] == "FilePathForbidden"
    assert result.get("output") is None


def test_write_file_creates_new_file_and_rejects_overwrite(tmp_path: Path) -> None:
    context = build_context(tmp_path, tmp_path / ".codepilot")
    tool = WriteFileTool(timeout_seconds=1)

    created = run_tool(tool, {"file_path": "/src/new.txt", "content": "hello"}, context)
    duplicate = run_tool(tool, {"file_path": "/src/new.txt", "content": "again"}, context)

    assert created["status"] == "ok"
    assert (tmp_path / "src" / "new.txt").read_text(encoding="utf-8") == "hello"
    assert duplicate["status"] == "error"
    assert duplicate["error_type"] == "FileAlreadyExists"


def test_edit_file_replaces_unique_text_and_returns_diff(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("name = 'old'\nprint(name)\n", encoding="utf-8")
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(
        EditFileTool(timeout_seconds=1),
        {"file_path": str(target), "old_string": "old", "new_string": "new"},
        context,
    )

    assert result["status"] == "ok"
    assert result["replaced_count"] == 1
    assert "-name = 'old'" in str(result["diff"])
    assert "+name = 'new'" in str(result["diff"])
    assert target.read_text(encoding="utf-8") == "name = 'new'\nprint(name)\n"


def test_edit_file_requires_unique_match_by_default(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(
        EditFileTool(timeout_seconds=1),
        {"file_path": str(target), "old_string": "value", "new_string": "item"},
        context,
    )

    assert result["status"] == "error"
    assert result["error_type"] == "EditMatchNotUnique"


def test_edit_file_replace_all_updates_every_match(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(
        EditFileTool(timeout_seconds=1),
        {"file_path": "/app.py", "old_string": "value", "new_string": "item", "replace_all": True},
        context,
    )

    assert result["status"] == "ok"
    assert result["replaced_count"] == 2
    assert target.read_text(encoding="utf-8") == "item = 1\nitem = 1\n"


def test_write_plan_writes_fixed_session_plan_file(tmp_path: Path) -> None:
    workspace_dir = tmp_path / ".codepilot"
    context = build_context(tmp_path, workspace_dir, agent_name="plan")

    result = run_tool(WritePlanTool(timeout_seconds=1), {"content": "# 执行计划\n"}, context)

    plan_path = workspace_dir / "plans" / "session_1.md"
    assert result["status"] == "ok"
    assert result["plan_path"] == str(plan_path)
    assert plan_path.read_text(encoding="utf-8") == "# 执行计划\n"


def test_write_plan_rejects_non_plan_agent(tmp_path: Path) -> None:
    context = build_context(tmp_path, tmp_path / ".codepilot", agent_name="build")

    result = run_tool(WritePlanTool(timeout_seconds=1), {"content": "# 执行计划\n"}, context)

    assert result["status"] == "error"
    assert result["error_type"] == "PlanToolAgentForbidden"


def test_agent_tool_permissions_are_scoped() -> None:
    profiles = build_agent_profiles(max_iterations=3)

    assert {"bash_tool", "read_file", "write_file", "edit_file"}.issubset(profiles["build"].allowed_tools)
    assert "load_skill" in profiles["build"].allowed_tools
    assert "write_plan" in profiles["plan"].allowed_tools
    assert "load_skill" in profiles["plan"].allowed_tools
    assert "bash_tool" in profiles["plan"].allowed_tools
    assert "write_file" not in profiles["plan"].allowed_tools
    assert "edit_file" not in profiles["plan"].allowed_tools
    assert profiles["explore"].allowed_tools == ["bash_tool", "read_file", "load_skill"]


def test_file_tool_descriptions_are_loaded_into_schema() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool(timeout_seconds=1))
    registry.register(WriteFileTool(timeout_seconds=1))
    registry.register(EditFileTool(timeout_seconds=1))
    registry.register(WritePlanTool(timeout_seconds=1))

    schemas = registry.get_llm_tool_schemas()

    descriptions = [schema["function"]["description"] for schema in schemas]  # type: ignore[index]
    assert all(isinstance(description, str) and description for description in descriptions)
    assert any("读取当前工作区内" in description for description in descriptions)
    assert any("写入当前 plan 会话" in description for description in descriptions)


def test_load_skill_loads_full_skill_content(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "demo"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示 skill\n---\n# Demo\n按规范执行。\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(skills_root)
    registry.discover()
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(LoadSkillTool(registry=registry, timeout_seconds=1), {"name": "DEMO"}, context)

    assert result["status"] == "ok"
    assert result["name"] == "demo"
    assert "## Skill: demo" in str(result["output"])
    assert "按规范执行" in str(result["output"])


def test_load_skill_rejects_empty_name(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    registry.discover()
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(LoadSkillTool(registry=registry, timeout_seconds=1), {"name": ""}, context)

    assert result["status"] == "error"
    assert result["error_type"] == "SkillNameInvalid"


def test_load_skill_reports_missing_skill(tmp_path: Path) -> None:
    registry = SkillRegistry(tmp_path / "skills")
    registry.discover()
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(LoadSkillTool(registry=registry, timeout_seconds=1), {"name": "missing"}, context)

    assert result["status"] == "error"
    assert result["error_type"] == "SkillNotFound"


def test_load_skill_tool_description_does_not_embed_skill_catalog(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "demo"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示 skill\n---\n# Demo\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(skills_root)
    registry.discover()
    tool = LoadSkillTool(registry=registry, timeout_seconds=1)

    assert "演示 skill" not in tool.get_llm_description()
