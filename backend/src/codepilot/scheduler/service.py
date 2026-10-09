from __future__ import annotations

"""定时任务共享业务校验。

HTTP API 和 Agent 工具都会创建/修改定时任务。校验集中在这里，避免两条入口
对 agent、模型、路径或触发器的约束出现分叉。
"""

from pathlib import Path
from typing import Any

from codepilot.config import AppSettings
from codepilot.scheduler.models import ScheduleTrigger


class ScheduleValidationError(Exception):
    """定时任务输入不符合业务约束。"""

    def __init__(self, message: str, *, error_type: str = "ScheduleInputInvalid") -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type


def validate_schedule_task_payload(
    *,
    settings: AppSettings,
    agent_profiles: dict[str, Any] | None,
    payload: dict[str, Any],
    profile_resolver: Any | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """校验定时任务输入，并归一化为 ScheduleRunner 可直接接收的字段。"""
    name = str(payload.get("name") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    agent_name = str(payload.get("agent_name") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not name:
        raise ScheduleValidationError("任务名不能为空", error_type="ScheduleNameEmpty")
    if not prompt:
        raise ScheduleValidationError("prompt 不能为空", error_type="SchedulePromptEmpty")
    if len(name) > 200 or len(prompt) > 32000:
        raise ScheduleValidationError("任务名称或要求超过长度限制", error_type="ScheduleInputTooLong")

    if profile_resolver is not None and agent_id:
        try:
            profile = profile_resolver(user_id, agent_id)
        except Exception:
            profile = None
    else:
        profile = (agent_profiles or {}).get(agent_name)
    if profile is None or getattr(profile, "kind", "agent") != "agent":
        raise ScheduleValidationError(f"agent `{agent_id or agent_name}` 不存在或不能直接选择", error_type="ScheduleAgentInvalid")

    if payload.get("follow_agent_model"):
        provider = str(getattr(profile, "default_provider", "") or "")
        model = str(getattr(profile, "default_model", "") or "")

    activated_provider = settings.llm_runtime.activated_providers.get(provider)
    if activated_provider is None:
        raise ScheduleValidationError(f"provider `{provider}` 未激活或不存在", error_type="ScheduleProviderInvalid")
    if model not in activated_provider.models:
        raise ScheduleValidationError(f"model `{model}` 不属于 provider `{provider}`", error_type="ScheduleModelInvalid")

    working_dir_value = str(payload.get("working_dir") or "").strip()
    if working_dir_value == ".":
        working_dir_value = str(getattr(getattr(profile_resolver, "__self__", None), "workspace_path", None) or working_dir_value)
    if payload.get("follow_agent_directory"):
        default_root = getattr(getattr(profile_resolver, "__self__", None), "workspace_path", None)
        working_dir_value = str(getattr(profile, "working_directory", None) or default_root or "")
    if not working_dir_value:
        raise ScheduleValidationError("working_dir 不能为空", error_type="ScheduleWorkingDirInvalid")
    working_dir = Path(working_dir_value).expanduser().resolve()
    if not working_dir.exists() or not working_dir.is_dir():
        raise ScheduleValidationError("工作目录不存在或不是目录，请重新选择。", error_type="ScheduleWorkingDirInvalid")
    default_directory = getattr(getattr(profile_resolver, "__self__", None), "workspace_path", None)
    if default_directory is not None:
        from codepilot.session.working_directory import resolve_working_directory
        try:
            working_dir = resolve_working_directory(settings, default_directory, str(working_dir))
        except ValueError as exc:
            raise ScheduleValidationError("工作目录不在管理员允许范围内", error_type="ScheduleWorkingDirInvalid") from exc

    trigger_value = payload.get("trigger")
    try:
        trigger = trigger_value if isinstance(trigger_value, ScheduleTrigger) else ScheduleTrigger.model_validate(trigger_value)
    except Exception as exc:  # noqa: BLE001
        raise ScheduleValidationError("触发时间格式无效，请检查时间、时区和间隔。", error_type="ScheduleTriggerInvalid") from exc

    if str(payload.get("isolation_mode") or "subprocess") != "subprocess":
        raise ScheduleValidationError("第一版只支持 subprocess 隔离模式", error_type="ScheduleIsolationModeInvalid")

    return {
        "name": name,
        "prompt": prompt,
        "agent_id": str(getattr(profile, "agent_id", "") or ""),
        "agent_name": str(getattr(profile, "name", agent_name) or agent_name),
        "revision_id": str(getattr(profile, "revision_id", "") or ""),
        "provider": provider,
        "model": model,
        "follow_agent_model": bool(payload.get("follow_agent_model", False)),
        "follow_agent_directory": bool(payload.get("follow_agent_directory", False)),
        "trigger": trigger,
        "working_dir": str(working_dir),
        "enabled": bool(payload.get("enabled", True)),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        "isolation_mode": "subprocess",
    }
