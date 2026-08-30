"""定义在线检索、精排和证据门禁的可审计运行契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from trusted_rag.domain.common import ContractModel, NonEmptyStr, StableId
from trusted_rag.domain.enums import RetrievalProfile
from trusted_rag.domain.knowledge import EvidenceUnit
from trusted_rag.domain.ports import SearchHit


class CandidateAudit(ContractModel):
    """单个候选在召回、融合和精排阶段的排名记录。"""

    chunk_id: StableId
    source_id: StableId
    dense_rank: Annotated[int, Field(ge=1)] | None = None
    dense_score: float | None = None
    bm25_rank: Annotated[int, Field(ge=1)] | None = None
    bm25_score: float | None = None
    fused_rank: Annotated[int, Field(ge=1)] | None = None
    fused_score: float | None = None
    rerank_rank: Annotated[int, Field(ge=1)] | None = None
    rerank_score: float | None = None


class RerankCallAudit(ContractModel):
    """不记录正文和密钥的精排调用审计。"""

    provider: Literal["dashscope"] = "dashscope"
    model: Literal["qwen3-rerank"] = "qwen3-rerank"
    request_sha256: NonEmptyStr
    request_id: str | None = None
    candidate_count: Annotated[int, Field(ge=0)]
    attempt_count: Annotated[int, Field(ge=1)]
    latency_ms: Annotated[int, Field(ge=0)]
    status: Literal["success", "fallback"]
    fallback_reason: str | None = None


class RerankResult(ContractModel):
    """精排后的候选及实际排序来源。"""

    hits: list[SearchHit]
    audit: RerankCallAudit


class RetrievalResult(ContractModel):
    """一次双路检索的候选、证据和完整审计摘要。"""

    profile: RetrievalProfile
    hits: list[SearchHit]
    evidence: list[EvidenceUnit] = Field(default_factory=list)
    candidates: list[CandidateAudit] = Field(default_factory=list)
    dense_candidate_count: Annotated[int, Field(ge=0)] = 0
    bm25_candidate_count: Annotated[int, Field(ge=0)] = 0
    fused_candidate_count: Annotated[int, Field(ge=0)] = 0
    structured_fact_count: Annotated[int, Field(ge=0)] = 0
    rerank: RerankCallAudit | None = None


class EvidenceGateDecision(ContractModel):
    """回答生成前后的确定性证据门禁结果。"""

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
