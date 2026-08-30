"""编排 Dense、Jieba BM25、RRF、精排和结构化事实证据。"""

from __future__ import annotations

from collections.abc import Sequence

from trusted_rag.domain.enums import QueryRoute, RetrievalProfile
from trusted_rag.domain.knowledge import EvidenceUnit
from trusted_rag.domain.ports import (
    DenseEmbedder,
    FactStore,
    SearchHit,
    SearchRequest,
    SnapshotReference,
    VectorStore,
)
from trusted_rag.domain.query import QueryFilters, QueryPlan
from trusted_rag.indexing.bm25 import JiebaBm25Indexer
from trusted_rag.retrieval.contracts import CandidateAudit, RetrievalResult
from trusted_rag.retrieval.evidence_repository import CorpusEvidenceRepository
from trusted_rag.retrieval.reranker import DashScopeReranker


class RetrievalService:
    """执行 QueryPlan 指定的文档召回、精排和事实查询。"""

    def __init__(
        self,
        *,
        vector_store: VectorStore,
        fact_store: FactStore,
        embedder: DenseEmbedder,
        bm25: JiebaBm25Indexer,
        evidence_repository: CorpusEvidenceRepository,
        reranker: DashScopeReranker | None = None,
        recall_top_k: int = 40,
        rerank_top_k: int = 8,
        rrf_k: int = 60,
    ) -> None:
        """初始化检索服务。

        :param vector_store: Qdrant 存储端口。
        :param fact_store: DuckDB 事实存储端口。
        :param embedder: DashScope Dense 查询向量客户端。
        :param bm25: 已恢复建库词表的 Jieba BM25 索引器。
        :param evidence_repository: 统一语料证据仓储。
        :param reranker: 可选 qwen3-rerank 客户端。
        :param recall_top_k: 每路召回候选数。
        :param rerank_top_k: 最终精排候选数。
        :param rrf_k: RRF 融合常数。
        :return: 无。
        """
        if min(recall_top_k, rerank_top_k, rrf_k) <= 0:
            raise ValueError("检索数量和 RRF k 必须为正数。")
        self.vector_store = vector_store
        self.fact_store = fact_store
        self.embedder = embedder
        self.bm25 = bm25
        self.evidence_repository = evidence_repository
        self.reranker = reranker
        self.recall_top_k = recall_top_k
        self.rerank_top_k = rerank_top_k
        self.rrf_k = rrf_k

    def retrieve(self, snapshot: SnapshotReference, plan: QueryPlan) -> RetrievalResult:
        """执行查询计划并合并文档与结构化证据。

        :param snapshot: 当前一致的 Qdrant/DuckDB 快照。
        :param plan: 已通过白名单校验的查询计划。
        :return: 最终候选、原子证据和审计摘要。
        """
        dense: list[SearchHit] = []
        bm25: list[SearchHit] = []
        hits: list[SearchHit] = []
        candidate_audit: list[CandidateAudit] = []
        if plan.route in {QueryRoute.DOCUMENT, QueryRoute.MIXED}:
            queries = plan.semantic_queries
            document_filters = _document_filters(plan)
            if plan.retrieval_profile in {RetrievalProfile.DENSE_ONLY, RetrievalProfile.DENSE_BM25}:
                dense = _merge_query_hits(
                    [
                        self.vector_store.search(
                            snapshot,
                            SearchRequest(
                                query=query,
                                profile=RetrievalProfile.DENSE_ONLY,
                                dense_vector=vector,
                                filters=document_filters,
                                top_k=self.recall_top_k,
                            ),
                        )
                        for query, vector in zip(
                            queries,
                            self.embedder.embed(queries),
                            strict=True,
                        )
                    ],
                    limit=self.recall_top_k,
                    rrf_k=self.rrf_k,
                )
            if plan.retrieval_profile in {RetrievalProfile.BM25_ONLY, RetrievalProfile.DENSE_BM25}:
                bm25 = _merge_query_hits(
                    [
                        self.vector_store.search(
                            snapshot,
                            SearchRequest(
                                query=query,
                                profile=RetrievalProfile.BM25_ONLY,
                                bm25_vector=self.bm25.encode_query(query),
                                filters=document_filters,
                                top_k=self.recall_top_k,
                            ),
                        )
                        for query in queries
                    ],
                    limit=self.recall_top_k,
                    rrf_k=self.rrf_k,
                )
            hits, candidate_audit = _combine_candidates(
                dense,
                bm25,
                profile=plan.retrieval_profile,
                limit=self.recall_top_k,
                rrf_k=self.rrf_k,
            )
        rerank_audit = None
        if hits and self.reranker is not None:
            reranked = self.reranker.rerank_with_audit(
                plan.normalized_query,
                hits,
                top_k=self.rerank_top_k,
            )
            hits = reranked.hits
            rerank_audit = reranked.audit
            ranks = {hit.chunk_id: hit for hit in hits}
            candidate_audit = [
                audit.model_copy(
                    update={
                        "rerank_rank": ranks[audit.chunk_id].rank,
                        "rerank_score": ranks[audit.chunk_id].score,
                    }
                )
                if audit.chunk_id in ranks
                else audit
                for audit in candidate_audit
            ]
        document_evidence = self.evidence_repository.from_hits(hits)
        structured_evidence: list[EvidenceUnit] = []
        if plan.route in {QueryRoute.STRUCTURED, QueryRoute.MIXED}:
            structured_evidence = self.fact_store.query(snapshot, plan)
        evidence = _deduplicate_evidence([*structured_evidence, *document_evidence])
        return RetrievalResult(
            profile=plan.retrieval_profile,
            hits=hits,
            evidence=evidence,
            candidates=candidate_audit,
            dense_candidate_count=len(dense),
            bm25_candidate_count=len(bm25),
            fused_candidate_count=len(hits),
            structured_fact_count=len(structured_evidence),
            rerank=rerank_audit,
        )


