"""提供领域契约测试使用的完整有效样例。"""

from datetime import UTC, datetime

import pytest

from trusted_rag.domain.common import ArtifactReferences, LineageMetadata, PublicSourceLocation, SourceLocation
from trusted_rag.domain.enums import (
    AnswerStatus,
    ChunkContentType,
    ElementType,
    EvidenceType,
    QueryIntent,
    QueryRoute,
    RetrievalProfile,
    SourceFormat,
    SourceKind,
    SourceProfile,
)
from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import ChunkRecord, DocumentRecord, ElementRecord, EvidenceUnit, SourceDocument
from trusted_rag.domain.query import (
    AnswerRecord,
    EvidenceCitation,
    QueryPlan,
    RetrievalSummary,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
SOURCE_SHA256 = "a" * 64
SOURCE_ID = stable_id("source", "banking-regulations", SOURCE_SHA256)
DOCUMENT_ID = stable_id("document", SOURCE_ID, "native_docx")
ELEMENT_ID = stable_id("element", DOCUMENT_ID, 0, "paragraph")
EVIDENCE_ID = stable_id("evidence", DOCUMENT_ID, ELEMENT_ID, "text")
CHUNK_ID = stable_id("chunk", DOCUMENT_ID, [ELEMENT_ID], canonical_sha256("商业银行应当建立风险管理制度。"))
QUERY_PLAN_ID = stable_id("query_plan", "trace_test", "资本管理要求是什么？")
ANSWER_ID = stable_id("answer", QUERY_PLAN_ID, [EVIDENCE_ID])


def lineage(*input_ids: str) -> LineageMetadata:
    """创建固定时间的测试血缘。

    :param input_ids: 当前对象直接依赖的稳定标识。
    :return: 可重复使用的血缘元数据。
    """
    return LineageMetadata(
        run_id="document-ingestion-v0.01-20260830T120000Z-a1b2c3d4",
        producer="contract-test",
        producer_version="0.1.0",
        input_ids=list(input_ids),
        created_at=NOW,
    )


@pytest.fixture
def source_document() -> SourceDocument:
    """返回一份原生 DOCX 来源记录。"""
    return SourceDocument(
        source_id=SOURCE_ID,
        knowledge_base_id="banking-regulations",
        original_file_name="商业银行风险管理办法.docx",
        normalized_file_name="385.docx",
        source_format=SourceFormat.DOCX,
        source_kind=SourceKind.ORIGINAL,
        source_sha256=SOURCE_SHA256,
        file_size_bytes=1024,
        relative_path="word/native-docx/385.docx",
        lineage=lineage(SOURCE_SHA256),
    )


@pytest.fixture
def document_record() -> DocumentRecord:
    """返回一份完成解析的文档记录。"""
    return DocumentRecord(
        document_id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        knowledge_base_id="banking-regulations",
        title="商业银行风险管理办法",
        source_profile=SourceProfile.NATIVE_DOCX,
        parser_name="docling",
        parser_version="2.120.1",
        parsing_config_version="v0.04",
        artifacts=ArtifactReferences(
            structured_json_uri="parsed/native-docx/385/document.json",
            markdown_uri="parsed/native-docx/385/document.md",
            html_uri="parsed/native-docx/385/document.html",
        ),
        lineage=lineage(SOURCE_ID),
    )


@pytest.fixture
def element_record() -> ElementRecord:
    """返回一条带章节与 Docling 引用的段落元素。"""
    return ElementRecord(
        element_id=ELEMENT_ID,
        document_id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        element_index=0,
        element_type=ElementType.PARAGRAPH,
        display_text="商业银行应当建立风险管理制度。",
        heading_path=["第二章", "风险管理"],
        clause="第十二条",
        location=SourceLocation(
            section_path=["第二章", "风险管理"],
            clause="第十二条",
            docling_ref="#/texts/12",
            element_ref=ELEMENT_ID,
        ),
        lineage=lineage(DOCUMENT_ID),
    )


@pytest.fixture
def evidence_unit() -> EvidenceUnit:
    """返回可定位到 DOCX 条款的原文证据。"""
    return EvidenceUnit(
        evidence_id=EVIDENCE_ID,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        evidence_type=EvidenceType.TEXT,
        excerpt="商业银行应当建立风险管理制度。",
        location=SourceLocation(
            section_path=["第二章", "风险管理"],
            clause="第十二条",
            docling_ref="#/texts/12",
            element_ref=ELEMENT_ID,
        ),
        lineage=lineage(ELEMENT_ID),
    )


@pytest.fixture
def chunk_record() -> ChunkRecord:
    """返回分别配置展示、Dense 与 BM25 文本的完整 Chunk。"""
    text = "商业银行应当建立风险管理制度。"
    retrieval_text = "商业银行风险管理办法\n第二章 > 风险管理\n第十二条\n" + text
    return ChunkRecord(
        chunk_id=CHUNK_ID,
        content_sha256=canonical_sha256(text),
        document_id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        chunk_index=0,
        content_type=ChunkContentType.TEXT,
        display_text=text,
        embedding_text=retrieval_text,
        bm25_text=retrieval_text,
        heading_path=["第二章", "风险管理"],
        clause="第十二条",
        token_count=18,
        source_element_ids=[ELEMENT_ID],
        evidence_ids=[EVIDENCE_ID],
        locations=[
            SourceLocation(
                section_path=["第二章", "风险管理"],
                clause="第十二条",
                docling_ref="#/texts/12",
                element_ref=ELEMENT_ID,
            )
        ],
        lineage=lineage(ELEMENT_ID),
    )


@pytest.fixture
def query_plan() -> QueryPlan:
    """返回规则生成的文档检索计划。"""
    return QueryPlan(
        query_plan_id=QUERY_PLAN_ID,
        trace_id="trace_test",
        knowledge_base_id="banking-regulations",
        original_query="资本管理要求是什么？",
        normalized_query="资本管理要求",
        semantic_queries=["商业银行资本管理要求"],
        route=QueryRoute.DOCUMENT,
        intent=QueryIntent.REGULATION_LOOKUP,
        retrieval_profile=RetrievalProfile.DENSE_BM25,
        planner_mode="rules_only",
        rule_confidence=0.95,
        created_at=NOW,
    )


@pytest.fixture
def answer_record() -> AnswerRecord:
    """返回包含安全公开引用的完整回答记录。"""
    return AnswerRecord(
        answer_id=ANSWER_ID,
        trace_id="trace_test",
        query_plan_id=QUERY_PLAN_ID,
        knowledge_base_id="banking-regulations",
        question="资本管理要求是什么？",
        status=AnswerStatus.ANSWERED,
        answer_text="商业银行应当建立风险管理制度。",
        citations=[
            EvidenceCitation(
                evidence_id=EVIDENCE_ID,
                source_id=SOURCE_ID,
                original_file_name="商业银行风险管理办法.docx",
                source_format=SourceFormat.DOCX,
                excerpt="商业银行应当建立风险管理制度。",
                location=PublicSourceLocation(
                    section_path=["第二章", "风险管理"],
                    clause="第十二条",
                    element_ref=ELEMENT_ID,
                ),
            )
        ],
        retrieval=RetrievalSummary(
            profile=RetrievalProfile.DENSE_BM25,
            dense_candidate_count=40,
            bm25_candidate_count=40,
            fused_candidate_count=32,
            reranked_candidate_count=10,
        ),
        created_at=NOW,
    )

