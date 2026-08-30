"""固定若宇 Excel 清洗、事实提取、Parquet 与 DuckDB 的当前有效行为。"""

from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ZRY_PROJECT = REPOSITORY_ROOT / "ZRY" / "nfra_trusted_rag"


@pytest.mark.source_characterization
def test_regional_premium_keeps_metric_period_unit_and_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """地区保费事实必须保留规范指标、期间、单位和原始单元格。"""
    openpyxl = pytest.importorskip("openpyxl")
    cleaner = _load_zry_module("clean_region_premium", monkeypatch)
    workbook_path = tmp_path / "001_2024年3月全国各地区原保险保费收入情况表.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "原保险保费"
    sheet.append(["2024年3月全国各地区原保险保费收入情况表（单位：亿元）"])
    sheet.append(["地区", "合计", "财产保险", "寿险", "意外险", "健康险"])
    sheet.append(["全国", 100, 20, 50, 10, 20])
    workbook.save(workbook_path)
    workbook.close()

    facts, quality = cleaner.parse_file(workbook_path, 2024, 3, tmp_path)

    assert [fact["metric_code"] for fact in facts] == [
        "premium_total",
        "premium_property",
        "premium_life",
        "premium_accident",
        "premium_health",
    ]
    assert [fact["source_cell"] for fact in facts] == ["B3", "C3", "D3", "E3", "F3"]
    assert all(fact["period_start"] == date(2024, 1, 1) for fact in facts)
    assert all(fact["period_end"] == date(2024, 3, 31) for fact in facts)
    assert all(fact["period_basis"] == "YTD" for fact in facts)
    assert all(fact["unit"] == "CNY_100M" for fact in facts)
    assert facts[0]["value"] == Decimal("100")
    assert quality["fact_count"] == 5


@pytest.mark.source_characterization
def test_excel_document_keeps_formula_and_marks_missing_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公式文本必须映射回单元格，缺少缓存值时必须进入质量告警。"""
    openpyxl = pytest.importorskip("openpyxl")
    documents = _load_zry_module("clean_excel_documents", monkeypatch)
    workbook_path = tmp_path / "999_公式样例.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "统计表"
    sheet.append(["指标", "一季度", "二季度", "合计"])
    sheet.append(["贷款余额", 10, 20, "=SUM(B2:C2)"])
    workbook.save(workbook_path)
    workbook.close()

    cells, rows, chunks, quality = documents.parse_file(workbook_path, tmp_path)

    formula_cell = next(cell for cell in cells if cell["source_cell"] == "D2")
    assert formula_cell["formula_raw"] == "=SUM(B2:C2)"
    assert formula_cell["value_text"] == ""
    assert formula_cell["value_type"] == "formula_without_cached_value"
    assert rows[-1]["source_range"] == "A2:D2"
    assert "=SUM(B2:C2)" in rows[-1]["row_text"]
    assert chunks[0]["source_range"] == "A1:D2"
    assert quality["formula_cell_count"] == 1
    assert quality["formula_cache_missing_count"] == 1
    assert quality["status"] == "FORMULA_CACHE_MISSING"


@pytest.mark.source_characterization
def test_zry_fact_parquet_preserves_decimal_formula_and_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事实写入 Parquet 后必须无损保留十进制值、公式和单元格。"""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    cleaner = _load_zry_module("clean_region_premium", monkeypatch)
    record = {
        field.name: None
        for field in cleaner.FACT_SCHEMA
    }
    record.update(
        {
            "release_id": "2024-03",
            "period_start": date(2024, 1, 1),
            "period_end": date(2024, 3, 31),
            "period_basis": "YTD",
            "region_code": "CN",
            "region_name": "全国",
            "region_name_raw": "全国",
            "region_type": "national",
            "region_order": 0,
            "metric_code": "premium_total",
            "metric_name": "原保险保费收入合计",
            "metric_name_raw": "合计",
            "metric_order": 0,
            "value": Decimal("123.456789"),
            "value_status": "reported",
            "unit": "CNY_100M",
            "schema_version": "V2_2024",
            "scope_version": "HQ_INCLUDED",
            "statistical_scope_version": "STANDARD_PUBLISHED_SCOPE",
            "accounting_basis_version": "2009_INSURANCE_ACCOUNTING",
            "source_file": "001.xlsx",
            "source_file_hash": "a" * 64,
            "source_format": "xlsx",
            "source_origin": "local_attachment",
            "source_url": "",
            "source_sheet": "原保险保费",
            "source_cell": "B3",
            "formula_raw": "=SUM(B4:B40)",
            "number_format": "0.000000",
            "footnotes": "",
            "quality_flags": "",
        }
    )
    parquet_path = tmp_path / "facts.parquet"
    pq.write_table(pa.Table.from_pylist([record], schema=cleaner.FACT_SCHEMA), parquet_path)

    restored = pq.read_table(parquet_path).to_pylist()[0]

    assert restored["value"] == Decimal("123.456789")
    assert restored["formula_raw"] == "=SUM(B4:B40)"
    assert restored["source_cell"] == "B3"


