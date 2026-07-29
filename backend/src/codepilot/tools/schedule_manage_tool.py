from __future__ import annotations

from typing import Any

from codepilot.config import AppSettings
from codepilot.scheduler.models import ScheduleRunStatus, compute_next_run_at
from codepilot.scheduler.runner import ScheduleRunner
from codepilot.scheduler.service import ScheduleValidationError, validate_schedule_task_payload
from codepilot.scheduler.store import ScheduleStore
from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolPreflightResult, ToolSpec
from codepilot.tools.file_tool_common import FileToolError, build_tool_failure, build_tool_success, load_tool_description


SCHEDULE_ACTIONS = {
    "list_tasks",
    "create_task",
    "update_task",
    "enable_task",
    "disable_task",
    "delete_task",
    "list_runs",
}
SCHEDULE_MUTATION_ACTIONS = {
    "create_task",
    "update_task",
    "enable_task",
    "disable_task",
    "delete_task",
}


class ScheduleManageTool(BaseTool):
    def __init__(
        self,
        *,
        store: ScheduleStore,
        runner: ScheduleRunner,
        settings: AppSettings,
        agent_profiles: dict[str, Any],
        timeout_seconds: int,
    ) -> None:
        self._store = store
        self._runner = runner
        self._settings = settings
        self._agent_profiles = agent_profiles
        self.spec = ToolSpec(
            name="schedule_manage",
            description=load_tool_description("schedule_manage"),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": sorted(SCHEDULE_ACTIONS)},
                    "task_id": {"type": "string", "description": "要修改、启停或删除的定时任务 ID。"},
                    "name": {"type": "string", "description": "任务名称。"},
                    "prompt": {"type": "string", "description": "定时执行时发送给 Agent 的固定 prompt。"},
                    "agent_name": {"type": "string", "description": "执行任务的 Agent 名称。"},
                    "provider": {"type": "string", "description": "已激活的模型 provider。"},
                    "model": {"type": "string", "description": "provider 下可用的模型 ID。"},
                    "working_dir": {"type": "string", "description": "worker 执行任务时使用的本机目录。"},
                    "trigger": {
                        "type": "object",
                        "description": "触发配置，支持 once、interval、daily、weekly。",
                        "additionalProperties": True,
                    },
                    "enabled": {"type": "boolean", "description": "创建或更新时是否启用任务。"},
                    "metadata": {"type": "object", "description": "附加元数据。", "additionalProperties": True},
                    "recent_limit": {
                        "type": "integer",
                        "description": "list_runs 返回的最近运行记录数量，默认 20，最大 50。",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            can_parallel=False,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
            side_effect="runtime_mutation",
        )

    async def preflight(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolPreflightResult:
        action = str(args.get("action") or "").strip()
        if action in SCHEDULE_MUTATION_ACTIONS:
            return ToolPreflightResult(
                status="requires_approval",
                reason="该操作会修改持久化定时任务，并可能影响后续 worker 执行，需要人工确认。",
            )
        return ToolPreflightResult(status="allow")

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            self._ensure_allowed(context)
            action = str(args.get("action") or "").strip()
            if action not in SCHEDULE_ACTIONS:
                raise FileToolError(f"不支持的 action：{action}", error_type="ScheduleActionInvalid")

            if action == "list_tasks":
                return self._list_tasks()
            if action == "create_task":
                return self._create_task(args)
            if action == "update_task":
                return self._update_task(args)
            if action == "enable_task":
                return self._set_enabled(args, enabled=True)
            if action == "disable_task":
                return self._set_enabled(args, enabled=False)
            if action == "delete_task":
                return self._delete_task(args)
            return self._list_runs(args)
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)

    def _ensure_allowed(self, context: ToolExecutionContext | None) -> None:
        if context is None:
            raise FileToolError("schedule_manage 缺少运行上下文。", error_type="ToolContextMissing")
        allowed_tools = getattr(context.agent, "allowed_tools", []) or []
        if self.spec.name not in allowed_tools:
            raise FileToolError("当前 Agent 不允许管理定时任务。", error_type="ScheduleToolAgentForbidden")

    def _list_tasks(self) -> dict[str, Any]:
        tasks = [task.model_dump() for task in self._store.list_tasks()]
        return build_tool_success(
            self.spec.name,
            tasks=tasks,
            output=f"当前共有 {len(tasks)} 个定时任务。",
        )

    def _create_task(self, args: dict[str, Any]) -> dict[str, Any]:
        validated = self._validate(args)
        task = self._runner.create_task(**validated)
        return build_tool_success(
            self.spec.name,
            schedule=task.model_dump(),
            output=f"定时任务已创建：{task.name}（{task.id}）。",
        )

    def _update_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = self._require_task_id(args)
        current = self._store.get_task(task_id)
        if current is None:
            raise FileToolError(f"定时任务不存在：{task_id}", error_type="ScheduleTaskNotFound")

        raw_updates = {key: value for key, value in args.items() if key in _UPDATABLE_FIELDS}
        if not raw_updates:
            raise FileToolError("update_task 至少需要提供一个要修改的字段。", error_type="ScheduleUpdateEmpty")

        merged = current.model_dump()
        merged.update(raw_updates)
        validated = self._validate(merged)
        updates = {key: validated[key] for key in raw_updates if key in validated}
        if "trigger" in raw_updates:
            updates["trigger"] = validated["trigger"]
            updates["next_run_at"] = compute_next_run_at(validated["trigger"]) if merged.get("enabled", current.enabled) else None

        task = self._runner.update_task(task_id, updates)
        if task is None:
            raise FileToolError(f"定时任务不存在：{task_id}", error_type="ScheduleTaskNotFound")
        return build_tool_success(
            self.spec.name,
            schedule=task.model_dump(),
            output=f"定时任务已更新：{task.name}（{task.id}）。",
        )

    def _set_enabled(self, args: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
        task_id = self._require_task_id(args)
        task = self._runner.update_task(task_id, {"enabled": enabled})
        if task is None:
            raise FileToolError(f"定时任务不存在：{task_id}", error_type="ScheduleTaskNotFound")
        state = "开启" if enabled else "关闭"
        return build_tool_success(
            self.spec.name,
            schedule=task.model_dump(),
            output=f"定时任务已{state}：{task.name}（{task.id}）。",
        )

    def _delete_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = self._require_task_id(args)
        cancelled_before = {
            run.id
            for run in self._store.list_runs()
            if run.task_id == task_id and run.status == ScheduleRunStatus.PENDING
        }
        deleted = self._runner.delete_task(task_id)
        if not deleted:
            raise FileToolError(f"定时任务不存在：{task_id}", error_type="ScheduleTaskNotFound")
        cancelled_after = {
            run.id
            for run in self._store.list_runs()
            if run.task_id == task_id and run.status == ScheduleRunStatus.CANCELLED
        }
        cancelled_count = len(cancelled_before & cancelled_after)
        return build_tool_success(
            self.spec.name,
            deleted_task_id=task_id,
            cancelled_pending_runs=cancelled_count,
            output=f"定时任务已删除：{task_id}，取消未启动运行 {cancelled_count} 个。",
        )

    def _list_runs(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("recent_limit") or 20)
        limit = max(1, min(limit, 50))
        active = [run.model_dump() for run in self._store.active_runs()]
        recent = [run.model_dump() for run in self._store.recent_runs(limit=limit)]
        return build_tool_success(
            self.spec.name,
            active=active,
            recent=recent,
            output=f"当前 active run {len(active)} 个，最近 run 返回 {len(recent)} 个。",
        )

    def _validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_schedule_task_payload(
                settings=self._settings,
                agent_profiles=self._agent_profiles,
                payload=payload,
            )
        except ScheduleValidationError as exc:
            raise FileToolError(exc.message, error_type=exc.error_type) from exc

    def _require_task_id(self, args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            raise FileToolError("task_id 不能为空。", error_type="ScheduleTaskIdEmpty")
        return task_id


_UPDATABLE_FIELDS = {
    "name",
    "prompt",
    "agent_name",
    "provider",
    "model",
    "trigger",
    "working_dir",
    "enabled",
    "metadata",
    "isolation_mode",
}
