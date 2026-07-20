from __future__ import annotations

import base64
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from pydantic import ValidationError

from codepilot.config.settings import AppSettings, McpSettings, McpStdioServerSettings, McpStreamableHttpServerSettings
from codepilot.tools import McpClientManager, ToolExecutionContext, ToolRegistry
from codepilot.tools.mcp import _open_session, _resolve_stdio_cwd, build_mcp_tool_name


# 测试场景说明
# 场景1：正常流程 - 发现 MCP 工具后按 Agent Markdown 的 server 权限暴露并调用。
# 场景2：边界情况 - 名称规范化、输出截断、图片附件和故障 server 隔离。
# 场景3：异常处理 - 未授权调用、远端错误、断线重连和非法配置。


class FakeSession:
    def __init__(self, *, fail_calls: int = 0, is_error: bool = False, image_data: str | None = None) -> None:
        self.fail_calls = fail_calls
        self.is_error = is_error
        self.image_data = image_data
        self.initialize_count = 0

    async def initialize(self) -> None:
        self.initialize_count += 1

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="create.issue",
                    description="创建事项",
                    inputSchema={"type": "object", "properties": {"title": {"type": "string"}}},
                )
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.fail_calls > 0:
            self.fail_calls -= 1
            raise ConnectionError("连接断开")
        content = [SimpleNamespace(type="text", text=f"{name}:{arguments['title']}")]
        if self.image_data is not None:
            content.append(SimpleNamespace(type="image", data=self.image_data, mimeType="image/png"))
        return SimpleNamespace(
            content=content,
            structuredContent={"created": not self.is_error},
            isError=self.is_error,
        )


def _settings(*, requires_approval: bool = False) -> McpSettings:
    return McpSettings(
        servers={
            "github": McpStdioServerSettings(
                transport="stdio",
                command="fake",
                requires_approval=requires_approval,
            )
        }
    )


def _context(tmp_path: Any, *, allowed_tools: list[str]) -> ToolExecutionContext:
    return ToolExecutionContext(
        session=SimpleNamespace(session_id="session-1"),
        workspace=SimpleNamespace(workspace_dir=tmp_path, workspace_path=tmp_path),
        agent=SimpleNamespace(name="build", allowed_tools=allowed_tools),
        tool_call_id="call-1",
    )


