from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot.session.agents import build_agent_profiles
from codepilot.session.attachments import MAX_IMAGE_ATTACHMENT_BYTES
from codepilot.skills import SkillRegistry
from codepilot.tools import (
    EditFileTool,
    LoadSkillTool,
    QuestionTool,
    ReadFileTool,
    TodoReadTool,
    TodoWriteTool,
    ToolRegistry,
    WebFetchTool,
    WriteFileTool,
    WritePlanTool,
)
from codepilot.tools.base import ToolExecutionContext
from codepilot.tools.webfetch_tool import FetchedPage


# 测试场景说明
# 场景1：正常流程 - 工具按预期读写文件、管理 todo、抽取 URL 核心正文。
# 场景2：边界情况 - 行数限制、空状态、长网页内容截断。
# 场景3：异常处理 - 拒绝越权路径、非法 URL、内网地址和不可抽取网页。


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


def test_read_file_returns_image_attachment_for_png(tmp_path: Path) -> None:
    target = tmp_path / "sample.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-bytes")
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(ReadFileTool(timeout_seconds=1), {"file_path": str(target)}, context)

    assert result["status"] == "ok"
    assert result["mime"] == "image/png"
    assert result["attachments"][0]["source_path"] == str(target)
    assert "已读取图片文件" in str(result["output"])


@pytest.mark.parametrize(
    ("filename", "header", "expected_mime"),
    [
        ("sample.jpg", b"\xff\xd8\xff" + b"image-bytes", "image/jpeg"),
        ("sample.webp", b"RIFFxxxxWEBP" + b"image-bytes", "image/webp"),
        ("sample.gif", b"GIF89a" + b"image-bytes", "image/gif"),
    ],
)
def test_read_file_returns_image_attachment_for_supported_images(
    tmp_path: Path,
    filename: str,
    header: bytes,
    expected_mime: str,
) -> None:
    target = tmp_path / filename
    target.write_bytes(header)
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(ReadFileTool(timeout_seconds=1), {"file_path": str(target)}, context)

    assert result["status"] == "ok"
    assert result["mime"] == expected_mime
    assert result["attachments"][0]["type"] == "image"
    assert result["attachments"][0]["mime"] == expected_mime
    assert result["attachments"][0]["source_path"] == str(target)


def test_read_file_skips_llm_attachment_for_oversized_image(tmp_path: Path) -> None:
    target = tmp_path / "large.png"
    # 只需构造合法图片头和超限字节数，避免依赖真实图片解码。
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * MAX_IMAGE_ATTACHMENT_BYTES)
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(ReadFileTool(timeout_seconds=1), {"file_path": str(target)}, context)

    assert result["status"] == "ok"
    assert result["mime"] == "image/png"
    assert result["attachments"] == []
    assert "未发送给 LLM" in str(result["output"])


def test_read_file_rejects_non_image_binary_file(tmp_path: Path) -> None:
    target = tmp_path / "archive.bin"
    target.write_bytes(b"\x80\x81\x82")
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(ReadFileTool(timeout_seconds=1), {"file_path": str(target)}, context)

    assert result["status"] == "error"
    assert result["error_type"] == "FileEncodingUnsupported"


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


def test_todo_write_and_read_use_session_runtime_file(tmp_path: Path) -> None:
    workspace_dir = tmp_path / ".codepilot"
    context = build_context(tmp_path, workspace_dir)
    todos = [
        {"content": "实现工具", "status": "in_progress", "priority": "high"},
        {"content": "补充测试", "status": "pending", "priority": "medium"},
    ]

    written = run_tool(TodoWriteTool(timeout_seconds=1), {"todos": todos}, context)
    read = run_tool(TodoReadTool(timeout_seconds=1), {}, context)

    todo_path = workspace_dir / "todos" / "session_1.json"
    assert written["status"] == "ok"
    assert written["todo_path"] == str(todo_path)
    assert read["todos"] == todos
    assert todo_path.exists()
    assert not (workspace_dir / "sessions" / "session_1.json").exists()
    assert not (workspace_dir / "plans" / "session_1.md").exists()


