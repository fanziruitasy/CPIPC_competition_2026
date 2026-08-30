"""验证 DOC 转 DOCX 的命令、摘要和来源回溯。"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from trusted_rag.domain.enums import SourceKind
from trusted_rag.ingestion.documents.libreoffice_converter import (
    LibreOfficeConversionError,
    LibreOfficeDocConverter,
    convert_one_doc,
)
from trusted_rag.ingestion.source_registry import SourceRegistrar


def test_conversion_builds_traceable_converted_source(tmp_path: Path) -> None:
    """成功转换保留源摘要、输出摘要和原 DOC 来源关系。"""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source_path = source_root / "监管附件.doc"
    source_path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"legacy-word")
    source = SourceRegistrar(source_root).register(
        source_path,
        knowledge_base_id="kb_a",
        run_id="registration-run",
    )

    def runner(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "--version" in arguments:
            return subprocess.CompletedProcess(arguments, 0, "LibreOffice Test", "")
        output_dir = Path(arguments[arguments.index("--outdir") + 1])
        input_path = Path(arguments[-1])
        (output_dir / f"{input_path.stem}.docx").write_bytes(b"converted-docx")
        return subprocess.CompletedProcess(arguments, 0, "success", "")

    converter = LibreOfficeDocConverter(tmp_path / "soffice.com", runner=runner)
    record = converter.convert(
        source,
        source_root=source_root,
        converted_root=tmp_path / "run" / "converted",
        work_root=tmp_path / "run" / "work",
        profile_root=tmp_path / "run" / "profile",
        run_id="conversion-run",
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert record.source_unchanged is True
    assert record.converted_source.source_kind is SourceKind.CONVERTED
    assert record.converted_source.converted_from_source_id == source.source_id
    assert record.converted_source.source_sha256 == record.output_sha256
    assert record.output_relative_uri.startswith("converted/")
    assert record.libreoffice_version == "LibreOffice Test"


def test_success_without_non_empty_docx_is_rejected(tmp_path: Path) -> None:
    """命令返回零但没有非空输出时仍视为失败。"""
    source = tmp_path / "source.doc"
    source.write_bytes(b"legacy")

    def runner(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, "", "")

    with pytest.raises(LibreOfficeConversionError, match="未生成非空 DOCX"):
        convert_one_doc(
            executable=tmp_path / "soffice.com",
            staged_doc=source,
            output_dir=tmp_path / "output",
            profile_dir=tmp_path / "profile",
            export_filter="Office Open XML Text",
            timeout_seconds=None,
            runner=runner,
        )


def test_timeout_is_reported_as_conversion_error(tmp_path: Path) -> None:
    """配置超时时转换器返回稳定转换异常。"""
    source = tmp_path / "source.doc"
    source.write_bytes(b"legacy")

    def runner(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(arguments, 3)

    with pytest.raises(LibreOfficeConversionError, match="timeout_seconds=3"):
        convert_one_doc(
            executable=tmp_path / "soffice.com",
            staged_doc=source,
            output_dir=tmp_path / "output",
            profile_dir=tmp_path / "profile",
            export_filter="Office Open XML Text",
            timeout_seconds=3,
            runner=runner,
        )


def test_native_and_converted_docx_configs_share_parser_settings() -> None:
    """两类 DOCX 使用相同解析参数，只保留来源类型和兜底差异。"""
    main_root = Path(__file__).resolve().parents[4]
    config_root = main_root / "configs" / "ingestion" / "documents"
    native = yaml.safe_load(
        (config_root / "native_docx" / "v0.01.yaml").read_text(encoding="utf-8")
    )
    converted = yaml.safe_load(
        (config_root / "converted_docx" / "v0.01.yaml").read_text(encoding="utf-8")
    )

    assert native["source_profile"] == "native-docx"
    assert converted["source_profile"] == "converted-docx"
    assert native["runtime"] == converted["runtime"]
    assert native["docling"] == converted["docling"]
    assert native["export"] == converted["export"]
    assert native["enrichment"]["fallback_mode"] == "none"
    assert converted["enrichment"]["fallback_mode"] == "original-doc"
