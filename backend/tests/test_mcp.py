from __future__ import annotations

import base64
import asyncio
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
# 场景3：异常处理 - 未授权调用、远端错误、断线不重放和非法配置。


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


@pytest.mark.asyncio
async def test_discovery_rejects_code_tool_name_collision(monkeypatch, tmp_path):
    from codepilot.session.agent_config import AgentConfigError
    from codepilot.tools.code_store import ToolStore
    from codepilot.tools.python_code_tool import PythonCodeTool
    from test_python_tools import named_payload
    from uuid import uuid4
    user = str(uuid4())
    store = ToolStore(tmp_path)
    record = store.save(user, None, named_payload())
    tool = PythonCodeTool(store, user, record['tool_id'], record['version'])
    registry = ToolRegistry(); registry.register(tool)
    @asynccontextmanager
    async def open_fake(*args, **kwargs):
        yield FakeSession()
    monkeypatch.setattr('codepilot.tools.mcp._open_session', open_fake)
    # 注入发现名称冲突，验证运行装配保护，而非依赖保留前缀永不冲突。
    monkeypatch.setattr('codepilot.tools.mcp.build_mcp_tool_name', lambda *args: 'test_tool')
    manager = McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), registry)
    try:
        with pytest.raises(AgentConfigError) as caught:
            await manager.start()
        assert caught.value.code == 'tool_name_conflict'
        assert registry.get('test_tool') is tool
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_personal_mcp_schemas_and_revocation_are_isolated(monkeypatch, tmp_path):
    calls = []
    revoked = False

    class IdentitySession(FakeSession):
        def __init__(self, identity):
            super().__init__()
            self.identity = identity

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=self.identity, description="专用工具", inputSchema={"type": "object"})])

        async def call_tool(self, name, arguments):
            calls.append(self.identity)
            return SimpleNamespace(content=[], isError=False)

    @asynccontextmanager
    async def open_identity(config, workspace, credentials):
        yield IdentitySession(credentials["identity"])

    def guard():
        if revoked:
            raise ValueError("连接已撤销")

    monkeypatch.setattr("codepilot.tools.mcp._open_session", open_identity)
    registries = [ToolRegistry(), ToolRegistry()]
    managers = [McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), registry,
                 credentials={"github": {"identity": identity}}, guards={"github": guard})
                for registry, identity in zip(registries, ["first", "second"])]
    try:
        for manager in managers:
            await manager.start()
        assert registries[0].get(build_mcp_tool_name("github", "first")) is not None
        assert registries[0].get(build_mcp_tool_name("github", "second")) is None
        assert registries[1].get(build_mcp_tool_name("github", "first")) is None
        await managers[0].call_tool("github", "first", {})
        revoked = True
        with pytest.raises(RuntimeError, match="McpServerUnavailable"):
            await managers[1].call_tool("github", "second", {})
        assert calls == ["first"]
    finally:
        for manager in managers:
            await manager.shutdown()


@pytest.mark.asyncio
async def test_multiple_connection_identities_share_service_capacity(monkeypatch, tmp_path):
    release = asyncio.Event()
    five_started = asyncio.Event()
    running = peak = 0

    class BlockingSession(FakeSession):
        async def call_tool(self, name, arguments):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            if running == 5:
                five_started.set()
            try:
                await release.wait()
                return SimpleNamespace(content=[], isError=False)
            finally:
                running -= 1

    @asynccontextmanager
    async def open_session(config, workspace):
        yield BlockingSession()

    monkeypatch.setattr("codepilot.tools.mcp._open_session", open_session)
    managers = [McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), ToolRegistry()) for _ in range(2)]
    pending = []
    try:
        for manager in managers:
            await manager.start()
        pending = [asyncio.create_task(managers[index % 2].call_tool("github", "create.issue", {})) for index in range(25)]
        await asyncio.wait_for(five_started.wait(), 2)
        with pytest.raises(RuntimeError, match="McpCapacityExceeded"):
            await managers[1].call_tool("github", "create.issue", {})
        release.set()
        await asyncio.gather(*pending)
        assert peak == 5
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        for manager in managers:
            await manager.shutdown()


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


def _context_with_tool_call_id(tmp_path: Any, *, tool_call_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        session=SimpleNamespace(session_id="session-1"),
        workspace=SimpleNamespace(workspace_dir=tmp_path, workspace_path=tmp_path),
        agent=SimpleNamespace(name="build", allowed_tools=["mcp:github"]),
        tool_call_id=tool_call_id,
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
async def test_mcp_call_failure_is_not_replayed(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    session = FakeSession(fail_calls=1)
    open_count = 0

    @asynccontextmanager
    async def fake_open_session(config: Any, workspace_path: Any) -> AsyncIterator[FakeSession]:
        nonlocal open_count
        open_count += 1
        yield session

    monkeypatch.setattr("codepilot.tools.mcp._open_session", fake_open_session)
    registry = ToolRegistry()
    manager = McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), registry)
    await manager.start()
    try:
        tool = registry.get(build_mcp_tool_name("github", "create.issue"))
        assert tool is not None
        result = await tool.execute({"title": "不可重放"}, context=_context(tmp_path, allowed_tools=["mcp:github"]))
        assert result["status"] == "error"
        assert result["error_type"] == "McpOutcomeUncertain"
        assert open_count == 1
        assert session.fail_calls == 0
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
async def test_mcp_image_attachment_path_stays_inside_attachment_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    image_data = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()

    @asynccontextmanager
    async def fake_open_session(config: Any, workspace_path: Any) -> AsyncIterator[FakeSession]:
        yield FakeSession(image_data=image_data)

    monkeypatch.setattr("codepilot.tools.mcp._open_session", fake_open_session)
    registry = ToolRegistry()
    manager = McpClientManager(_settings(), SimpleNamespace(workspace_path=tmp_path), registry)
    await manager.start()
    try:
        tool = registry.get(build_mcp_tool_name("github", "create.issue"))
        assert tool is not None
        result = await tool.execute(
            {"title": "图片"},
            context=_context_with_tool_call_id(tmp_path, tool_call_id="../../outside"),
        )
        attachment = result["attachments"][0]
        target = Path(attachment["source_path"]).resolve()
        attachment_root = tmp_path.joinpath("attachments").resolve()
        assert target.is_relative_to(attachment_root)
        assert not tmp_path.joinpath("outside", attachment["filename"]).exists()
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