def test_todo_write_rejects_multiple_in_progress(tmp_path: Path) -> None:
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(
        TodoWriteTool(timeout_seconds=1),
        {
            "todos": [
                {"content": "a", "status": "in_progress", "priority": "high"},
                {"content": "b", "status": "in_progress", "priority": "low"},
            ]
        },
        context,
    )

    assert result["status"] == "error"
    assert result["error_type"] == "TodoInProgressConflict"


def test_todo_read_reports_empty_state(tmp_path: Path) -> None:
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(TodoReadTool(timeout_seconds=1), {}, context)

    assert result["status"] == "ok"
    assert result["todos"] == []
    assert "还没有 todo" in str(result["output"])


def test_question_rejects_custom_field(tmp_path: Path) -> None:
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(
        QuestionTool(timeout_seconds=1),
        {
            "questions": [
                {
                    "id": "scope",
                    "question": "选择范围",
                    "options": [{"value": "backend", "label": "后端"}],
                    "custom": True,
                }
            ]
        },
        context,
    )

    assert result["status"] == "error"
    assert result["error_type"] == "QuestionUnknownField"


def test_question_rejects_invalid_questions(tmp_path: Path) -> None:
    context = build_context(tmp_path, tmp_path / ".codepilot")

    result = run_tool(QuestionTool(timeout_seconds=1), {"questions": []}, context)

    assert result["status"] == "error"
    assert result["error_type"] == "QuestionInputInvalid"


def test_agent_tool_permissions_are_scoped() -> None:
    profiles = build_agent_profiles(max_iterations=3, subagent_max_iterations=5)

    assert {"bash_tool", "read_file", "write_file", "edit_file"}.issubset(profiles["build"].allowed_tools)
    assert "load_skill" in profiles["build"].allowed_tools
    assert "webfetch" in profiles["build"].allowed_tools
    assert "write_plan" in profiles["plan"].allowed_tools
    assert "load_skill" in profiles["plan"].allowed_tools
    assert "webfetch" in profiles["plan"].allowed_tools
    assert "bash_tool" in profiles["plan"].allowed_tools
    assert "write_file" not in profiles["plan"].allowed_tools
    assert "edit_file" not in profiles["plan"].allowed_tools
    assert {"todo_write", "todo_read", "question"}.issubset(profiles["build"].allowed_tools)
    assert {"todo_write", "todo_read", "question"}.issubset(profiles["plan"].allowed_tools)
    assert profiles["explore"].allowed_tools == ["bash_tool", "read_file", "load_skill", "webfetch"]
    assert profiles["build"].kind == "agent"
    assert profiles["plan"].kind == "agent"
    assert profiles["explore"].kind == "subagent"
    assert "task" in profiles["build"].allowed_tools
    assert "task" in profiles["plan"].allowed_tools
    assert "task" not in profiles["explore"].allowed_tools
    assert profiles["build"].max_iterations == 3
    assert profiles["plan"].max_iterations == 3
    assert profiles["explore"].max_iterations == 5


def test_file_tool_descriptions_are_loaded_into_schema() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool(timeout_seconds=1))
    registry.register(WriteFileTool(timeout_seconds=1))
    registry.register(EditFileTool(timeout_seconds=1))
    registry.register(WritePlanTool(timeout_seconds=1))
    registry.register(TodoWriteTool(timeout_seconds=1))
    registry.register(TodoReadTool(timeout_seconds=1))
    registry.register(QuestionTool(timeout_seconds=1))
    registry.register(WebFetchTool(timeout_seconds=1))

    schemas = registry.get_llm_tool_schemas()

    descriptions = [schema["function"]["description"] for schema in schemas]  # type: ignore[index]
    assert all(isinstance(description, str) and description for description in descriptions)
    assert any("读取 workspace 内" in description for description in descriptions)
    assert any("写入当前 plan agent 会话" in description for description in descriptions)
    assert any("去除导航栏" in description for description in descriptions)


