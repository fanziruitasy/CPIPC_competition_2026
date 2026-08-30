"""验证 PDF 配置、页面路由和三类代表解析产物。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trusted_rag.domain.enums import SourceProfile
from trusted_rag.ingestion.documents.docling_artifacts import inspect_docling_json
from trusted_rag.ingestion.documents.pdf_configuration import (
    build_pdf_pipeline_options,
    load_pdf_parsing_config,
)
from trusted_rag.ingestion.documents.pdf_preflight import inspect_pdf_pages

MAIN_ROOT = Path(__file__).resolve().parents[4]
WORKSPACE_ROOT = MAIN_ROOT.parent
CONFIG_PATH = MAIN_ROOT / "configs" / "ingestion" / "documents" / "pdf" / "v0.01.yaml"
SOURCE_ROOT = (
    WORKSPACE_ROOT
    / "Data"
    / "03-金融大模型与智能体赛道-南京银行-面向银行业监管制度与统计报表的可信RAG问答"
    / "nfra_page_attachments_500"
)
PARSED_ROOT = (
    WORKSPACE_ROOT
    / "Data"
    / "staging"
    / "document_preprocessing"
    / "runs"
    / "docling"
    / "pdf"
    / "v0.06"
    / "005"
    / "parsed_documents"
)


def _source_pdf(number: str) -> Path:
    matches = sorted(SOURCE_ROOT.rglob(f"{number}_*.pdf"))
    if not matches:
        pytest.skip(f"本地未提供 PDF {number}。")
    return matches[0]


def test_pdf_config_builds_picture_formula_and_heading_options() -> None:
    """最终 PDF 配置必须同时启用图片、公式、表格和标题层级能力。"""
    config = load_pdf_parsing_config(CONFIG_PATH)
    options = build_pdf_pipeline_options(
        config,
        environ={
            "VL_PROVIDER": "dashscope",
            "VL_MODEL": "qwen-vl-max",
            "DASHSCOPE_BASE_URL": "https://example.test/v1",
            "DASHSCOPE_API_KEY": "test-secret",
            "VL_TIMEOUT_SECONDS": "90",
            "VL_MAX_TOKENS": "1024",
            "VL_TEMPERATURE": "0.1",
            "VL_TOP_P": "0.8",
        },
    )

    assert options.do_picture_description is True
    assert options.do_formula_enrichment is True
    assert options.do_table_structure is True
    assert options.heading_hierarchy_options.enabled is True
    assert str(options.picture_description_options.url) == "https://example.test/v1/chat/completions"
    assert options.code_formula_options.engine_options.params["model"] == "qwen-vl-max"


@pytest.mark.source_characterization
def test_pdf_preflight_routes_plain_and_rotated_pages() -> None:
    """普通页面走 PDFium，含 Rotate 的页面走 docling_parse，横版不等同于旋转。"""
    plain = inspect_pdf_pages(_source_pdf("361"))
    rotated = inspect_pdf_pages(_source_pdf("370"))

    assert plain.selected_backend == "pypdfium2"
    assert plain.rotated_pages == []
    assert rotated.selected_backend == "docling_parse"
    assert [(item.page_number, item.rotation_degrees) for item in rotated.rotated_pages] == [(1, 90)]


@pytest.mark.source_characterization
@pytest.mark.parametrize(
    ("number", "expected_minimum_tables", "expected_minimum_pages"),
    [("361", 0, 1), ("370", 1, 1), ("374", 0, 70)],
)
def test_plain_rotated_table_and_complex_pdf_artifacts(
    number: str,
    expected_minimum_tables: int,
    expected_minimum_pages: int,
) -> None:
    """普通、旋转表格和复杂长文 PDF 都必须能恢复为 DoclingDocument。"""
    path = PARSED_ROOT / number / f"{number}.json"
    if not path.is_file():
        pytest.skip(f"本地未提供 PDF {number} 的解析产物。")

    report = inspect_docling_json(path, source_profile=SourceProfile.PDF)

    assert report.table_count >= expected_minimum_tables
    assert report.page_count >= expected_minimum_pages
    assert report.unresolved_body_references == []
