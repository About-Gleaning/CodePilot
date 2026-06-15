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
    agent_profiles: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """校验定时任务输入，并归一化为 ScheduleRunner 可直接接收的字段。"""
    name = str(payload.get("name") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    agent_name = str(payload.get("agent_name") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not name:
        raise ScheduleValidationError("任务名不能为空", error_type="ScheduleNameEmpty")
    if not prompt:
        raise ScheduleValidationError("prompt 不能为空", error_type="SchedulePromptEmpty")

    profile = agent_profiles.get(agent_name)
    if profile is None or getattr(profile, "kind", "agent") != "agent":
        raise ScheduleValidationError(f"agent `{agent_name}` 不存在或不能直接选择", error_type="ScheduleAgentInvalid")

    activated_provider = settings.llm_runtime.activated_providers.get(provider)
    if activated_provider is None:
        raise ScheduleValidationError(f"provider `{provider}` 未激活或不存在", error_type="ScheduleProviderInvalid")
    if model not in activated_provider.models:
        raise ScheduleValidationError(f"model `{model}` 不属于 provider `{provider}`", error_type="ScheduleModelInvalid")

    working_dir_value = str(payload.get("working_dir") or "").strip()
    if not working_dir_value:
        raise ScheduleValidationError("working_dir 不能为空", error_type="ScheduleWorkingDirInvalid")
    working_dir = Path(working_dir_value).expanduser().resolve()
    if not working_dir.exists() or not working_dir.is_dir():
        raise ScheduleValidationError(f"working_dir `{working_dir}` 不存在或不是目录", error_type="ScheduleWorkingDirInvalid")

    trigger_value = payload.get("trigger")
    try:
        trigger = trigger_value if isinstance(trigger_value, ScheduleTrigger) else ScheduleTrigger.model_validate(trigger_value)
    except Exception as exc:  # noqa: BLE001
        raise ScheduleValidationError(str(exc), error_type="ScheduleTriggerInvalid") from exc

    if str(payload.get("isolation_mode") or "subprocess") != "subprocess":
        raise ScheduleValidationError("第一版只支持 subprocess 隔离模式", error_type="ScheduleIsolationModeInvalid")

    return {
        "name": name,
        "prompt": prompt,
        "agent_name": agent_name,
        "provider": provider,
        "model": model,
        "trigger": trigger,
        "working_dir": str(working_dir),
        "enabled": bool(payload.get("enabled", True)),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        "isolation_mode": "subprocess",
    }
