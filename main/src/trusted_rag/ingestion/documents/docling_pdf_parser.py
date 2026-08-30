"""按页面预检结果通过 Docling Standard Pipeline 解析 PDF。"""

from __future__ import annotations

import gc
import os
import shutil
import uuid
from importlib.metadata import version
from pathlib import Path
from typing import Any

from pydantic import Field

from trusted_rag.domain.common import ContractModel
from trusted_rag.domain.enums import QualityStatus, SourceFormat, SourceProfile
from trusted_rag.domain.knowledge import SourceDocument
from trusted_rag.infrastructure.artifacts import write_json_atomic
from trusted_rag.ingestion.documents.docling_artifacts import (
    DoclingParseArtifacts,
    inspect_docling_json,
    inventory_artifacts,
)
from trusted_rag.ingestion.documents.docling_export import export_docling_document
from trusted_rag.ingestion.documents.pdf_configuration import (
    PdfParsingConfig,
    build_pdf_pipeline_options,
)
from trusted_rag.ingestion.documents.pdf_pipeline import TrustedStandardPdfPipeline
from trusted_rag.ingestion.documents.pdf_preflight import PdfPreflightReport, inspect_pdf_pages


class PdfParseArtifacts(ContractModel):
    """PDF 页面预检和 Docling 解析产物。"""

    preflight: PdfPreflightReport
    parsed: DoclingParseArtifacts
    docling_errors: list[dict[str, Any]] = Field(default_factory=list)


class DoclingPdfParser:
    """每份 PDF 独立创建 Converter，避免原生后端状态和内存跨文件累积。"""

    def __init__(self, config: PdfParsingConfig, *, env_file: Path | None = None) -> None:
        """初始化 PDF 解析器。

        :param config: 版本化 PDF Docling 配置。
        :param env_file: 图片和公式模型连接环境文件。
        :return: 无。
        """
        self.config = config
        self.env_file = env_file
        self._apply_runtime_settings()

    def parse(
        self,
        source: SourceDocument,
        *,
        source_path: Path,
        output_root: Path,
    ) -> PdfParseArtifacts:
        """预检、路由、解析并原子发布单份 PDF。

        :param source: 已登记 PDF 来源。
        :param source_path: 实际解析的只读 PDF 路径。
        :param output_root: 本次运行解析产物根目录。
        :return: 页面预检、Docling 产物、质量报告和错误摘要。
        :raises ValueError: 来源格式不是 PDF 时抛出。
        :raises RuntimeError: Docling 返回失败状态时抛出。
        """
        if source.source_format is not SourceFormat.PDF:
            raise ValueError("DoclingPdfParser 只接受 PDF 来源。")
        preflight = inspect_pdf_pages(source_path)
        final_dir = output_root / source.source_id
        if final_dir.exists():
            raise FileExistsError(final_dir)
        temporary_dir = output_root / f".{source.source_id}.{uuid.uuid4().hex}.tmp"
        temporary_dir.mkdir(parents=True, exist_ok=False)
        converter: Any = None
        result: Any = None
        try:
            converter = self._build_converter(preflight.selected_backend)
            result = converter.convert(source=source_path, **self.config.docling.convert_options)
            if result.status.value not in {"success", "partial_success"}:
                raise RuntimeError(f"Docling 转换状态异常：{result.status.value}")
            export_docling_document(
                result.document,
                document_name=source.source_id,
                output_dir=temporary_dir,
                image_mode_name=self.config.export.image_mode,
                markdown_options=self.config.export.markdown,
                json_options=self.config.export.json_options,
                html_options=self.config.export.html,
            )
            quality = inspect_docling_json(
                temporary_dir / f"{source.source_id}.json",
                source_profile=SourceProfile.PDF,
            )
            if result.status.value == "partial_success" and quality.quality_status is QualityStatus.PASSED:
                quality = quality.model_copy(
                    update={
                        "quality_status": QualityStatus.REQUIRES_REVIEW,
                        "quality_flags": [*quality.quality_flags, "docling_partial_success"],
                        "requires_manual_review": True,
                        "review_reasons": [*quality.review_reasons, "Docling 返回部分成功，需人工抽查。"],
                    }
                )
            write_json_atomic(temporary_dir / "quality_report.json", quality)
            write_json_atomic(temporary_dir / "pdf_preflight.json", preflight)
            os.replace(temporary_dir, final_dir)
            errors = [_jsonable_error(item) for item in result.errors]
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise
        finally:
            result = None
            converter = None
            _release_runtime_memory()
        parsed = DoclingParseArtifacts(
            source_id=source.source_id,
            document_name=source.source_id,
            source_profile=SourceProfile.PDF,
            parser_version=version("docling"),
            files=inventory_artifacts(final_dir, relative_to=output_root),
            quality=quality,
        )
        return PdfParseArtifacts(preflight=preflight, parsed=parsed, docling_errors=errors)

    def _build_converter(self, backend_name: str) -> Any:
        from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption

        backends: dict[str, Any] = {
            "pypdfium2": PyPdfiumDocumentBackend,
            "docling_parse": DoclingParseDocumentBackend,
        }
        pipeline_options = build_pdf_pipeline_options(self.config, env_file=self.env_file)
        return DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options,
                    pipeline_cls=TrustedStandardPdfPipeline,
                    backend=backends[backend_name],
                )
            },
        )

    def _apply_runtime_settings(self) -> None:
        from docling.datamodel.settings import BatchConcurrencySettings, DebugSettings, settings

        runtime = self.config.runtime
        os.environ["OMP_NUM_THREADS"] = str(runtime.cpu_threads)
        os.environ["MKL_NUM_THREADS"] = str(runtime.cpu_threads)
        os.environ["NUMEXPR_NUM_THREADS"] = str(runtime.cpu_threads)
        settings.perf = BatchConcurrencySettings.model_validate(runtime.perf)
        settings.debug = DebugSettings.model_validate(runtime.debug)


def _jsonable_error(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
        return {str(key): item for key, item in payload.items()}
    return {"type": type(value).__name__, "message": str(value)}


def _release_runtime_memory() -> None:
    """回收单文档 Python 对象并释放 PyTorch 未占用的 CUDA 缓存。

    :return: 无。
    """
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        return
