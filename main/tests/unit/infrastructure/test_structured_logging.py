"""验证 UTF-8 结构化日志、上下文绑定和递归脱敏。"""

import json
import logging
from io import StringIO

from trusted_rag.infrastructure.configuration import LoggingSettings
from trusted_rag.infrastructure.structured_logging import configure_logging, log_context, sanitize_for_logging


def test_sanitize_for_logging_redacts_nested_secrets() -> None:
    """任意深度的常见凭证字段都必须脱敏。"""
    payload = sanitize_for_logging(
        {"question": "监管要求是什么？", "nested": {"api_key": "secret", "access_token": "token-value"}}
    )

    assert payload["question"] == "监管要求是什么？"
    assert payload["nested"]["api_key"] == "***REDACTED***"
    assert payload["nested"]["access_token"] == "***REDACTED***"


def test_json_logging_preserves_chinese_and_context() -> None:
    """JSON 日志应保留中文并自动加入追踪上下文。"""
    stream = StringIO()
    configure_logging(LoggingSettings(), stream=stream)
    logger = logging.getLogger("trusted_rag.test")

    with log_context(trace_id="trace_test", run_id="run_test"):
        logger.info("解析完成", extra={"metadata": {"password": "secret", "file_name": "监管制度.pdf"}})

    raw_log = stream.getvalue().strip()
    payload = json.loads(raw_log)
    assert "解析完成" in raw_log
    assert "\\u89e3" not in raw_log
    assert payload["trace_id"] == "trace_test"
    assert payload["run_id"] == "run_test"
    assert payload["context"]["metadata"]["password"] == "***REDACTED***"
    assert "secret" not in raw_log

