from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import FileToolError, build_tool_failure, build_tool_success, load_tool_description


TODO_STATUSES = {"pending", "in_progress", "completed"}
TODO_PRIORITIES = {"low", "medium", "high"}
MAX_TODOS = 20


class TodoWriteTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="todo_write",
            description=load_tool_description("todo_write"),
            input_schema={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "maxItems": MAX_TODOS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "待办事项内容。"},
                                "status": {"type": "string", "enum": sorted(TODO_STATUSES)},
                                "priority": {"type": "string", "enum": sorted(TODO_PRIORITIES)},
                            },
                            "required": ["content", "status", "priority"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["todos"],
                "additionalProperties": False,
            },
            can_parallel=False,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            todos = _normalize_todos(args.get("todos"))
            todo_path = _todo_path(context)
            todo_path.parent.mkdir(parents=True, exist_ok=True)
            todo_path.write_text(json.dumps({"todos": todos}, ensure_ascii=False, indent=2), encoding="utf-8")
            summary = _summarize(todos)
            return build_tool_success(
                self.spec.name,
                todo_path=str(todo_path),
                todos=todos,
                summary=summary,
                output=f"Todo 已保存：共 {summary['total']} 项，进行中 {summary['in_progress']} 项，已完成 {summary['completed']} 项。",
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)


class TodoReadTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="todo_read",
            description=load_tool_description("todo_read"),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            can_parallel=True,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            todo_path = _todo_path(context)
            if not todo_path.exists():
                return build_tool_success(
                    self.spec.name,
                    todo_path=str(todo_path),
                    todos=[],
                    summary=_summarize([]),
                    output="当前 session 还没有 todo 记录。",
                )
            payload = json.loads(todo_path.read_text(encoding="utf-8"))
            todos = _normalize_todos(payload.get("todos"))
            return build_tool_success(
                self.spec.name,
                todo_path=str(todo_path),
                todos=todos,
                summary=_summarize(todos),
                output=json.dumps(todos, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)


def _todo_path(context: ToolExecutionContext | None) -> Path:
    if context is None:
        raise FileToolError("todo 工具缺少运行上下文。", error_type="ToolContextMissing")
    todos_dir = Path(context.workspace.workspace_dir).resolve() / "todos"
    path = (todos_dir / f"{context.session.session_id}.json").resolve()
    if not path.is_relative_to(todos_dir.resolve()):
        raise FileToolError("todo 文件路径越界。", error_type="TodoPathForbidden")
    return path


def _normalize_todos(raw_todos: Any) -> list[dict[str, str]]:
    if not isinstance(raw_todos, list):
        raise FileToolError("todos 必须是数组。", error_type="TodoInputInvalid")
    if len(raw_todos) > MAX_TODOS:
        raise FileToolError(f"todos 最多允许 {MAX_TODOS} 项。", error_type="TodoTooManyItems")

    todos: list[dict[str, str]] = []
    in_progress_count = 0
    for index, raw in enumerate(raw_todos):
        if not isinstance(raw, dict):
            raise FileToolError(f"第 {index + 1} 项 todo 必须是对象。", error_type="TodoItemInvalid")
        content = str(raw.get("content", "")).strip()
        status = str(raw.get("status", "")).strip()
        priority = str(raw.get("priority", "")).strip()
        if not content:
            raise FileToolError(f"第 {index + 1} 项 todo 内容不能为空。", error_type="TodoContentEmpty")
        if status not in TODO_STATUSES:
            raise FileToolError(f"第 {index + 1} 项 todo 状态非法：{status}", error_type="TodoStatusInvalid")
        if priority not in TODO_PRIORITIES:
            raise FileToolError(f"第 {index + 1} 项 todo 优先级非法：{priority}", error_type="TodoPriorityInvalid")
        if status == "in_progress":
            in_progress_count += 1
        todos.append({"content": content, "status": status, "priority": priority})

    if in_progress_count > 1:
        raise FileToolError("最多只能有一个 in_progress todo。", error_type="TodoInProgressConflict")
    return todos


def _summarize(todos: list[dict[str, str]]) -> dict[str, int]:
    return {
        "total": len(todos),
        "pending": sum(1 for item in todos if item["status"] == "pending"),
        "in_progress": sum(1 for item in todos if item["status"] == "in_progress"),
        "completed": sum(1 for item in todos if item["status"] == "completed"),
    }
