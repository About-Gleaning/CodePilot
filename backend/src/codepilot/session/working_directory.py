from pathlib import Path

from codepilot.session.agent_config import AgentConfigError


def resolve_working_directory(config, default: Path, selected: str | None) -> Path:
    default = default.resolve()
    target = (Path(selected).expanduser() if selected else default).resolve()
    roots = [default, *(Path(root).expanduser().resolve() for root in config.agent.allowed_working_roots)]
    if not target.is_dir():
        raise AgentConfigError("工作目录不存在，请选择已有目录", code="working_directory_missing", status=409)
    if not any(target.is_relative_to(root) for root in roots):
        raise AgentConfigError("工作目录不在管理员允许范围内", code="working_directory_forbidden", status=403)
    return target
