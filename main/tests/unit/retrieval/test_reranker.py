"""验证 qwen3-rerank 成功排序和显式降级。"""

from datetime import UTC, datetime
from types import SimpleNamespace

from trusted_rag.domain.common import LineageMetadata, SourceLocation
from trusted_rag.domain.enums import ChunkContentType, RetrievalProfile
from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import ChunkRecord
from trusted_rag.domain.ports import SearchHit
from trusted_rag.retrieval.reranker import DashScopeReranker


def test_rerank_success_uses_model_scores() -> None:
    """模型分数应覆盖 RRF 顺序并记录成功来源。"""
    hits = [_hit("第一条", 1), _hit("第二条", 2)]
    response = SimpleNamespace(
        status_code=200,
        request_id="rerank-request",
        output={
            "results": [
                {"index": 0, "relevance_score": 0.2},
                {"index": 1, "relevance_score": 0.9},
            ]
        },
    )
    client = DashScopeReranker(api_key="test", max_retries=0, request_callable=lambda **_: response)

    result = client.rerank_with_audit("哪条相关？", hits, top_k=2)

    assert result.hits[0].chunk.display_text == "第二条"
    assert result.hits[0].channel == "qwen3-rerank"
    assert result.audit.status == "success"


def test_rerank_failure_keeps_input_order() -> None:
    """接口失败时必须保留融合顺序并公开降级状态。"""
    hits = [_hit("第一条", 1), _hit("第二条", 2)]

    def fail(**_: object) -> object:
        raise TimeoutError("timeout")

    client = DashScopeReranker(api_key="test", max_retries=0, request_callable=fail)
    result = client.rerank_with_audit("哪条相关？", hits, top_k=2)

    assert [hit.chunk.display_text for hit in result.hits] == ["第一条", "第二条"]
    assert result.audit.status == "fallback"
    assert result.audit.fallback_reason == "TimeoutError"


def _hit(text: str, rank: int) -> SearchHit:
    source_id = stable_id("source", text)
    document_id = stable_id("document", source_id)
    element_id = stable_id("element", document_id)
    evidence_id = stable_id("evidence", element_id)
    chunk = ChunkRecord(
        chunk_id=stable_id("chunk", document_id),
        content_sha256=canonical_sha256(text),
        document_id=document_id,
        source_id=source_id,
        chunk_index=0,
        content_type=ChunkContentType.TEXT,
        display_text=text,
        embedding_text=text,
        bm25_text=text,
        token_count=2,
        source_element_ids=[element_id],
        evidence_ids=[evidence_id],
        locations=[SourceLocation(section_path=["测试"])],
        lineage=LineageMetadata(
            run_id="test",
            producer="test",
            producer_version="1",
            created_at=datetime(2026, 8, 30, tzinfo=UTC),
        ),
    )
    return SearchHit(
        chunk_id=chunk.chunk_id,
        score=1 / rank,
        rank=rank,
        channel=RetrievalProfile.DENSE_BM25,
        chunk=chunk,
    )

