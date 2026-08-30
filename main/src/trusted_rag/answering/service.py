"""把证据门禁、回答模型和公开引用绑定为可信回答。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from trusted_rag.domain.enums import AnswerStatus
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import EvidenceUnit
from trusted_rag.domain.ports import AnswerGenerator
from trusted_rag.domain.query import AnswerRecord, ModelUsage, QueryPlan, RetrievalSummary
from trusted_rag.retrieval.contracts import RetrievalResult
from trusted_rag.retrieval.evidence_repository import CorpusEvidenceRepository

from .gates import post_generation_gate, pre_generation_gate


class TrustedAnswerService:
    """仅允许通过确定性门禁的模型草稿形成最终答案。"""

    def __init__(self, generator: AnswerGenerator, evidence_repository: CorpusEvidenceRepository) -> None:
        """初始化可信回答服务。

        :param generator: 受控回答模型端口。
        :param evidence_repository: 生成公开引用的语料仓储。
        :return: 无。
        """
        self.generator = generator
        self.evidence_repository = evidence_repository

    def answer(
        self,
        plan: QueryPlan,
        retrieval: RetrievalResult,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        answer_question: str | None = None,
    ) -> AnswerRecord:
        """执行前后门禁并返回回答、引用或可信拒答。

        :param plan: 当前查询计划。
        :param retrieval: 文档与结构化证据检索结果。
        :param user_id: 可选前端用户标识。
        :param session_id: 可选会话标识。
        :param answer_question: 仅供回答模型使用的完整题面；检索与门禁仍使用原问题。
        :return: 符合 answer.v1 契约的最终记录。
        """
        summary = _summary(retrieval)
        if plan.clarification_required:
            return _declined(
                plan,
                summary,
                AnswerStatus.CLARIFICATION_REQUIRED,
                "请补充具体文件、机构、指标、期间或统计口径。",
                user_id,
                session_id,
            )
        preflight = pre_generation_gate(plan.original_query, retrieval.evidence)
        if not preflight.allowed:
            return _declined(
                plan,
                summary,
                AnswerStatus.REFUSED,
                "；".join(preflight.reasons),
                user_id,
                session_id,
            )
        draft = self.generator.generate(answer_question or plan.original_query, retrieval.evidence)
        postflight = post_generation_gate(
            draft,
            retrieval.evidence,
            question=plan.original_query,
        )
        if not postflight.allowed:
            return _declined(
                plan,
                summary,
                AnswerStatus.REFUSED,
                "；".join(postflight.reasons),
                user_id,
                session_id,
                model_name=draft.model_name,
                input_tokens=draft.input_tokens,
                output_tokens=draft.output_tokens,
                latency_ms=draft.latency_ms,
                request_id=draft.request_id,
            )
        selected = _selected_evidence(retrieval.evidence, draft.cited_evidence_ids)
        return AnswerRecord(
            answer_id=stable_id("answer", plan.query_plan_id, draft.answer_text),
            trace_id=plan.trace_id,
            query_plan_id=plan.query_plan_id,
            knowledge_base_id=plan.knowledge_base_id,
            user_id=user_id,
            session_id=session_id,
            question=plan.original_query,
            status=AnswerStatus.ANSWERED,
            answer_text=draft.answer_text,
            citations=[self.evidence_repository.citation(item) for item in selected],
            retrieval=summary,
            usage=ModelUsage(
                provider="dashscope",
                model_name=draft.model_name,
                request_id=draft.request_id,
                input_tokens=draft.input_tokens,
                output_tokens=draft.output_tokens,
                latency_ms=draft.latency_ms,
            ),
            created_at=datetime.now(UTC),
        )


def _selected_evidence(evidence: Sequence[EvidenceUnit], ids: Sequence[str]) -> list[EvidenceUnit]:
    by_id = {item.evidence_id: item for item in evidence}
    return [by_id[item] for item in dict.fromkeys(ids)]


def _summary(retrieval: RetrievalResult) -> RetrievalSummary:
    return RetrievalSummary(
        profile=retrieval.profile,
        dense_candidate_count=retrieval.dense_candidate_count,
        bm25_candidate_count=retrieval.bm25_candidate_count,
        fused_candidate_count=retrieval.fused_candidate_count,
        reranked_candidate_count=len(retrieval.hits) if retrieval.rerank else 0,
        structured_fact_count=retrieval.structured_fact_count,
    )


def _declined(
    plan: QueryPlan,
    summary: RetrievalSummary,
    status: AnswerStatus,
    reason: str,
    user_id: str | None,
    session_id: str | None,
    *,
    model_name: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    request_id: str | None = None,
) -> AnswerRecord:
    return AnswerRecord(
        answer_id=stable_id("answer", plan.query_plan_id, status.value, reason),
        trace_id=plan.trace_id,
        query_plan_id=plan.query_plan_id,
        knowledge_base_id=plan.knowledge_base_id,
        user_id=user_id,
        session_id=session_id,
        question=plan.original_query,
        status=status,
        answer_text="",
        refusal_reason=reason,
        retrieval=summary,
        usage=ModelUsage(
            provider="dashscope" if model_name else None,
            model_name=model_name,
            request_id=request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        ),
        created_at=datetime.now(UTC),
    )
