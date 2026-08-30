"""固定三类现有 Docling JSON 的结构、定位和多模态行为。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCLING_RUNS = REPOSITORY_ROOT / "Data" / "staging" / "document_preprocessing" / "runs" / "docling"


@dataclass(frozen=True)
class ArtifactExpectation:
    """一份已验证 Docling JSON 的稳定结构预期。"""

    relative_path: str
    name: str
    texts: int
    tables: int
    pictures: int
    pages: int


EXPECTATIONS = (
    ArtifactExpectation(
        "converted-docx/v0.03/001/parsed_documents/393/393.json",
        "393",
        211,
        0,
        10,
        0,
    ),
    ArtifactExpectation(
        "native-docx/v0.04/001/parsed_documents/405/405.json",
        "405",
        339,
        4,
        4,
        0,
    ),
    ArtifactExpectation(
        "pdf/v0.06/001/parsed_documents/370/370.json",
        "370",
        4,
        1,
        0,
        1,
    ),
    ArtifactExpectation(
        "pdf/v0.06/001/parsed_documents/374/374.json",
        "374",
        89,
        0,
        6,
        76,
    ),
)


@pytest.mark.source_characterization
@pytest.mark.parametrize("expectation", EXPECTATIONS, ids=lambda item: item.name)
def test_existing_docling_structure(expectation: ArtifactExpectation) -> None:
    """代表性 JSON 必须保持 DoclingDocument 结构和已知元素数量。"""
    path = DOCLING_RUNS / expectation.relative_path
    if not path.is_file():
        pytest.skip("本地未提供现有 Docling 解析产物。")
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["schema_name"] == "DoclingDocument"
    assert document["version"] == "1.10.0"
    assert document["name"] == expectation.name
    assert len(document.get("texts", [])) == expectation.texts
    assert len(document.get("tables", [])) == expectation.tables
    assert len(document.get("pictures", [])) == expectation.pictures
    assert len(document.get("pages", {})) == expectation.pages
    assert document["body"]["self_ref"] == "#/body"
    assert document["body"]["children"]


@pytest.mark.source_characterization
def test_existing_table_picture_formula_locations() -> None:
    """表格、图片和公式保留 Docling 引用，PDF 元素保留页码定位。"""
    paths = {
        "table": DOCLING_RUNS / "pdf/v0.06/001/parsed_documents/370/370.json",
        "picture": DOCLING_RUNS / "pdf/v0.06/001/parsed_documents/374/374.json",
        "formula": DOCLING_RUNS / "native-docx/v0.04/001/parsed_documents/420/420.json",
    }
    if not all(path.is_file() for path in paths.values()):
        pytest.skip("本地未提供多模态 Docling 解析产物。")
    table_doc = json.loads(paths["table"].read_text(encoding="utf-8"))
    picture_doc = json.loads(paths["picture"].read_text(encoding="utf-8"))
    formula_doc = json.loads(paths["formula"].read_text(encoding="utf-8"))

    table = table_doc["tables"][0]
    picture = picture_doc["pictures"][0]
    formulas = [item for item in formula_doc["texts"] if item.get("label") == "formula"]
    assert table["self_ref"] == "#/tables/0"
    assert table["prov"][0]["page_no"] == 1
    assert picture["self_ref"] == "#/pictures/0"
    assert picture["prov"][0]["page_no"] >= 1
    assert len(formulas) == 10
    assert formulas[0]["self_ref"] == "#/texts/40"
    assert formulas[0]["text"] == "RC=max \\{\\{}V-C,0{\\}\\}"


@pytest.mark.source_characterization
@pytest.mark.parametrize(
    ("relative_manifest", "source_count"),
    [
        ("converted-docx/v0.03/001/reports/summary.json", 32),
        ("native-docx/v0.04/001/reports/summary.json", 34),
        ("pdf/v0.06/001/reports/summary.json", 45),
    ],
)
def test_existing_runs_preserved_all_source_hashes(
    relative_manifest: str,
    source_count: int,
) -> None:
    """现有三类全量运行都报告源文件摘要未改变。"""
    path = DOCLING_RUNS / relative_manifest
    if not path.is_file():
        pytest.skip("本地未提供 Docling 运行汇总。")
    summary = json.loads(path.read_text(encoding="utf-8"))

    assert summary["source_count"] == source_count
    assert summary["all_sources_unchanged"] is True