@pytest.mark.source_characterization
def test_zry_duckdb_build_keeps_numeric_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DuckDB 构建后必须可按指标查询精确数值及其单元格证据。"""
    duckdb = pytest.importorskip("duckdb")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    builder = _load_zry_module("build_duckdb", monkeypatch)
    input_root = tmp_path / "clean"
    for name in ("facts", "dictionaries", "templates", "references"):
        (input_root / name).mkdir(parents=True, exist_ok=True)

    _write_parquet(
        pq,
        pa,
        input_root / "source_catalog.parquet",
        [{"source_file": "001_监管统计表.xlsx"}],
    )
    _write_parquet(
        pq,
        pa,
        input_root / "facts" / "facts.parquet",
        [
            {
                "source_file": "001_监管统计表.xlsx",
                "source_sheet": "资产负债表",
                "period_end": date(2024, 3, 31),
                "metric_name": "资产总额",
                "metric_name_raw": "资产总额",
                "value": Decimal("123.450000"),
                "unit": "亿元",
                "source_cell": "C8",
            }
        ],
    )
    document = {
        "source_file": "001_监管统计表.xlsx",
        "source_file_hash": "b" * 64,
        "source_format": "xlsx",
        "source_sheet": "说明",
        "row_number": 1,
        "source_range": "A1:B1",
        "row_text": "口径 | 境内汇总",
    }
    _write_parquet(pq, pa, input_root / "templates" / "template.parquet", [document])
    _write_parquet(pq, pa, input_root / "references" / "reference.parquet", [document])
    _write_parquet(
        pq,
        pa,
        input_root / "dictionaries" / "metric_definitions.parquet",
        [
            {
                "source_file": "001_监管统计表.xlsx",
                "source_sheet": "词典",
                "metric_name": "资产总额",
                "definition": "资产合计",
            }
        ],
    )
    _write_parquet(
        pq,
        pa,
        input_root / "dictionaries" / "institution_scopes.parquet",
        [
            {
                "source_file": "001_监管统计表.xlsx",
                "source_sheet": "词典",
                "institution_type": "商业银行",
                "scope_definition": "商业银行法人机构",
            }
        ],
    )
    _write_parquet(
        pq,
        pa,
        input_root / "dictionaries" / "release_schedule.parquet",
        [
            {
                "source_file": "001_监管统计表.xlsx",
                "source_sheet": "词典",
                "indicator_names": "资产总额",
                "notes": "季度发布",
            }
        ],
    )
    database_path = tmp_path / "facts.duckdb"

    summary = builder.build(input_root, database_path)
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        result = connection.execute(
            "SELECT value, unit, source_sheet, source_cell FROM facts WHERE metric_name = ?",
            ["资产总额"],
        ).fetchone()
    finally:
        connection.close()

    assert summary == {
        "db_path": str(database_path.resolve()),
        "source_count": 1,
        "fact_count": 1,
        "document_count": 2,
    }
    assert result == (Decimal("123.450000"), "亿元", "资产负债表", "C8")


def _load_zry_module(name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    if not (ZRY_PROJECT / f"{name}.py").is_file():
        pytest.skip("本地未提供若宇来源代码。")
    monkeypatch.syspath_prepend(str(ZRY_PROJECT))
    return importlib.import_module(name)


def _write_parquet(pq: ModuleType, pa: ModuleType, path: Path, rows: list[dict[str, object]]) -> None:
    pq.write_table(pa.Table.from_pylist(rows), path)
