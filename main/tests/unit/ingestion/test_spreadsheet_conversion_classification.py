"""验证 XLS 转换完整性和工作簿、表区分类。"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from trusted_rag.domain.enums import SourceFormat, SourceKind
from trusted_rag.ingestion.source_registry import SourceRegistrar
from trusted_rag.ingestion.spreadsheets.classification import (
    classify_workbook,
    detect_table_regions,
)
from trusted_rag.ingestion.spreadsheets.libreoffice_converter import (
    LibreOfficeSpreadsheetConverter,
)


def test_xls_conversion_builds_traceable_xlsx_source(tmp_path: Path) -> None:
    """成功转换保留源摘要、输出摘要和原 XLS 来源关系。"""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source_path = source_root / "监管统计表.xls"
    source_path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"legacy-excel")
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
        (output_dir / f"{input_path.stem}.xlsx").write_bytes(b"converted-xlsx")
        return subprocess.CompletedProcess(arguments, 0, "success", "")

    record = LibreOfficeSpreadsheetConverter(
        tmp_path / "soffice.com",
        runner=runner,
    ).convert(
        source,
        source_root=source_root,
        converted_root=tmp_path / "run" / "converted",
        work_root=tmp_path / "run" / "work",
        profile_root=tmp_path / "run" / "profile",
        run_id="conversion-run",
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert record.source_unchanged is True
    assert record.converted_source.source_format is SourceFormat.XLSX
    assert record.converted_source.source_kind is SourceKind.CONVERTED
    assert record.converted_source.converted_from_source_id == source.source_id
    assert record.converted_source.source_sha256 == record.output_sha256
    assert record.output_relative_uri.startswith("converted/")


def test_duplicate_xls_content_reuses_verified_conversion(tmp_path: Path) -> None:
    """内容相同的两个 XLS 来源应复用转换产物并保留各自原文件名。"""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    left_path = source_root / "376_发布日程及指标解释.xls"
    right_path = source_root / "377_发布日程表.xls"
    payload = bytes.fromhex("D0CF11E0A1B11AE1") + b"same-legacy-excel"
    left_path.write_bytes(payload)
    right_path.write_bytes(payload)
    registrar = SourceRegistrar(source_root)
    left = registrar.register(left_path, knowledge_base_id="kb_a", run_id="registration-run")
    right = registrar.register(right_path, knowledge_base_id="kb_a", run_id="registration-run")

    def runner(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "--version" in arguments:
            return subprocess.CompletedProcess(arguments, 0, "LibreOffice Test", "")
        output_dir = Path(arguments[arguments.index("--outdir") + 1])
        input_path = Path(arguments[-1])
        (output_dir / f"{input_path.stem}.xlsx").write_bytes(b"converted-xlsx")
        return subprocess.CompletedProcess(arguments, 0, "success", "")

    converter = LibreOfficeSpreadsheetConverter(tmp_path / "soffice.com", runner=runner)
    arguments = {
        "source_root": source_root,
        "converted_root": tmp_path / "run" / "converted",
        "work_root": tmp_path / "run" / "work",
        "profile_root": tmp_path / "run" / "profile",
        "run_id": "conversion-run",
        "libreoffice_version": "LibreOffice Test",
        "created_at": datetime(2026, 8, 30, tzinfo=UTC),
    }
    first = converter.convert(left, **arguments)
    second = converter.convert(right, **arguments)

    assert left.source_id == right.source_id
    assert second.output_sha256 == first.output_sha256
    assert second.output_relative_uri == first.output_relative_uri
    assert second.converted_source.original_file_name == right_path.name


def test_workbook_classification_keeps_zry_routes() -> None:
    """已验证的月报、监管表、模板和参考资料必须进入固定业务路由。"""
    regional = classify_workbook(Path("001_2024年3月全国各地区原保险保费收入情况表.xlsx"))
    regulatory = classify_workbook(Path("002_2024年商业银行主要监管指标情况表.xlsx"))
    template = classify_workbook(Path("394_报送模板.xls"))
    reference = classify_workbook(Path("391_参考名单.xlsx"))

    assert (regional.dataset_id, regional.year, regional.month) == (
        "regional_premium_monthly",
        2024,
        3,
    )
    assert regulatory.dataset_id == "commercial_bank_regulatory_indicators"
    assert template.role == "template"
    assert reference.role == "reference"


def test_detect_table_regions_splits_at_blank_rows() -> None:
    """空行分隔的两个内容区域必须形成两个可定位表区。"""
    regions = detect_table_regions(
        "统计表",
        [
            ["指标", "数值"],
            ["资产", 10],
            [None, None],
            ["说明"],
        ],
    )

    assert len(regions) == 2
    assert regions[0].model_dump() == {
        "sheet_name": "统计表",
        "start_row": 1,
        "end_row": 2,
        "start_column": 1,
        "end_column": 2,
        "header_row": 1,
        "nonempty_cell_count": 4,
    }
    assert regions[1].start_row == 4
