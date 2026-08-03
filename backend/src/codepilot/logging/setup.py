from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from codepilot.config.settings import LoggingSettings

_SENSITIVE_KEYS = {"api_key", "token", "password", "authorization", "cookie", "secret"}
_BEARER_PATTERN = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_API_KEY_PATTERN = re.compile(r"\b(?:sk|key)-[A-Za-z0-9_-]{12,}\b")
_DATA_URL_PATTERN = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+")
_URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@")


def _redact_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _redact_value(event_dict)


def _redact_value(value: Any, *, key: str = "") -> Any:
    if key.lower() in _SENSITIVE_KEYS and value is not None:
        return "***REDACTED***"
    if isinstance(value, dict):
        return {item_key: _redact_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if not isinstance(value, str):
        return value
    redacted = _BEARER_PATTERN.sub("Bearer ***REDACTED***", value)
    redacted = _API_KEY_PATTERN.sub("***REDACTED***", redacted)
    redacted = _DATA_URL_PATTERN.sub("[image data url redacted]", redacted)
    redacted = _URL_CREDENTIAL_PATTERN.sub(r"\1***REDACTED***@", redacted)
    home = str(Path.home())
    return redacted.replace(home, "<USER_HOME>") if home else redacted


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
    processors.extend([structlog.processors.StackInfoRenderer(), structlog.processors.format_exc_info])
    # 异常格式化可能把底层 URL 或认证头带入字符串，因此脱敏必须位于最后。
    if settings.redact_secrets:
        processors.append(_redact_processor)
    return processors


def _json_dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(value, ensure_ascii=False, **kwargs)


def get_logger(name: str = "codepilot") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
