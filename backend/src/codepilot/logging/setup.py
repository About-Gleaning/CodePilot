from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from codepilot.config.settings import LoggingSettings

_SENSITIVE_KEYS = {"api_key", "token", "password", "authorization", "cookie", "secret"}


def _redact_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key, value in list(event_dict.items()):
        if key.lower() in _SENSITIVE_KEYS and value is not None:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(settings: LoggingSettings, logs_dir: Path | None = None) -> None:
    handlers: list[logging.Handler] = []
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            handlers.append(logging.FileHandler(logs_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log", encoding="utf-8"))
        except OSError:
            # 日志目录不可写时降级为仅控制台输出，避免导入应用入口时直接失败。
            pass
    if not handlers:
        handlers.append(logging.StreamHandler())

    processors = _build_processors(settings)
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(serializer=_json_dumps) if settings.format == "json" else structlog.dev.ConsoleRenderer(),
        foreign_pre_chain=processors,
    )
    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=getattr(logging, settings.level.upper(), logging.INFO), handlers=handlers, force=True)
    structlog.configure(
        processors=[*processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _build_processors(settings: LoggingSettings) -> list[Any]:
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if settings.redact_secrets:
        processors.append(_redact_processor)
    processors.extend([structlog.processors.StackInfoRenderer(), structlog.processors.format_exc_info])
    return processors


def _json_dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(value, ensure_ascii=False, **kwargs)


def get_logger(name: str = "codepilot") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
