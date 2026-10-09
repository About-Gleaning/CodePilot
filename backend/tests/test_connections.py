import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from codepilot.config import AppSettings
from codepilot.config.settings import McpSettings, McpStreamableHttpServerSettings
from codepilot.session.agent_config import AgentConfigError
from codepilot.session.connections import ConnectionStore
from codepilot.tools.mcp import _resolve_env


def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_CONNECTION_KEY", Fernet.generate_key().decode())
    settings = AppSettings(mcp=McpSettings(servers={"demo": McpStreamableHttpServerSettings(
        transport="streamable_http", url="https://example.test/mcp", assignable_to_private_agents=True,
        credential_fields=["DEMO_TOKEN"], headers_from_env={"Authorization": "DEMO_TOKEN"},
    )}))
    return ConnectionStore(tmp_path, settings)


def test_connections_encrypt_isolate_rotate_and_revoke(tmp_path, monkeypatch):
    service = store(tmp_path, monkeypatch)
    owner, other = str(uuid4()), str(uuid4())
    created = service.save(owner, None, "mcp:demo", {"DEMO_TOKEN": "private-test-value"}, None)
    assert "private-test-value" not in json.dumps(created)
    assert "private-test-value" not in service._path(owner, created["connection_id"]).read_text()
    assert service.resolve(owner, created["connection_id"])[1] == {"DEMO_TOKEN": "private-test-value"}
    with pytest.raises(AgentConfigError):
        service.resolve(other, created["connection_id"])
    changed = service.save(owner, created["connection_id"], "mcp:demo", {"DEMO_TOKEN": "replacement"}, created["revision"])
    with pytest.raises(AgentConfigError):
        service.resolve(owner, created["connection_id"], created["revision"])
    service.revoke(owner, changed["connection_id"], changed["revision"])
    with pytest.raises(AgentConfigError):
        service.resolve(owner, changed["connection_id"])


def test_private_identity_never_falls_back_to_team_environment(tmp_path, monkeypatch):
    service = store(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_TOKEN", "team-test-value")
    with pytest.raises(ValueError):
        _resolve_env({"Authorization": "DEMO_TOKEN"}, {})
    assert _resolve_env({"Authorization": "DEMO_TOKEN"}, {"DEMO_TOKEN": "personal"}) == {"Authorization": "personal"}
    with pytest.raises(AgentConfigError):
        service.save(str(uuid4()), None, "mcp:arbitrary", {"DEMO_TOKEN": "personal"}, None)


def test_private_connection_requires_separate_key(tmp_path, monkeypatch):
    service = store(tmp_path, monkeypatch)
    monkeypatch.delenv("CODEPILOT_CONNECTION_KEY")
    with pytest.raises(AgentConfigError) as error:
        service.save(str(uuid4()), None, "mcp:demo", {"DEMO_TOKEN": "personal"}, None)
    assert error.value.code == "connection_key_missing"
