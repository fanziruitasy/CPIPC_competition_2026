"""通过 Docling Python API 解析原生与 DOC 转换型 DOCX。"""

from __future__ import annotations

import os
import shutil
import uuid
from importlib.metadata import version
from pathlib import Path
from typing import Any

from trusted_rag.domain.enums import QualityStatus, SourceFormat, SourceProfile
from trusted_rag.domain.knowledge import SourceDocument
from trusted_rag.infrastructure.artifacts import write_json_atomic
from trusted_rag.ingestion.documents.docling_artifacts import (
    DoclingParseArtifacts,
    inspect_docling_json,
    inventory_artifacts,
)
from trusted_rag.ingestion.documents.docling_configuration import (
    DocxParsingConfig,
    build_word_docling_options,
)
from trusted_rag.ingestion.documents.docling_export import export_docling_document


class DoclingDocxParser:
    """复用一个 DocumentConverter 并按文档原子发布 DOCX 解析结果。"""

    def __init__(self, config: DocxParsingConfig, *, env_file: Path | None = None) -> None:
        """初始化解析器并创建可复用的 Docling Converter。

        :param config: 原生或转换型 DOCX 的版本化配置。
        :param env_file: 内联图片描述所需环境变量文件。
        :return: 无。
        """
        self.config = config
        self._apply_runtime_settings()
        self.converter = self._build_converter(env_file)

    def parse(
        self,
        source: SourceDocument,
        *,
        source_path: Path,
        output_root: Path,
    ) -> DoclingParseArtifacts:
        """解析单个 DOCX 并导出 JSON、Markdown、HTML 和图片资产。

        :param source: 已登记且不可变的 DOCX 来源记录。
        :param source_path: 实际交给 Docling 的 DOCX 文件。
        :param output_root: 本次运行的解析产物根目录。
        :return: 包含哈希清单和质量状态的解析产物记录。
        :raises ValueError: 来源格式、profile 或输出不符合约束时抛出。
        :raises RuntimeError: Docling 转换未成功时抛出。
        """
        if source.source_format is not SourceFormat.DOCX:
            raise ValueError("DoclingDocxParser 只接受 DOCX 来源。")
        profile = SourceProfile(self.config.source_profile.replace("-", "_"))
        if profile is SourceProfile.CONVERTED_DOCX and source.converted_from_source_id is None:
            raise ValueError("converted-docx 必须保留原 DOC 的 source_id。")
        final_dir = output_root / source.source_id
        if final_dir.exists():
            raise FileExistsError(final_dir)
        temporary_dir = output_root / f".{source.source_id}.{uuid.uuid4().hex}.tmp"
        temporary_dir.mkdir(parents=True, exist_ok=False)
        try:
            result = self.converter.convert(source=source_path)
            if result.status.value not in {"success", "partial_success"}:
                raise RuntimeError(f"Docling 转换状态异常：{result.status.value}")
            self._export(result.document, source.source_id, temporary_dir)
            json_path = temporary_dir / f"{source.source_id}.json"
            quality = inspect_docling_json(json_path, source_profile=profile)
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
            os.replace(temporary_dir, final_dir)
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise
        files = inventory_artifacts(final_dir, relative_to=output_root)
        return DoclingParseArtifacts(
            source_id=source.source_id,
            document_name=source.source_id,
            source_profile=profile,
            parser_version=version("docling"),
            files=files,
            quality=quality,
        )

    def _build_converter(self, env_file: Path | None) -> Any:
        from docling.backend.msword_backend import MsWordDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, WordFormatOption
        from docling.pipeline.simple_pipeline import SimplePipeline

        pipeline_options, backend_options = build_word_docling_options(
            self.config,
            env_file=env_file,
        )
        return DocumentConverter(
            allowed_formats=[InputFormat.DOCX],
            format_options={
                InputFormat.DOCX: WordFormatOption(
                    pipeline_options=pipeline_options,
                    pipeline_cls=SimplePipeline,
                    backend=MsWordDocumentBackend,
                    backend_options=backend_options,
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

    def _export(self, document: Any, document_name: str, output_dir: Path) -> None:
        export_docling_document(
            document,
            document_name=document_name,
            output_dir=output_dir,
            image_mode_name=self.config.export.image_mode,
            markdown_options=self.config.export.markdown,
            json_options=self.config.export.json_options,
            html_options=self.config.export.html,
        )
