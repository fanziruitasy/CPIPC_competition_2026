"""加载并解析版本化 DOCX Docling 配置，凭证仅在内存中注入。"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class JsonSerializationConfig(_StrictModel):
    """Docling JSON 文本序列化设置。"""

    encoding: Literal["utf-8"] = "utf-8"
    ensure_ascii: Literal[False] = False
    indent: int = Field(default=2, ge=0)


class RuntimeConfig(_StrictModel):
    """DOCX 解析性能与调试设置。"""

    reuse_converter: Literal[True] = True
    timeout_seconds: None = None
    automatic_retries: Literal[0] = 0
    cpu_threads: int = Field(ge=1)
    torch_interop_threads: int = Field(ge=1)
    device: str
    perf: dict[str, int]
    debug: dict[str, bool]
    compile_torch_models: bool = False


class DoclingConfig(_StrictModel):
    """直接映射到 Docling Python API 的配置。"""

    allowed_formats: list[Literal["docx"]]
    pipeline_class: Literal["simple"]
    backend: Literal["msword"]
    backend_options: dict[str, Any]
    pipeline_options: dict[str, Any]


class ExportConfig(_StrictModel):
    """结构化 JSON、Markdown、HTML 和资产导出参数。"""

    image_mode: Literal["referenced"]
    markdown: dict[str, Any]
    json_options: dict[str, Any] = Field(default_factory=dict, alias="json")
    html: dict[str, Any]


class EnrichmentConnection(_StrictModel):
    """内联模型连接使用的环境变量名称。"""

    provider_env: str
    model_env: str
    base_url_env: str
    api_key_env: str
    timeout_seconds_env: str | None = None
    max_tokens_env: str | None = None
    temperature_env: str | None = None
    top_p_env: str | None = None


class EnrichmentConfig(_StrictModel):
    """DOCX 多模态增强和兜底策略。"""

    ocr_mode: Literal["disabled"]
    detect_pictures: bool
    detect_formulas: bool
    connection: EnrichmentConnection
    fallback_mode: Literal["none", "original-doc"]


class DocxParsingConfig(_StrictModel):
    """一份可版本管理的 DOCX 解析配置。"""

    schema_version: Literal["0.01"]
    source_profile: Literal["native-docx", "converted-docx"]
    parser_name: Literal["docling"]
    parser_version: Literal["2.120.1"]
    outputs: list[Literal["json", "md", "html"]]
    json_serialization: JsonSerializationConfig
    runtime: RuntimeConfig
    docling: DoclingConfig
    export: ExportConfig
    enrichment: EnrichmentConfig


def load_docx_parsing_config(path: Path) -> DocxParsingConfig:
    """读取并严格校验 DOCX Docling YAML 配置。

    :param path: 版本化 YAML 文件。
    :return: 不可变的配置模型。
    :raises FileNotFoundError: 配置不存在时抛出。
    :raises ValueError: YAML 根节点无效、输出不完整或版本不匹配时抛出。
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("DOCX 解析配置根节点必须是对象。")
    config = DocxParsingConfig.model_validate(payload)
    if set(config.outputs) != {"json", "md", "html"} or len(config.outputs) != 3:
        raise ValueError("DOCX 解析必须完整导出 json、md 和 html。")
    installed = version("docling")
    if installed != config.parser_version:
        raise RuntimeError(f"Docling 版本不匹配：配置 {config.parser_version}，当前 {installed}。")
    return config


