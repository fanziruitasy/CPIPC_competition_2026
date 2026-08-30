"""验证配置优先级、字段校验和凭证保护。"""

from pathlib import Path

import pytest

from trusted_rag.infrastructure.configuration import load_settings, settings_for_logging
from trusted_rag.infrastructure.errors import ErrorCode, TrustedRagError


def test_configuration_priority(tmp_path: Path) -> None:
    """显式参数应覆盖环境变量，环境变量应覆盖 YAML。"""
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "application:\n  environment: yaml\nlogging:\n  level: WARNING\n",
        encoding="utf-8",
    )

    settings = load_settings(
        config_path,
        environ={"TRUSTED_RAG_ENVIRONMENT": "environment", "TRUSTED_RAG_LOG_LEVEL": "ERROR"},
        overrides={"application": {"environment": "explicit"}},
    )

    assert settings.application.environment == "explicit"
    assert settings.logging.level == "ERROR"


def test_settings_for_logging_redacts_api_key() -> None:
    """安全配置副本不得暴露 DashScope 密钥。"""
    settings = load_settings(environ={"DASHSCOPE_API_KEY": "secret-value"})

    payload = settings_for_logging(settings)

    assert payload["services"]["dashscope_api_key"] == "***REDACTED***"
    assert "secret-value" not in str(payload)


def test_invalid_configuration_uses_stable_error_code(tmp_path: Path) -> None:
    """未知配置字段应转换为稳定错误码。"""
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("unknown: true\n", encoding="utf-8")

    with pytest.raises(TrustedRagError) as captured:
        load_settings(config_path, environ={})

    assert captured.value.code is ErrorCode.CONFIG_INVALID

