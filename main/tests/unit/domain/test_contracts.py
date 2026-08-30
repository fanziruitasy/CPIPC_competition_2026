"""验证统一 v1 契约的模式、序列化和业务约束。"""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from tests.unit.domain.conftest import DOCUMENT_ID, EVIDENCE_ID, NOW, SOURCE_ID, lineage
from trusted_rag.domain.common import LineageMetadata, ModelGeneratedContent, SourceLocation, model_to_utf8_json
from trusted_rag.domain.enums import (
    ChunkContentType,
    FormulaCacheStatus,
    ModelContentStatus,
    ValueType,
)
from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import ChunkRecord, SourceDocument, TableFact
from trusted_rag.domain.query import AnswerRecord, QueryPlan


def test_all_core_contracts_expose_v1_schema(
    source_document: SourceDocument,
    chunk_record: ChunkRecord,
    query_plan: QueryPlan,
    answer_record: AnswerRecord,
) -> None:
    """最终核心记录应公开统一 v1 模式且不含 Learned Sparse。"""
    assert source_document.schema_version == "source_document.v1"
    assert chunk_record.schema_version == "chunk.v1"
    assert query_plan.schema_version == "query_plan.v1"
    assert answer_record.schema_version == "answer.v1"
    assert "learned_sparse" not in str(ChunkRecord.model_json_schema())


def test_contract_json_preserves_chinese(chunk_record: ChunkRecord) -> None:
    """契约 JSON 应直接显示中文而不是 Unicode 转义。"""
    payload = model_to_utf8_json(chunk_record)

    assert "商业银行" in payload
    assert "\\u5546" not in payload


def test_unknown_fields_are_rejected(source_document: SourceDocument) -> None:
    """未知字段不得被静默忽略。"""
    payload = source_document.model_dump(mode="python")
    payload["legacy_doc_id"] = "385"

    with pytest.raises(ValidationError):
        SourceDocument.model_validate(payload)


def test_source_path_must_be_relative(source_document: SourceDocument) -> None:
    """统一来源契约不得保存宿主机绝对路径。"""
    payload = source_document.model_dump(mode="python")
    payload["relative_path"] = r"E:\CPIPC_competition_2026\Data\385.docx"

    with pytest.raises(ValidationError):
        SourceDocument.model_validate(payload)


def test_docx_location_can_use_clause_without_fake_page(chunk_record: ChunkRecord) -> None:
    """DOCX 引用可以依靠章节、条款和元素位置而不伪造页码。"""
    location = chunk_record.locations[0]

    assert location.page_number is None
    assert location.clause == "第十二条"
    assert location.docling_ref == "#/texts/12"


def test_table_fact_preserves_raw_and_decimal_string() -> None:
    """银行统计数值应同时保留原值和高精度十进制字符串。"""
    fact = TableFact(
        fact_id=stable_id("fact", DOCUMENT_ID, "Sheet1", "B3"),
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        table_id="table_1",
        metric_code="total_assets",
        metric_name="资产总额",
        period_end=date(2026, 6, 30),
        raw_value="12345.678900",
        normalized_value="12345.678900",
        value_type=ValueType.DECIMAL,
        unit="亿元",
        evidence_id=stable_id("evidence", DOCUMENT_ID, "Sheet1", "B3"),
        location=SourceLocation(sheet_name="资产负债表", cell_range="B3"),
        lineage=lineage(DOCUMENT_ID),
    )

    assert fact.raw_value == "12345.678900"
    assert fact.normalized_value == "12345.678900"
    assert isinstance(fact.normalized_value, str)


def test_invalid_decimal_string_is_rejected() -> None:
    """无法精确解析的规范数值必须被拒绝。"""
    with pytest.raises(ValidationError):
        TableFact(
            fact_id=stable_id("fact", DOCUMENT_ID, "Sheet1", "B3"),
            source_id=SOURCE_ID,
            document_id=DOCUMENT_ID,
            table_id="table_1",
            raw_value="一百",
            normalized_value="一百",
            value_type=ValueType.DECIMAL,
            evidence_id=EVIDENCE_ID,
            location=SourceLocation(sheet_name="资产负债表", cell_range="B3"),
            lineage=lineage(DOCUMENT_ID),
        )


def test_formula_fact_requires_formula_and_cache_state() -> None:
    """公式事实必须保留公式文本并明确缓存状态。"""
    with pytest.raises(ValidationError):
        TableFact(
            fact_id=stable_id("fact", DOCUMENT_ID, "Sheet1", "C3"),
            source_id=SOURCE_ID,
            document_id=DOCUMENT_ID,
            table_id="table_1",
            raw_value="3",
            normalized_value="3",
            value_type=ValueType.DECIMAL,
            is_formula=True,
            formula_cache_status=FormulaCacheStatus.NOT_FORMULA,
            evidence_id=EVIDENCE_ID,
            location=SourceLocation(sheet_name="资产负债表", cell_range="C3"),
            lineage=lineage(DOCUMENT_ID),
        )


def test_unvalidated_model_description_cannot_enter_retrieval_text(chunk_record: ChunkRecord) -> None:
    """未通过校验的大模型描述不得用于 Dense 或 BM25。"""
    payload = chunk_record.model_dump(mode="python")
    payload["content_type"] = ChunkContentType.PICTURE
    payload["model_generated_content_used"] = True
    payload["model_generated_content"] = [
        ModelGeneratedContent(
            text="图片展示资本充足率变化。",
            model_name="qwen-vl",
            validation_status=ModelContentStatus.REJECTED,
            original_element_ref="#/pictures/1",
            generated_at=NOW,
        ).model_dump(mode="python")
    ]

    with pytest.raises(ValidationError):
        ChunkRecord.model_validate(payload)


def test_stable_identity_ignores_mapping_insertion_order() -> None:
    """相同业务内容的键顺序变化不得改变摘要和稳定标识。"""
    first = {"source": SOURCE_ID, "location": {"page": 3, "ref": "#/texts/2"}}
    second = {"location": {"ref": "#/texts/2", "page": 3}, "source": SOURCE_ID}

    assert canonical_sha256(first) == canonical_sha256(second)
    assert stable_id("evidence", first) == stable_id("evidence", second)


def test_different_evidence_locations_do_not_collide() -> None:
    """同一文档的不同位置必须生成不同证据标识。"""
    first = stable_id("evidence", SOURCE_ID, {"page": 1, "ref": "#/texts/1"})
    second = stable_id("evidence", SOURCE_ID, {"page": 2, "ref": "#/texts/1"})

    assert first != second


def test_lineage_requires_timezone_aware_created_at() -> None:
    """血缘时间必须带时区以避免跨环境歧义。"""
    with pytest.raises(ValidationError):
        LineageMetadata(
            run_id="run_test",
            producer="test",
            producer_version="0.1.0",
            created_at=datetime(2026, 8, 30, 12, 0),
        )