@pytest.mark.asyncio
async def test_mcp_discovery_exposes_namespaced_tool_by_server_permission(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    session = FakeSession()

    @asynccontextmanager
    async def fake_open_session(config: Any, workspace_path: Any) -> AsyncIterator[FakeSession]:
        yield session

    monkeypatch.setattr("codepilot.tools.mcp._open_session", fake_open_session)
    registry = ToolRegistry()
    manager = McpClientManager(
        settings=_settings(),
        workspace=SimpleNamespace(workspace_path=tmp_path, workspace_dir=tmp_path),
        tool_registry=registry,
    )

    await manager.start()
    try:
        tool_name = build_mcp_tool_name("github", "create.issue")
        tool = registry.get(tool_name)
        assert tool is not None
        assert registry.get_llm_tool_schemas(["read_file"]) == []
        assert registry.get_llm_tool_schemas([tool_name]) == []
        schemas = registry.get_llm_tool_schemas(["mcp:github"])
        assert [schema["function"]["name"] for schema in schemas] == [tool_name]  # type: ignore[index]
        assert schemas[0]["function"]["parameters"]["properties"]["title"]["type"] == "string"  # type: ignore[index]

        result = await tool.execute({"title": "测试"}, context=_context(tmp_path, allowed_tools=["mcp:github"]))
        assert result["status"] == "ok"
        assert result["structured_content"] == {"created": True}
        assert result["content"][0]["text"] == "create.issue:测试"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_adapter_rejects_agent_without_server_permission(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    @asynccontextmanager
    async def fake_open_session(config: Any, workspace_path: Any) -> AsyncIterator[FakeSession]:
        yield FakeSession()

    monkeypatch.setattr("codepilot.tools.mcp._open_session", fake_open_session)
    registry = ToolRegistry()
    manager = McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), registry)
    await manager.start()
    try:
        tool = registry.get(build_mcp_tool_name("github", "create.issue"))
        assert tool is not None
        result = await tool.execute({"title": "测试"}, context=_context(tmp_path, allowed_tools=[]))
        assert result["status"] == "error"
        assert result["error_type"] == "McpPermissionError"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_call_reconnects_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    sessions = [FakeSession(fail_calls=1), FakeSession()]

    @asynccontextmanager
    async def fake_open_session(config: Any, workspace_path: Any) -> AsyncIterator[FakeSession]:
        yield sessions.pop(0)

    monkeypatch.setattr("codepilot.tools.mcp._open_session", fake_open_session)
    registry = ToolRegistry()
    manager = McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), registry)
    await manager.start()
    try:
        tool = registry.get(build_mcp_tool_name("github", "create.issue"))
        assert tool is not None
        result = await tool.execute({"title": "重试"}, context=_context(tmp_path, allowed_tools=["mcp:github"]))
        assert result["status"] == "ok"
        assert result["content"][0]["text"] == "create.issue:重试"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_error_and_image_are_normalized_without_base64_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    image_data = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()

    @asynccontextmanager
    async def fake_open_session(config: Any, workspace_path: Any) -> AsyncIterator[FakeSession]:
        yield FakeSession(is_error=True, image_data=image_data)

    monkeypatch.setattr("codepilot.tools.mcp._open_session", fake_open_session)
    registry = ToolRegistry()
    manager = McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), registry)
    await manager.start()
    try:
        tool = registry.get(build_mcp_tool_name("github", "create.issue"))
        assert tool is not None
        result = await tool.execute({"title": "失败"}, context=_context(tmp_path, allowed_tools=["mcp:github"]))
        assert result["status"] == "error"
        assert result["error_type"] == "McpToolError"
        attachment = result["attachments"][0]
        assert image_data not in str(result)
        assert tmp_path.joinpath("attachments", "session-1", "call-1", attachment["filename"]).exists()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_startup_failure_is_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    settings = McpSettings(
        servers={
            "broken": McpStdioServerSettings(transport="stdio", command="broken"),
            "github": McpStdioServerSettings(transport="stdio", command="ok", requires_approval=False),
        }
    )

    @asynccontextmanager
    async def fake_open_session(config: Any, workspace_path: Any) -> AsyncIterator[FakeSession]:
        if config.command == "broken":
            raise ConnectionError("无法启动")
        yield FakeSession()

    monkeypatch.setattr("codepilot.tools.mcp._open_session", fake_open_session)
    registry = ToolRegistry()
    manager = McpClientManager(settings, SimpleNamespace(workspace_path=tmp_path), registry)
    await manager.start()
    try:
        assert registry.get(build_mcp_tool_name("github", "create.issue")) is not None
        assert registry.get(build_mcp_tool_name("broken", "create.issue")) is None
    finally:
        await manager.shutdown()


def test_mcp_configuration_rejects_unsafe_values(tmp_path: Any) -> None:
    with pytest.raises(ValidationError, match="仅允许 HTTPS"):
        AppSettings.model_validate(
            {"mcp": {"servers": {"remote": {"transport": "streamable_http", "url": "http://example.com/mcp"}}}}
        )
    with pytest.raises(ValidationError, match="禁止内嵌认证信息"):
        AppSettings.model_validate(
            {
                "mcp": {
                    "servers": {
                        "remote": {"transport": "streamable_http", "url": "https://user:secret@example.com/mcp"}
                    }
                }
            }
        )
    with pytest.raises(ValidationError, match="server 名称非法"):
        AppSettings.model_validate(
            {"mcp": {"servers": {"bad:name": {"transport": "stdio", "command": "python"}}}}
        )
    with pytest.raises(ValueError, match="cwd 必须位于"):
        _resolve_stdio_cwd("../outside", tmp_path)


def test_mcp_tool_name_is_deterministic_and_bounded() -> None:
    first = build_mcp_tool_name("github", "issues/create.with a very long remote tool name" * 3)
    second = build_mcp_tool_name("github", "issues/create.with a very long remote tool name" * 3)

    assert first == second
    assert len(first) <= 64
    assert first.startswith("mcp__github__")


