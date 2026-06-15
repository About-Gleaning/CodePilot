from __future__ import annotations

"""定时任务的数据模型和时间计算工具。"""

from datetime import UTC, datetime, time, timedelta, tzinfo
from enum import Enum
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


TriggerKind = Literal["once", "interval", "daily", "weekly"]


class ScheduleRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = {
    ScheduleRunStatus.COMPLETED,
    ScheduleRunStatus.FAILED,
    ScheduleRunStatus.TIMEOUT,
    ScheduleRunStatus.INTERRUPTED,
    ScheduleRunStatus.CANCELLED,
}


class ScheduleTrigger(BaseModel):
    kind: TriggerKind
    run_at: str | None = None
    interval_seconds: int | None = None
    time_of_day: str | None = None
    day_of_week: int | None = None
    timezone: str | None = None

    @field_validator("time_of_day")
    @classmethod
    def validate_time_of_day(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_time_of_day(value)
        return value

    @field_validator("run_at")
    @classmethod
    def validate_run_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_iso_datetime(value)
        return value

    @model_validator(mode="after")
    def validate_by_kind(self) -> "ScheduleTrigger":
        if self.kind == "once" and not self.run_at:
            raise ValueError("once 触发器必须提供 run_at")
        if self.kind == "interval":
            if self.interval_seconds is None:
                raise ValueError("interval 触发器必须提供 interval_seconds")
            if self.interval_seconds < 60:
                raise ValueError("interval_seconds 不能小于 60")
        if self.kind == "daily" and not self.time_of_day:
            raise ValueError("daily 触发器必须提供 time_of_day")
        if self.kind == "weekly":
            if self.day_of_week is None:
                raise ValueError("weekly 触发器必须提供 day_of_week")
            if self.day_of_week < 1 or self.day_of_week > 7:
                raise ValueError("day_of_week 必须是 1 到 7，1 表示周一，7 表示周日")
            if not self.time_of_day:
                raise ValueError("weekly 触发器必须提供 time_of_day")
        if self.timezone:
            load_timezone(self.timezone)
        return self


class ScheduleTask(BaseModel):
    id: str = Field(default_factory=lambda: f"scht_{uuid4().hex}")
    name: str
    prompt: str
    agent_name: str
    provider: str
    model: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    trigger: ScheduleTrigger
    working_dir: str
    isolation_mode: str = "subprocess"
    enabled: bool = True
    created_at: str
    updated_at: str
    next_run_at: str | None = None
    last_run_at: str | None = None


class ScheduleRun(BaseModel):
    id: str = Field(default_factory=lambda: f"run_{uuid4().hex}")
    task_id: str
    task_name: str
    session_id: str | None = None
    status: ScheduleRunStatus
    scheduled_at: str
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    working_dir: str
    error: str | None = None
    summary: str | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_iso_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_time_of_day(value: str) -> time:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("time_of_day 必须使用 HH:mm 格式")
    hour = int(parts[0])
    minute = int(parts[1])
    return time(hour=hour, minute=minute)


def load_timezone(value: str | None) -> tzinfo:
    if not value:
        return datetime.now().astimezone().tzinfo or UTC
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"timezone `{value}` 不存在") from exc


def compute_next_run_at(trigger: ScheduleTrigger, *, now: datetime | None = None) -> str:
    """按触发器计算下一次执行时间，统一以 UTC ISO 字符串落盘。"""
    current = (now or utc_now()).astimezone(UTC)
    if trigger.kind == "once":
        assert trigger.run_at is not None
        return to_iso(parse_iso_datetime(trigger.run_at))
    if trigger.kind == "interval":
        assert trigger.interval_seconds is not None
        return to_iso(current + timedelta(seconds=trigger.interval_seconds))

    assert trigger.time_of_day is not None
    zone = load_timezone(trigger.timezone)
    local_now = current.astimezone(zone)
    run_time = parse_time_of_day(trigger.time_of_day)
    candidate = local_now.replace(
        hour=run_time.hour,
        minute=run_time.minute,
        second=0,
        microsecond=0,
    )
    if trigger.kind == "weekly":
        assert trigger.day_of_week is not None
        days_until_target = trigger.day_of_week - local_now.isoweekday()
        if days_until_target < 0:
            days_until_target += 7
        candidate += timedelta(days=days_until_target)
        if candidate <= local_now:
            candidate += timedelta(days=7)
        return to_iso(candidate)

    if candidate <= local_now:
        candidate += timedelta(days=1)
    return to_iso(candidate)


def compute_following_run_at(trigger: ScheduleTrigger, scheduled_at: str, *, now: datetime | None = None) -> str | None:
    """任务触发后计算下一轮；单次任务没有下一轮。"""
    current = (now or utc_now()).astimezone(UTC)
    if trigger.kind == "once":
        return None
    if trigger.kind == "interval":
        assert trigger.interval_seconds is not None
        candidate = parse_iso_datetime(scheduled_at) + timedelta(seconds=trigger.interval_seconds)
        while candidate <= current:
            candidate += timedelta(seconds=trigger.interval_seconds)
        return to_iso(candidate)
    return compute_next_run_at(trigger, now=current)
