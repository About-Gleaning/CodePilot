from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot.config import AppSettings
from codepilot.session.agent_config import AgentConfigError
from codepilot.session.working_directory import resolve_working_directory


def test_directory_requires_existing_admin_allowed_root(tmp_path: Path):
    default = tmp_path / "default"
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    for path in (default, allowed, outside):
        path.mkdir()
    settings = AppSettings()
    settings.agent.allowed_working_roots = [str(allowed)]
    assert resolve_working_directory(settings, default, None) == default
    assert resolve_working_directory(settings, default, str(allowed)) == allowed
    with pytest.raises(AgentConfigError) as forbidden:
        resolve_working_directory(settings, default, str(outside))
    assert forbidden.value.status == 403
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AgentConfigError):
        resolve_working_directory(settings, default, str(allowed / "escape"))
    with pytest.raises(AgentConfigError):
        resolve_working_directory(settings, default, str(allowed / "missing"))
