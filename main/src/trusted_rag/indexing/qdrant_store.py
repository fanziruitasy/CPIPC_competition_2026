"""实现 Dense 与本地 BM25 双路 Qdrant 不可变快照存储。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from qdrant_client import QdrantClient, models

from trusted_rag.domain.enums import RetrievalProfile
from trusted_rag.domain.knowledge import ChunkRecord
from trusted_rag.domain.ports import (
    IndexRecord,
    SearchHit,
    SearchRequest,
    SnapshotReference,
)


class QdrantVectorStore:
    """通过命名 Dense/BM25 向量和 Alias 实现可切换索引快照。"""

    def __init__(
        self,
        client: QdrantClient,
        *,
        dense_name: str = "dense",
        bm25_name: str = "bm25",
        dense_size: int = 1024,
        rrf_k: int = 60,
    ) -> None:
        """初始化 Qdrant 适配器。

        :param client: 已配置 URL、超时和可选 API Key 的 Qdrant 客户端。
        :param dense_name: Dense 命名向量名称。
        :param bm25_name: BM25 命名稀疏向量名称。
        :param dense_size: Dense 维度，固定为 1024。
        :param rrf_k: 双路检索时的 RRF 常数。
        :return: 无。
        """
        if dense_size != 1024 or rrf_k <= 0:
            raise ValueError("Qdrant Dense 维度必须为 1024，RRF k 必须为正数。")
        self.client = client
        self.dense_name = dense_name
        self.bm25_name = bm25_name
        self.dense_size = dense_size
        self.rrf_k = rrf_k

    def create_snapshot(self, snapshot: SnapshotReference) -> None:
        """创建包含 Dense 与 BM25 的空不可变 Collection。

        :param snapshot: 新快照及其唯一 Collection 名称。
        :return: 无。
        :raises FileExistsError: Collection 已存在时抛出。
        """
        if self.client.collection_exists(snapshot.qdrant_collection):
            raise FileExistsError(f"Qdrant Collection 已存在：{snapshot.qdrant_collection}")
        self.client.create_collection(
            collection_name=snapshot.qdrant_collection,
            vectors_config={
                self.dense_name: models.VectorParams(
                    size=self.dense_size,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                self.bm25_name: models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
        for field_name in (
            "source_id",
            "source_format",
            "original_file_names",
            "content_type",
            "quality_status",
            "requires_manual_review",
            "metrics",
            "entities",
            "periods",
            "units",
        ):
            schema = (
                models.PayloadSchemaType.BOOL
                if field_name == "requires_manual_review"
                else models.PayloadSchemaType.KEYWORD
            )
            self.client.create_payload_index(
                collection_name=snapshot.qdrant_collection,
                field_name=field_name,
                field_schema=schema,
                wait=True,
            )

    def upsert(self, snapshot: SnapshotReference, records: Sequence[IndexRecord]) -> None:
        """把统一索引记录幂等写入指定快照。

        :param snapshot: 目标不可变快照。
        :param records: Dense、BM25、Chunk 与 Payload 记录。
        :return: 无。
        """
        points = [
            models.PointStruct(
                id=_point_id(record.chunk.chunk_id),
                vector={
                    self.dense_name: record.dense_vector.values,
                    self.bm25_name: models.SparseVector(
                        indices=record.bm25_vector.indices,
                        values=record.bm25_vector.values,
                    ),
                },
                payload=_payload(record),
            )
            for record in records
        ]
        if points:
            self.client.upsert(
                collection_name=snapshot.qdrant_collection,
                points=points,
                wait=True,
            )

    def search(self, snapshot: SnapshotReference, request: SearchRequest) -> list[SearchHit]:
        """按检索 Profile 执行 Dense、BM25 或 RRF 双路检索。

        :param snapshot: 要查询的 Collection 快照。
        :param request: 查询向量、过滤条件与 Top-K。
        :return: 携带完整 Chunk 的统一候选。
        """
        query_filter = _query_filter(request)
        if request.profile is RetrievalProfile.DENSE_ONLY:
            assert request.dense_vector is not None
            return self._search_channel(
                snapshot,
                vector=request.dense_vector.values,
                using=self.dense_name,
                query_filter=query_filter,
                limit=request.top_k,
                channel=RetrievalProfile.DENSE_ONLY,
            )
        if request.profile is RetrievalProfile.BM25_ONLY:
            assert request.bm25_vector is not None
            return self._search_channel(
                snapshot,
                vector=models.SparseVector(
                    indices=request.bm25_vector.indices,
                    values=request.bm25_vector.values,
                ),
                using=self.bm25_name,
                query_filter=query_filter,
                limit=request.top_k,
                channel=RetrievalProfile.BM25_ONLY,
            )
        assert request.dense_vector is not None and request.bm25_vector is not None
        dense = self._search_channel(
            snapshot,
            vector=request.dense_vector.values,
            using=self.dense_name,
            query_filter=query_filter,
            limit=request.top_k,
            channel=RetrievalProfile.DENSE_ONLY,
        )
        bm25 = self._search_channel(
            snapshot,
            vector=models.SparseVector(
                indices=request.bm25_vector.indices,
                values=request.bm25_vector.values,
            ),
            using=self.bm25_name,
            query_filter=query_filter,
            limit=request.top_k,
            channel=RetrievalProfile.BM25_ONLY,
        )
        return _rrf_fuse(dense, bm25, limit=request.top_k, rrf_k=self.rrf_k)

    def activate(self, snapshot: SnapshotReference, *, alias: str) -> None:
        """原子删除旧 Alias 指向并创建新指向。

        :param snapshot: 已完成冒烟验证的新快照。
        :param alias: 对外稳定别名。
        :return: 无。
        """
        if not self.client.collection_exists(snapshot.qdrant_collection):
            raise FileNotFoundError(snapshot.qdrant_collection)
        existing = {item.alias_name for item in self.client.get_aliases().aliases}
        operations: list[Any] = []
        if alias in existing:
            operations.append(
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))
            )
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name=snapshot.qdrant_collection,
                    alias_name=alias,
                )
            )
        )
        self.client.update_collection_aliases(change_aliases_operations=operations)

    def _search_channel(
        self,
        snapshot: SnapshotReference,
        *,
        vector: list[float] | models.SparseVector,
        using: str,
        query_filter: models.Filter | None,
        limit: int,
        channel: RetrievalProfile,
    ) -> list[SearchHit]:
        response = self.client.query_points(
            collection_name=snapshot.qdrant_collection,
            query=vector,
            using=using,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        hits: list[SearchHit] = []
        for rank, point in enumerate(response.points, start=1):
            payload = point.payload or {}
            chunk = ChunkRecord.model_validate(payload["chunk"])
            hits.append(
                SearchHit(
                    chunk_id=chunk.chunk_id,
                    score=float(point.score),
                    rank=rank,
                    channel=channel,
                    chunk=chunk,
                )
            )
        return hits


def _payload(record: IndexRecord) -> dict[str, Any]:
    chunk = record.chunk
    payload = {
        "chunk": chunk.model_dump(mode="json"),
        "chunk_id": chunk.chunk_id,
        "source_id": chunk.source_id,
        "document_id": chunk.document_id,
        "content_type": chunk.content_type.value,
        "quality_status": chunk.quality.status.value,
        "requires_manual_review": chunk.quality.requires_manual_review,
        "metrics": chunk.retrieval.indicator_names,
        "entities": chunk.retrieval.organization_names,
        "periods": chunk.retrieval.periods,
        "units": chunk.retrieval.units,
    }
    payload.update(record.payload)
    return payload


def _query_filter(request: SearchRequest) -> models.Filter | None:
    filters = request.filters
    mapping: tuple[tuple[str, Sequence[Any]], ...] = (
        ("source_id", filters.source_ids),
        ("original_file_names", filters.original_file_names),
        ("source_format", [item.value for item in filters.source_formats]),
        ("issuer", filters.issuers),
        ("document_number", filters.document_numbers),
        ("regulatory_topics", filters.regulatory_topics),
        ("business_domains", filters.business_domains),
        ("metrics", filters.metrics),
        ("entities", filters.entities),
        ("periods", filters.periods),
        ("units", filters.units),
    )
    conditions: list[models.Condition] = [
        models.FieldCondition(key=field, match=models.MatchAny(any=list(values)))
        for field, values in mapping
        if values
    ]
    return models.Filter(must=conditions) if conditions else None


def _rrf_fuse(
    dense: Sequence[SearchHit],
    bm25: Sequence[SearchHit],
    *,
    limit: int,
    rrf_k: int,
) -> list[SearchHit]:
    scores: dict[str, float] = {}
    chunks: dict[str, ChunkRecord] = {}
    for hits in (dense, bm25):
        for hit in hits:
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1 / (rrf_k + hit.rank)
            chunks[hit.chunk_id] = hit.chunk
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:limit]
    return [
        SearchHit(
            chunk_id=chunk_id,
            score=scores[chunk_id],
            rank=rank,
            channel=RetrievalProfile.DENSE_BM25,
            chunk=chunks[chunk_id],
        )
        for rank, chunk_id in enumerate(ordered, start=1)
    ]


def _point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"trusted-rag:{chunk_id}"))