@pytest.mark.asyncio
async def test_mcp_stdio_inherits_process_environment_when_injecting_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClientSession:
        def __init__(self, read: Any, write: Any) -> None:
            return None

        async def __aenter__(self) -> "FakeClientSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    @asynccontextmanager
    async def fake_stdio_client(parameters: Any) -> AsyncIterator[tuple[str, str]]:
        captured["parameters"] = parameters
        yield "read", "write"

    monkeypatch.setenv("PATH", "/test/bin")
    monkeypatch.setenv("MCP_TOKEN", "secret")
    monkeypatch.setattr("codepilot.tools.mcp.stdio_client", fake_stdio_client)
    monkeypatch.setattr("codepilot.tools.mcp.ClientSession", FakeClientSession)
    config = McpStdioServerSettings(
        transport="stdio",
        command="mcp-server",
        env_from_process={"TOKEN": "MCP_TOKEN"},
    )

    async with _open_session(config, tmp_path):
        pass

    assert captured["parameters"].env["PATH"] == "/test/bin"
    assert captured["parameters"].env["TOKEN"] == "secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_data", "image_mime"),
    [
        (base64.b64encode(b"<html>not an image</html>").decode(), "text/html"),
        (base64.b64encode(b"not a PNG").decode(), "image/png"),
        ("not-valid-base64", "image/png"),
    ],
)
async def test_mcp_invalid_image_is_omitted_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    image_data: str,
    image_mime: str,
) -> None:
    @asynccontextmanager
    async def fake_open_session(config: Any, workspace_path: Any) -> AsyncIterator[FakeSession]:
        session = FakeSession(image_data=image_data)
        original_call_tool = session.call_tool

        async def call_tool_with_image_mime(name: str, arguments: dict[str, Any]) -> Any:
            result = await original_call_tool(name, arguments)
            result.content[1].mimeType = image_mime
            return result

        session.call_tool = call_tool_with_image_mime  # type: ignore[method-assign]
        yield session

    monkeypatch.setattr("codepilot.tools.mcp._open_session", fake_open_session)
    registry = ToolRegistry()
    manager = McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), registry)
    await manager.start()
    try:
        tool = registry.get(build_mcp_tool_name("github", "create.issue"))
        assert tool is not None
        result = await tool.execute({"title": "图片"}, context=_context(tmp_path, allowed_tools=["mcp:github"]))
        assert result["status"] == "ok"
        assert "attachments" not in result
        assert result["content"][1]["omitted"] is True
        assert image_data not in str(result)
        assert not tmp_path.joinpath("attachments").exists()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_official_sdk_stdio_end_to_end(tmp_path: Any) -> None:
    server_script = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    settings = McpSettings(
        servers={
            "local": McpStdioServerSettings(
                transport="stdio",
                command=sys.executable,
                args=[str(server_script)],
                cwd=".",
                requires_approval=False,
            )
        }
    )
    registry = ToolRegistry()
    manager = McpClientManager(
        settings=settings,
        workspace=SimpleNamespace(workspace_path=tmp_path, workspace_dir=tmp_path),
        tool_registry=registry,
    )

    await manager.start()
    try:
        tool = registry.get("mcp__local__echo")
        assert tool is not None
        result = await tool.execute({"text": "真实调用"}, context=_context(tmp_path, allowed_tools=["mcp:local"]))
        assert result["status"] == "ok"
        assert result["content"][0]["text"] == "真实调用"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_streamable_http_uses_header_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    captured: dict[str, Any] = {}

    class FakeHttpClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> "FakeHttpClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    class FakeClientSession:
        def __init__(self, read: Any, write: Any) -> None:
            captured["streams"] = (read, write)

        async def __aenter__(self) -> "FakeClientSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    @asynccontextmanager
    async def fake_http_transport(url: str, *, http_client: Any) -> AsyncIterator[tuple[str, str, None]]:
        captured["url"] = url
        captured["http_client"] = http_client
        yield "read", "write", None

    monkeypatch.setenv("MCP_AUTH", "Bearer secret")
    monkeypatch.setattr("codepilot.tools.mcp.httpx.AsyncClient", FakeHttpClient)
    monkeypatch.setattr("codepilot.tools.mcp.streamable_http_client", fake_http_transport)
    monkeypatch.setattr("codepilot.tools.mcp.ClientSession", FakeClientSession)
    config = McpStreamableHttpServerSettings(
        transport="streamable_http",
        url="https://example.com/mcp",
        headers_from_env={"Authorization": "MCP_AUTH"},
    )

    async with _open_session(config, tmp_path) as session:
        assert isinstance(session, FakeClientSession)

    assert captured["url"] == "https://example.com/mcp"
    assert captured["headers"] == {"Authorization": "Bearer secret"}
    assert captured["timeout"] == 120
