"""验证 Qdrant Dense/BM25 快照、过滤、融合和 Alias 切换。"""

from __future__ import annotations

from datetime import UTC, datetime

from qdrant_client import QdrantClient

from trusted_rag.domain.common import LineageMetadata, SourceLocation
from trusted_rag.domain.enums import ChunkContentType, RetrievalProfile
from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import ChunkRecord, RetrievalMetadata
from trusted_rag.domain.ports import (
    DenseVector,
    IndexRecord,
    SearchRequest,
    SnapshotReference,
    SparseVector,
)
from trusted_rag.domain.query import QueryFilters
from trusted_rag.indexing.qdrant_store import QdrantVectorStore


def test_qdrant_snapshot_dense_bm25_filter_and_alias() -> None:
    """双路向量、Payload 过滤和 Alias 应在同一快照中可用。"""
    client = QdrantClient(":memory:")
    store = QdrantVectorStore(client)
    snapshot = SnapshotReference(
        knowledge_base_id="kb_test",
        snapshot_id="snapshot_test_001",
        qdrant_collection="trusted_rag_test_001",
        duckdb_uri="snapshots/test.duckdb",
    )
    first = _chunk("资本充足率要求", 1)
    second = _chunk("流动性覆盖率要求", 2)
    store.create_snapshot(snapshot)
    store.upsert(
        snapshot,
        [
            _index_record(first, dense_value=1.0, sparse_index=1, source_format="pdf"),
            _index_record(second, dense_value=0.0, sparse_index=2, source_format="docx"),
        ],
    )

    dense_hits = store.search(
        snapshot,
        SearchRequest(
            query="资本要求",
            profile=RetrievalProfile.DENSE_ONLY,
            dense_vector=DenseVector(values=[1.0] + [0.0] * 1023),
            filters=QueryFilters(source_formats=["pdf"]),
            top_k=5,
        ),
    )
    hybrid_hits = store.search(
        snapshot,
        SearchRequest(
            query="资本要求",
            profile=RetrievalProfile.DENSE_BM25,
            dense_vector=DenseVector(values=[1.0] + [0.0] * 1023),
            bm25_vector=SparseVector(indices=[1], values=[1.0]),
            top_k=5,
        ),
    )
    store.activate(snapshot, alias="trusted_rag_current")

    assert [hit.chunk_id for hit in dense_hits] == [first.chunk_id]
    assert hybrid_hits[0].chunk_id == first.chunk_id
    aliases = {item.alias_name: item.collection_name for item in client.get_aliases().aliases}
    assert aliases["trusted_rag_current"] == snapshot.qdrant_collection


def _chunk(text: str, index: int) -> ChunkRecord:
    source_id = stable_id("source", "kb_test", index)
    document_id = stable_id("document", source_id)
    element_id = stable_id("element", document_id, 0)
    evidence_id = stable_id("evidence", element_id)
    return ChunkRecord(
        chunk_id=stable_id("chunk", document_id, index),
        content_sha256=canonical_sha256(text),
        document_id=document_id,
        source_id=source_id,
        chunk_index=0,
        content_type=ChunkContentType.TEXT,
        display_text=text,
        embedding_text=text,
        bm25_text=text,
        token_count=8,
        source_element_ids=[element_id],
        evidence_ids=[evidence_id],
        locations=[SourceLocation(section_path=["测试章节"])],
        retrieval=RetrievalMetadata(indicator_names=[text.removesuffix("要求")]),
        lineage=LineageMetadata(
            run_id="test-run",
            producer="test",
            producer_version="1",
            created_at=datetime(2026, 8, 30, tzinfo=UTC),
        ),
    )


def _index_record(
    chunk: ChunkRecord,
    *,
    dense_value: float,
    sparse_index: int,
    source_format: str,
) -> IndexRecord:
    dense = [0.0] * 1024
    dense[0] = dense_value
    return IndexRecord(
        chunk=chunk,
        dense_vector=DenseVector(values=dense),
        bm25_vector=SparseVector(indices=[sparse_index], values=[1.0]),
        payload={
            "source_format": source_format,
            "original_file_names": [f"来源.{source_format}"],
        },
    )
