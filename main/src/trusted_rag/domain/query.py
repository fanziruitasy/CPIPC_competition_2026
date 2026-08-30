"""定义查询计划、公开引用和可信回答契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from trusted_rag.domain.common import ContractModel, NonEmptyStr, PublicSourceLocation, StableId
from trusted_rag.domain.enums import (
    AnswerStatus,
    QueryIntent,
    QueryRoute,
    RetrievalProfile,
    SourceFormat,
    StructuredOperationType,
)


class QueryFilters(ContractModel):
    """与 Qdrant Payload 和 DuckDB 白名单字段对齐的过滤条件。"""

    source_ids: list[StableId] = Field(default_factory=list)
    original_file_names: list[str] = Field(default_factory=list)
    source_formats: list[SourceFormat] = Field(default_factory=list)
    issuers: list[str] = Field(default_factory=list)
    document_numbers: list[str] = Field(default_factory=list)
    regulatory_topics: list[str] = Field(default_factory=list)
    business_domains: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)


class StructuredOperation(ContractModel):
    """允许事实库确定性执行的单个结构化操作。"""

    operation: StructuredOperationType
    metric: str | None = None
    entity: str | None = None
    periods: list[str] = Field(default_factory=list)
    unit: str | None = None
    group_by: list[Literal["metric", "entity", "period", "unit"]] = Field(default_factory=list)


class QueryPlan(ContractModel):
    """规则或受控模型生成并经过模式校验的完整查询计划。"""

    schema_version: Literal["query_plan.v1"] = "query_plan.v1"
    query_plan_id: StableId
    trace_id: NonEmptyStr
    knowledge_base_id: NonEmptyStr
    original_query: NonEmptyStr
    normalized_query: NonEmptyStr
    semantic_queries: list[NonEmptyStr] = Field(min_length=1)
    route: QueryRoute
    intent: QueryIntent
    retrieval_profile: RetrievalProfile = RetrievalProfile.DENSE_BM25
    filters: QueryFilters = Field(default_factory=QueryFilters)
    structured_operations: list[StructuredOperation] = Field(default_factory=list)
    planner_mode: Literal["rules_only", "rules_with_model"]
    planner_reasons: list[str] = Field(default_factory=list)
    rule_confidence: Annotated[float, Field(ge=0, le=1)]
    clarification_required: bool = False
    rejection_reason: str | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_route_requirements(self) -> QueryPlan:
        """校验结构化路由和拒答路由的必要字段。

        :return: 通过路由约束的当前计划。
        :raises ValueError: 路由与操作或拒答原因不一致时抛出。
        """
        if self.route in {QueryRoute.STRUCTURED, QueryRoute.MIXED} and not self.structured_operations:
            raise ValueError("结构化或混合路由必须包含受控结构化操作。")
        if self.route is QueryRoute.REJECT and not self.rejection_reason:
            raise ValueError("拒答路由必须提供 rejection_reason。")
        if self.route is not QueryRoute.REJECT and self.rejection_reason is not None:
            raise ValueError("非拒答路由不得提供 rejection_reason。")
        return self


class EvidenceCitation(ContractModel):
    """可安全返回前端的证据引用，不包含内部绝对路径。"""

    evidence_id: StableId
    source_id: StableId
    original_file_name: NonEmptyStr
    source_format: SourceFormat
    excerpt: NonEmptyStr
    location: PublicSourceLocation


class RetrievalSummary(ContractModel):
    """一次问答实际执行的检索与事实查询摘要。"""

    profile: RetrievalProfile
    dense_candidate_count: Annotated[int, Field(ge=0)] = 0
    bm25_candidate_count: Annotated[int, Field(ge=0)] = 0
    fused_candidate_count: Annotated[int, Field(ge=0)] = 0
    reranked_candidate_count: Annotated[int, Field(ge=0)] = 0
    structured_fact_count: Annotated[int, Field(ge=0)] = 0


class ModelUsage(ContractModel):
    """一次回答的模型、Token、费用和延迟信息。"""

    provider: str | None = None
    model_name: str | None = None
    request_id: str | None = None
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    estimated_cost_cny: Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")] | None = None
    latency_ms: Annotated[int, Field(ge=0)] = 0


class AnswerRecord(ContractModel):
    """包含回答、引用、拒答和审计摘要的最终问答记录。"""

    schema_version: Literal["answer.v1"] = "answer.v1"
    answer_id: StableId
    trace_id: NonEmptyStr
    query_plan_id: StableId
    knowledge_base_id: NonEmptyStr
    user_id: str | None = None
    session_id: str | None = None
    question: NonEmptyStr
    status: AnswerStatus
    answer_text: str
    citations: list[EvidenceCitation] = Field(default_factory=list)
    refusal_reason: str | None = None
    retrieval: RetrievalSummary
    usage: ModelUsage = Field(default_factory=ModelUsage)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_answer_state(self) -> AnswerRecord:
        """校验成功回答、拒答和澄清状态的必要字段。

        :return: 通过状态约束的当前回答。
        :raises ValueError: 回答状态与文本、引用或拒答原因不一致时抛出。
        """
        if self.status is AnswerStatus.ANSWERED:
            if not self.answer_text:
                raise ValueError("成功回答必须包含 answer_text。")
            if not self.citations:
                raise ValueError("成功回答必须包含至少一条来源引用。")
            if self.refusal_reason is not None:
                raise ValueError("成功回答不得包含 refusal_reason。")
        elif self.status in {AnswerStatus.REFUSED, AnswerStatus.CLARIFICATION_REQUIRED}:
            if not self.refusal_reason:
                raise ValueError("拒答或澄清状态必须提供 refusal_reason。")
        return self