def build_word_docling_options(
    config: DocxParsingConfig,
    *,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Any, Any]:
    """把 YAML 参数解析为 Docling Word Pipeline 和 Backend 对象。

    :param config: 已校验 DOCX 配置。
    :param env_file: 保存模型连接信息的可选 `.env` 文件。
    :param environ: 可注入的进程环境变量，主要用于测试。
    :return: ``(ConvertPipelineOptions, MsWordBackendOptions)``。
    :raises RuntimeError: 内联模型变量缺失或 Docling 字段不兼容时抛出。
    """
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.backend_options import MsWordBackendOptions
    from docling.datamodel.pipeline_options import ConvertPipelineOptions, PictureDescriptionApiOptions

    pipeline_raw = copy.deepcopy(config.docling.pipeline_options)
    description_raw = pipeline_raw.pop("picture_description_options", None)
    classification_raw = pipeline_raw.get("picture_classification_options")
    if isinstance(classification_raw, dict) and "engine_options" not in classification_raw:
        # 将项目配置的平铺写法适配为 Docling 2.120.1 嵌套引擎结构。
        pipeline_raw["picture_classification_options"] = {"engine_options": classification_raw}
    pipeline_raw["accelerator_options"] = AcceleratorOptions(
        num_threads=config.runtime.cpu_threads,
        device=config.runtime.device,
    )
    _reject_unknown(pipeline_raw, ConvertPipelineOptions.model_fields, "pipeline_options")
    pipeline_options = ConvertPipelineOptions.model_validate(pipeline_raw)
    if description_raw is not None:
        if not isinstance(description_raw, dict) or description_raw.pop("kind", None) != "api":
            raise ValueError("picture_description_options.kind 必须是 api。")
        connection = _load_model_connection(config, env_file=env_file, environ=environ)
        timeout_seconds = description_raw.pop("timeout_seconds", None)
        description_raw.update(connection)
        if timeout_seconds is not None and "timeout" not in connection:
            description_raw["timeout"] = timeout_seconds
        _reject_unknown(
            description_raw,
            PictureDescriptionApiOptions.model_fields,
            "picture_description_options",
        )
        pipeline_options.picture_description_options = PictureDescriptionApiOptions.model_validate(description_raw)
    _reject_unknown(config.docling.backend_options, MsWordBackendOptions.model_fields, "backend_options")
    backend_options = MsWordBackendOptions.model_validate(config.docling.backend_options)
    return pipeline_options, backend_options


def redacted_docx_options(config: DocxParsingConfig) -> dict[str, Any]:
    """生成不包含模型 URL、密钥和运行时环境值的配置快照。

    :param config: 已校验 DOCX 配置。
    :return: 可安全写入运行报告的普通字典。
    """
    return config.model_dump(mode="json", by_alias=True)


def _load_model_connection(
    config: DocxParsingConfig,
    *,
    env_file: Path | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    return resolve_model_api_connection(
        config.enrichment.connection,
        env_file=env_file,
        environ=environ,
    )


def resolve_model_api_connection(
    connection: EnrichmentConnection,
    *,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """从环境变量生成兼容 OpenAI Chat Completions 的 Docling 连接参数。

    :param connection: 只保存环境变量名称的连接配置。
    :param env_file: 可选 `.env` 文件。
    :param environ: 可注入环境映射，主要用于测试。
    :return: 仅存在于内存的 URL、请求头、模型参数和超时。
    :raises RuntimeError: 必需变量缺失时抛出。
    """
    values: dict[str, str] = {}
    if env_file is not None:
        values.update({key: str(value) for key, value in dotenv_values(env_file).items() if value is not None})
    values.update(dict(os.environ if environ is None else environ))
    def required(variable: str) -> str:
        value = values.get(variable, "").strip()
        if not value:
            raise RuntimeError(f"内联图片描述缺少环境变量：{variable}")
        return value

    base_url = required(connection.base_url_env).rstrip("/")
    if not base_url.endswith("/chat/completions"):
        base_url += "/chat/completions"
    params: dict[str, Any] = {"model": required(connection.model_env)}
    optional = {
        connection.max_tokens_env: ("max_tokens", int),
        connection.temperature_env: ("temperature", float),
        connection.top_p_env: ("top_p", float),
    }
    for variable, (name, converter) in optional.items():
        if variable and values.get(variable, "").strip():
            params[name] = converter(values[variable])
    timeout = None
    if connection.timeout_seconds_env and values.get(connection.timeout_seconds_env, "").strip():
        timeout = float(values[connection.timeout_seconds_env])
    result: dict[str, Any] = {
        "url": base_url,
        "headers": {"Authorization": f"Bearer {required(connection.api_key_env)}", "Content-Type": "application/json"},
        "params": params,
    }
    if timeout is not None:
        result["timeout"] = timeout
    required(connection.provider_env)
    return result


def _reject_unknown(raw: Mapping[str, Any], allowed: Mapping[str, Any], label: str) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"{label} 包含 Docling 2.120.1 不支持的字段：{unknown}")
