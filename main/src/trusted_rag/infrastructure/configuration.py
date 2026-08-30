"""加载默认值、YAML、环境变量和显式参数组成的统一配置。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from trusted_rag.infrastructure.errors import ErrorCode, TrustedRagError


class ApplicationSettings(BaseModel):
    """应用名称、环境和时区配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = "trusted-rag"
    environment: str = "development"
    timezone: str = "Asia/Shanghai"


class RuntimeSettings(BaseModel):
    """运行产物根目录和配置版本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_root: Path = Path("data_runtime")
    config_version: str = "0.01"


class LoggingSettings(BaseModel):
    """结构化日志输出配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: str = "INFO"
    json_output: bool = True
    include_context: bool = True

    @field_validator("level")
    @classmethod
    def normalize_level(cls, value: str) -> str:
        """规范化并校验日志级别。

        :param value: 用户配置的日志级别。
        :return: 大写后的有效日志级别。
        :raises ValueError: 日志级别不受支持时抛出。
        """
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("日志级别必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL。")
        return normalized


class ServiceSettings(BaseModel):
    """外部模型、向量库和事实库连接配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dashscope_api_key: SecretStr | None = None
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_native_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    qdrant_url: str = "http://localhost:6333"
    rag_db_path: Path = Path("data_runtime/duckdb/nfra.duckdb")


class AppSettings(BaseModel):
    """可信 RAG 进程使用的完整基础配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "0.01"
    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    services: ServiceSettings = Field(default_factory=ServiceSettings)


_ENVIRONMENT_BINDINGS: dict[str, tuple[str, str]] = {
    "TRUSTED_RAG_ENVIRONMENT": ("application", "environment"),
    "TZ": ("application", "timezone"),
    "TRUSTED_RAG_DATA_ROOT": ("runtime", "data_root"),
    "TRUSTED_RAG_CONFIG_VERSION": ("runtime", "config_version"),
    "TRUSTED_RAG_LOG_LEVEL": ("logging", "level"),
    "DASHSCOPE_API_KEY": ("services", "dashscope_api_key"),
    "DASHSCOPE_BASE_URL": ("services", "dashscope_base_url"),
    "DASHSCOPE_NATIVE_BASE_URL": ("services", "dashscope_native_base_url"),
    "QDRANT_URL": ("services", "qdrant_url"),
    "RAG_DB_PATH": ("services", "rag_db_path"),
}


def load_settings(
    config_path: Path | str | None = None,
    *,
    env_file: Path | str | None = None,
    overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppSettings:
    """按默认值、YAML、环境变量、显式参数的顺序加载配置。

    :param config_path: 可选版本化 YAML 配置文件。
    :param env_file: 可选 `.env` 文件；真实凭证不会写入配置快照。
    :param overrides: 最高优先级的嵌套显式参数，供命令行适配层传入。
    :param environ: 可替代当前进程环境的映射，主要用于测试。
    :return: 已完成类型校验且不可变的应用配置。
    :raises TrustedRagError: 配置文件不存在、格式错误或字段校验失败时抛出。
    """
    merged = AppSettings().model_dump(mode="python")
    if config_path is not None:
        _deep_merge(merged, _load_yaml(Path(config_path)))

    environment_values: dict[str, str] = {}
    if env_file is not None:
        environment_values.update(
            {key: value for key, value in dotenv_values(env_file).items() if value is not None}
        )
    environment_values.update(dict(os.environ if environ is None else environ))
    _deep_merge(merged, _environment_payload(environment_values))
    if overrides:
        _deep_merge(merged, deepcopy(dict(overrides)))

    try:
        return AppSettings.model_validate(merged)
    except ValidationError as exc:
        raise TrustedRagError(
            ErrorCode.CONFIG_INVALID,
            "应用配置未通过校验。",
            details={"validation_errors": exc.errors(include_url=False)},
        ) from exc


def settings_for_logging(settings: AppSettings) -> dict[str, Any]:
    """生成可写入日志且不会暴露凭证的配置副本。

    :param settings: 已校验的应用配置。
    :return: 适合记录的普通字典，密钥字段固定显示为掩码。
    """
    payload = settings.model_dump(mode="json")
    if settings.services.dashscope_api_key is not None:
        payload["services"]["dashscope_api_key"] = "***REDACTED***"
    return payload


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise TrustedRagError(ErrorCode.CONFIG_FILE_NOT_FOUND, f"配置文件不存在：{path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TrustedRagError(
            ErrorCode.CONFIG_INVALID,
            f"无法读取配置文件：{path}",
            details={"exception_type": type(exc).__name__},
        ) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise TrustedRagError(ErrorCode.CONFIG_INVALID, "YAML 配置根节点必须是对象。")
    return payload


def _environment_payload(values: Mapping[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for variable, path in _ENVIRONMENT_BINDINGS.items():
        value = values.get(variable)
        if value is None or value == "":
            continue
        cursor = payload
        for key in path[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[path[-1]] = value
    return payload


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)

