from __future__ import annotations

from typing import Any
from uuid import uuid4

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import FileToolError, build_tool_failure, load_tool_description


DEFAULT_CUSTOM_LABEL = "不是以上任何选项"
MAX_QUESTIONS = 3
MAX_OPTIONS = 8


class QuestionTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="question",
            description=load_tool_description("question"),
            input_schema={
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_QUESTIONS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "question": {"type": "string"},
                                "multiple": {"type": "boolean"},
                                "custom": {"type": "boolean"},
                                "options": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "value": {"type": "string"},
                                            "label": {"type": "string"},
                                        },
                                        "required": ["value", "label"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["id", "question", "options"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["questions"],
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
            questions = _normalize_questions(args.get("questions"))
            return {
                "status": "question_required",
                "tool_name": self.spec.name,
                "question_id": f"question_{uuid4().hex}",
                "questions": questions,
                "output": f"需要用户回答 {len(questions)} 个问题后继续。",
            }
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)


def _normalize_questions(raw_questions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_questions, list) or not raw_questions:
        raise FileToolError("questions 必须是非空数组。", error_type="QuestionInputInvalid")
    if len(raw_questions) > MAX_QUESTIONS:
        raise FileToolError(f"questions 最多允许 {MAX_QUESTIONS} 项。", error_type="QuestionTooManyItems")

    seen_ids: set[str] = set()
    questions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raise FileToolError(f"第 {index + 1} 个问题必须是对象。", error_type="QuestionItemInvalid")
        question_id = str(raw.get("id", "")).strip()
        question_text = str(raw.get("question", "")).strip()
        if not question_id:
            raise FileToolError(f"第 {index + 1} 个问题缺少 id。", error_type="QuestionIdEmpty")
        if question_id in seen_ids:
            raise FileToolError(f"问题 id 重复：{question_id}", error_type="QuestionIdDuplicate")
        if not question_text:
            raise FileToolError(f"第 {index + 1} 个问题内容不能为空。", error_type="QuestionTextEmpty")
        options = _normalize_options(raw.get("options"), question_index=index + 1)
        if raw.get("custom") is True and not any(option["value"] == "__custom__" for option in options):
            # custom 使用固定 value，便于前后端识别自由输入分支。
            options.append({"value": "__custom__", "label": DEFAULT_CUSTOM_LABEL})
        seen_ids.add(question_id)
        questions.append(
            {
                "id": question_id,
                "question": question_text,
                "multiple": bool(raw.get("multiple", False)),
                "custom": bool(raw.get("custom", False)),
                "options": options,
            }
        )
    return questions


def _normalize_options(raw_options: Any, *, question_index: int) -> list[dict[str, str]]:
    if not isinstance(raw_options, list) or not raw_options:
        raise FileToolError(f"第 {question_index} 个问题必须提供选项。", error_type="QuestionOptionsInvalid")
    if len(raw_options) > MAX_OPTIONS:
        raise FileToolError(f"第 {question_index} 个问题最多允许 {MAX_OPTIONS} 个选项。", error_type="QuestionTooManyOptions")

    seen_values: set[str] = set()
    options: list[dict[str, str]] = []
    for option_index, raw in enumerate(raw_options):
        if not isinstance(raw, dict):
            raise FileToolError(f"第 {question_index} 个问题的第 {option_index + 1} 个选项必须是对象。", error_type="QuestionOptionInvalid")
        value = str(raw.get("value", "")).strip()
        label = str(raw.get("label", "")).strip()
        if not value or not label:
            raise FileToolError(f"第 {question_index} 个问题的选项 value/label 不能为空。", error_type="QuestionOptionEmpty")
        if value in seen_values:
            raise FileToolError(f"第 {question_index} 个问题的选项 value 重复：{value}", error_type="QuestionOptionDuplicate")
        seen_values.add(value)
        options.append({"value": value, "label": label})
    return options