def _combine_candidates(
    dense: Sequence[SearchHit],
    bm25: Sequence[SearchHit],
    *,
    profile: RetrievalProfile,
    limit: int,
    rrf_k: int,
) -> tuple[list[SearchHit], list[CandidateAudit]]:
    by_id = {hit.chunk_id: hit for hit in [*dense, *bm25]}
    dense_by_id = {hit.chunk_id: hit for hit in dense}
    bm25_by_id = {hit.chunk_id: hit for hit in bm25}
    if profile is RetrievalProfile.DENSE_ONLY:
        ordered_ids = [hit.chunk_id for hit in dense[:limit]]
        scores = {hit.chunk_id: hit.score for hit in dense}
    elif profile is RetrievalProfile.BM25_ONLY:
        ordered_ids = [hit.chunk_id for hit in bm25[:limit]]
        scores = {hit.chunk_id: hit.score for hit in bm25}
    else:
        scores = {
            chunk_id: (1 / (rrf_k + dense_by_id[chunk_id].rank) if chunk_id in dense_by_id else 0)
            + (1 / (rrf_k + bm25_by_id[chunk_id].rank) if chunk_id in bm25_by_id else 0)
            for chunk_id in by_id
        }
        ordered_ids = sorted(scores, key=lambda item: (-scores[item], item))[:limit]
    hits = [
        SearchHit(
            chunk_id=chunk_id,
            score=scores[chunk_id],
            rank=rank,
            channel=profile,
            chunk=by_id[chunk_id].chunk,
        )
        for rank, chunk_id in enumerate(ordered_ids, start=1)
    ]
    audits = [
        CandidateAudit(
            chunk_id=chunk_id,
            source_id=by_id[chunk_id].chunk.source_id,
            dense_rank=dense_by_id[chunk_id].rank if chunk_id in dense_by_id else None,
            dense_score=dense_by_id[chunk_id].score if chunk_id in dense_by_id else None,
            bm25_rank=bm25_by_id[chunk_id].rank if chunk_id in bm25_by_id else None,
            bm25_score=bm25_by_id[chunk_id].score if chunk_id in bm25_by_id else None,
            fused_rank=rank,
            fused_score=scores[chunk_id],
        )
        for rank, chunk_id in enumerate(ordered_ids, start=1)
    ]
    return hits, audits


def _merge_query_hits(
    hit_lists: Sequence[Sequence[SearchHit]],
    *,
    limit: int,
    rrf_k: int,
) -> list[SearchHit]:
    """按怡佳选择题检索方式融合多个选项查询的同路候选。

    :param hit_lists: 同一 Dense 或 BM25 通道的多次查询结果。
    :param limit: 融合后保留的唯一候选数。
    :param rrf_k: 多查询 RRF 常数。
    :return: 以 RRF 分数重新排序且去重的候选。
    """
    if len(hit_lists) == 1:
        return list(hit_lists[0][:limit])
    by_id: dict[str, SearchHit] = {}
    scores: dict[str, float] = {}
    for hits in hit_lists:
        for hit in hits:
            by_id.setdefault(hit.chunk_id, hit)
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1 / (rrf_k + hit.rank)
    ordered_ids = sorted(scores, key=lambda item: (-scores[item], item))[:limit]
    return [
        SearchHit(
            chunk_id=chunk_id,
            score=scores[chunk_id],
            rank=rank,
            channel=by_id[chunk_id].channel,
            chunk=by_id[chunk_id].chunk,
        )
        for rank, chunk_id in enumerate(ordered_ids, start=1)
    ]


def _deduplicate_evidence(evidence: Sequence[EvidenceUnit]) -> list[EvidenceUnit]:
    result: list[EvidenceUnit] = []
    seen: set[str] = set()
    for item in evidence:
        if item.evidence_id not in seen:
            result.append(item)
            seen.add(item.evidence_id)
    return result


def _document_filters(plan: QueryPlan) -> QueryFilters:
    """仅保留可精确验证的来源字段，避免词典命中误排除法规文档。

    :param plan: 当前查询计划。
    :return: 文档召回可以安全执行的元数据过滤条件。
    """
    return QueryFilters(
        source_ids=plan.filters.source_ids,
        original_file_names=plan.filters.original_file_names,
        source_formats=plan.filters.source_formats,
    )