def test_webfetch_extracts_core_markdown_from_html() -> None:
    async def fake_fetch(url: str) -> FetchedPage:
        return FetchedPage(
            final_url=url,
            html="""
            <html>
              <body>
                <nav>首页 文档 登录</nav>
                <main>
                  <article>
                    <h1>核心标题</h1>
                    <p>这是页面最重要的正文内容。</p>
                    <p>第二段包含更多可阅读信息。</p>
                  </article>
                </main>
                <footer>版权信息</footer>
              </body>
            </html>
            """,
        )

    tool = WebFetchTool(timeout_seconds=1)
    tool._resolve_host_addresses = lambda hostname, port: {"93.184.216.34"}  # type: ignore[method-assign]
    tool._fetch_html = fake_fetch  # type: ignore[method-assign]

    result = run_tool(tool, {"url": "https://example.com/post"}, build_context(Path.cwd(), Path.cwd() / ".codepilot"))

    assert result["status"] == "ok"
    assert "核心标题" in str(result["output"])
    assert "这是页面最重要的正文内容" in str(result["output"])
    assert "首页 文档 登录" not in str(result["output"])
    assert "版权信息" not in str(result["output"])


def test_webfetch_rejects_non_http_url() -> None:
    result = run_tool(
        WebFetchTool(timeout_seconds=1),
        {"url": "file:///etc/passwd"},
        build_context(Path.cwd(), Path.cwd() / ".codepilot"),
    )

    assert result["status"] == "error"
    assert result["error_type"] == "WebFetchUrlSchemeUnsupported"


def test_webfetch_rejects_private_host() -> None:
    tool = WebFetchTool(timeout_seconds=1)
    tool._resolve_host_addresses = lambda hostname, port: {"127.0.0.1"}  # type: ignore[method-assign]

    result = run_tool(tool, {"url": "http://localhost:8000"}, build_context(Path.cwd(), Path.cwd() / ".codepilot"))

    assert result["status"] == "error"
    assert result["error_type"] == "WebFetchHostForbidden"


def test_webfetch_truncates_long_output() -> None:
    async def fake_fetch(url: str) -> FetchedPage:
        return FetchedPage(final_url=url, html="<html><body><article><p>正文</p></article></body></html>")

    tool = WebFetchTool(timeout_seconds=1)
    tool._resolve_host_addresses = lambda hostname, port: {"93.184.216.34"}  # type: ignore[method-assign]
    tool._fetch_html = fake_fetch  # type: ignore[method-assign]
    tool._extract_markdown = lambda html, url: "内容" * 30_000  # type: ignore[method-assign]

    result = run_tool(tool, {"url": "https://example.com/long"}, build_context(Path.cwd(), Path.cwd() / ".codepilot"))

    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert len(str(result["output"])) == 50_000


def test_webfetch_reports_empty_extracted_content() -> None:
    async def fake_fetch(url: str) -> FetchedPage:
        return FetchedPage(final_url=url, html="<html><body><nav>只有导航</nav></body></html>")

    tool = WebFetchTool(timeout_seconds=1)
    tool._resolve_host_addresses = lambda hostname, port: {"93.184.216.34"}  # type: ignore[method-assign]
    tool._fetch_html = fake_fetch  # type: ignore[method-assign]
    tool._extract_markdown = lambda html, url: ""  # type: ignore[method-assign]

    result = run_tool(tool, {"url": "https://example.com/empty"}, build_context(Path.cwd(), Path.cwd() / ".codepilot"))

    assert result["status"] == "error"
    assert result["error_type"] == "WebFetchContentEmpty"


def test_tool_descriptions_do_not_reference_stale_schema_terms() -> None:
    descriptions_dir = Path(__file__).resolve().parents[1] / "src" / "codepilot" / "tools" / "descriptions"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in descriptions_dir.glob("*.txt"))

    stale_terms = [
        "filePath",
        "oldString",
        "newString",
        "replaceAll",
        "related_artifacts",
        "list_artifacts",
        "read_artifact",
        '"header"',
        '"description"',
        "cancelled",
        "行号前缀",
    ]
    for term in stale_terms:
        assert term not in combined


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
