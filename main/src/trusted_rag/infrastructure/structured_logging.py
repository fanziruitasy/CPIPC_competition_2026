"""提供 UTF-8 JSON 日志、追踪上下文和递归敏感字段脱敏。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

from trusted_rag.infrastructure.configuration import LoggingSettings

_TRACE_ID: ContextVar[str | None] = ContextVar("trace_id", default=None)
_RUN_ID: ContextVar[str | None] = ContextVar("run_id", default=None)
_SENSITIVE_MARKERS = ("api_key", "apikey", "authorization", "credential", "password", "secret", "token")
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def sanitize_for_logging(value: Any) -> Any:
    """递归掩盖映射中的凭证字段，并保留普通中文内容。

    :param value: 任意日志字段值。
    :return: 可安全写入日志的等价结构。
    """
    if isinstance(value, Mapping):
        return {
            str(key): "***REDACTED***" if _is_sensitive_key(str(key)) else sanitize_for_logging(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_for_logging(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_logging(item) for item in value]
    return value


@contextmanager
def log_context(*, trace_id: str | None = None, run_id: str | None = None) -> Iterator[None]:
    """在当前执行上下文内绑定追踪标识和运行标识。

    :param trace_id: 可选请求或任务追踪标识。
    :param run_id: 可选知识构建或评测运行标识。
    :return: 用于 ``with`` 语句的上下文迭代器。
    """
    trace_token = _TRACE_ID.set(trace_id) if trace_id is not None else None
    run_token = _RUN_ID.set(run_id) if run_id is not None else None
    try:
        yield
    finally:
        if run_token is not None:
            _RUN_ID.reset(run_token)
        if trace_token is not None:
            _TRACE_ID.reset(trace_token)


class JsonFormatter(logging.Formatter):
    """将标准日志记录转换为单行 UTF-8 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化一条日志并附加上下文与扩展字段。

        :param record: Python 标准库日志记录。
        :return: 不转义中文的单行 JSON 字符串。
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = _TRACE_ID.get()
        run_id = _RUN_ID.get()
        if trace_id is not None:
            payload["trace_id"] = trace_id
        if run_id is not None:
            payload["run_id"] = run_id

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_FIELDS and not key.startswith("_")
        }
        if extras:
            payload["context"] = sanitize_for_logging(extras)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def configure_logging(settings: LoggingSettings, *, stream: TextIO | None = None) -> None:
    """以单一处理器配置进程根日志器，避免模块重复添加处理器。

    :param settings: 已校验的日志配置。
    :param stream: 可选输出流，默认由标准库写入标准错误。
    :return: 无。
    """
    handler = logging.StreamHandler(stream)
    if settings.json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.level)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)

