"""加载 PDF Standard Pipeline 配置并安全注入图片与公式模型连接。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trusted_rag.ingestion.documents.docling_configuration import (
    EnrichmentConnection,
    ExportConfig,
    JsonSerializationConfig,
    resolve_model_api_connection,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PdfRuntimeConfig(_StrictModel):
    """PDF 单文档 Converter 生命周期和性能设置。"""

    converter_lifetime: Literal["per-document"]
    timeout_seconds: None = None
    automatic_retries: Literal[0] = 0
    cpu_threads: int = Field(ge=1)
    torch_interop_threads: int = Field(ge=1)
    device: str
    perf: dict[str, int]
    debug: dict[str, bool]
    compile_torch_models: bool = False


class BackendRoutingConfig(_StrictModel):
    """依据 Rotate 元数据选择 PDF 后端的策略。"""

    default_backend: Literal["pypdfium2"]
    rotated_page_backend: Literal["docling_parse"]


class PdfDoclingConfig(_StrictModel):
    """直接映射到 Docling PDF Python API 的配置。"""

    allowed_formats: list[Literal["pdf"]]
    pipeline_class: Literal["standard"]
    convert_options: dict[str, Any]
    pipeline_options: dict[str, Any]


class PdfEnrichmentConfig(_StrictModel):
    """PDF 图片和公式内联模型策略。"""

    detect_pictures: Literal[True]
    detect_formulas: Literal[True]
    connection: EnrichmentConnection


class PdfParsingConfig(_StrictModel):
    """一份可版本管理的 PDF 解析配置。"""

    schema_version: Literal["0.01"]
    source_profile: Literal["pdf"]
    parser_name: Literal["docling"]
    parser_version: Literal["2.120.1"]
    outputs: list[Literal["json", "md", "html"]]
    json_serialization: JsonSerializationConfig
    runtime: PdfRuntimeConfig
    backend_routing: BackendRoutingConfig
    docling: PdfDoclingConfig
    export: ExportConfig
    enrichment: PdfEnrichmentConfig


def load_pdf_parsing_config(path: Path) -> PdfParsingConfig:
    """读取并严格校验 PDF Docling 配置。

    :param path: PDF 版本化 YAML。
    :return: 不可变配置模型。
    :raises FileNotFoundError: 配置不存在时抛出。
    :raises ValueError: 配置结构或必要输出不符合约束时抛出。
    :raises RuntimeError: Docling 版本不匹配时抛出。
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("PDF 解析配置根节点必须是对象。")
    config = PdfParsingConfig.model_validate(payload)
    if set(config.outputs) != {"json", "md", "html"} or len(config.outputs) != 3:
        raise ValueError("PDF 解析必须完整导出 json、md 和 html。")
    installed = version("docling")
    if installed != config.parser_version:
        raise RuntimeError(f"Docling 版本不匹配：配置 {config.parser_version}，当前 {installed}。")
    return config


def build_pdf_pipeline_options(
    config: PdfParsingConfig,
    *,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """构造包含远程图片和公式增强的 ``PdfPipelineOptions``。

    :param config: 已校验 PDF 配置。
    :param env_file: 可选 `.env` 文件。
    :param environ: 可注入环境映射，主要用于测试。
    :return: Docling ``PdfPipelineOptions``。
    :raises ValueError: Docling 参数不支持或 API 引擎配置错误时抛出。
    """
    from docling.datamodel.pipeline_options import PdfPipelineOptions, PictureDescriptionApiOptions

    raw = copy.deepcopy(config.docling.pipeline_options)
    picture_raw = raw.pop("picture_description_options")
    if not isinstance(picture_raw, dict) or picture_raw.pop("kind", None) != "api":
        raise ValueError("PDF picture_description_options.kind 必须是 api。")
    connection = resolve_model_api_connection(
        config.enrichment.connection,
        env_file=env_file,
        environ=environ,
    )
    picture_raw.update(connection)
    _reject_unknown(picture_raw, PictureDescriptionApiOptions.model_fields, "picture_description_options")

    formula = raw.get("code_formula_options")
    if not isinstance(formula, dict):
        raise ValueError("PDF 必须配置 code_formula_options。")
    engine = formula.get("engine_options")
    if not isinstance(engine, dict) or engine.get("engine_type") != "api":
        raise ValueError("PDF 公式增强必须使用 API engine。")
    engine.update(connection)
    model_spec = formula.get("model_spec")
    if not isinstance(model_spec, dict) or not str(model_spec.get("prompt", "")).strip():
        raise ValueError("PDF 公式识别 Prompt 不能为空。")

    from docling.datamodel.accelerator_options import AcceleratorOptions

    raw["accelerator_options"] = AcceleratorOptions(
        num_threads=config.runtime.cpu_threads,
        device=config.runtime.device,
    )
    _reject_unknown(raw, PdfPipelineOptions.model_fields, "pipeline_options")
    options = PdfPipelineOptions.model_validate(raw)
    options.picture_description_options = PictureDescriptionApiOptions.model_validate(picture_raw)
    return options


def _reject_unknown(raw: Mapping[str, Any], allowed: Mapping[str, Any], label: str) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"{label} 包含 Docling 2.120.1 不支持的字段：{unknown}")
