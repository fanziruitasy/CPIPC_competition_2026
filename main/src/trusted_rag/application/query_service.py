"""编排查询规划、检索、事实查询、回答和 trace_id 审计。"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from trusted_rag.answering.service import TrustedAnswerService
from trusted_rag.domain.enums import QueryRoute
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.ports import AuditRecord, AuditRepository, QueryPlanner, SnapshotReference
from trusted_rag.domain.query import AnswerRecord, QueryPlan
from trusted_rag.retrieval.service import RetrievalService


class TrustedRagQueryService:
    """提供前端 API 与评测共用的单次可信问答用例。"""

    def __init__(
        self,
        *,
        planner: QueryPlanner,
        retrieval: RetrievalService,
        answering: TrustedAnswerService,
        audit: AuditRepository,
        snapshot: SnapshotReference,
    ) -> None:
        """初始化在线问答用例。

        :param planner: 实现 QueryPlanner 端口的规则优先规划器。
        :param retrieval: 双路检索和事实查询服务。
        :param answering: 可信回答服务。
        :param audit: 追加式审计仓储。
        :param snapshot: 当前一致知识库快照。
        :return: 无。
        """
        self.planner = planner
        self.retrieval = retrieval
        self.answering = answering
        self.audit = audit
        self.snapshot = snapshot

    def ask(
        self,
        question: str,
        *,
        trace_id: str,
        user_id: str | None = None,
        session_id: str | None = None,
        source_ids: list[str] | None = None,
        answer_question: str | None = None,
    ) -> AnswerRecord:
        """执行完整可信问答并写入三阶段审计。

        :param question: 用户问题。
        :param trace_id: 前端和日志共用的追踪标识。
        :param user_id: 可选用户标识。
        :param session_id: 可选会话标识。
        :param source_ids: 用户问题已经明确限定的可选来源范围。
        :param answer_question: 仅供回答阶段使用的可选完整题面，不改变检索查询。
        :return: 包含引用或拒答原因的 answer.v1。
        """
        planning_question = answer_question or question
        plan = self.planner.plan(
            planning_question,
            knowledge_base_id=self.snapshot.knowledge_base_id,
            trace_id=trace_id,
        )
        if answer_question:
            # 选择题选项参与规划和语义检索。公开问题与可信门禁仍使用原始题干。
            plan = plan.model_copy(
                update={
                    "original_query": question,
                    "semantic_queries": _multiple_choice_queries(
                        question,
                        answer_question,
                    )
                    or plan.semantic_queries,
                }
            )
        if source_ids:
            plan = _scope_plan(plan, source_ids)
        self._audit("query_plan", trace_id, plan.model_dump(mode="json"))
        retrieval = self.retrieval.retrieve(self.snapshot, plan)
        self._audit("retrieval", trace_id, retrieval.model_dump(mode="json"))
        answer = self.answering.answer(
            plan,
            retrieval,
            user_id=user_id,
            session_id=session_id,
            answer_question=answer_question,
        )
        self._audit("answer", trace_id, answer.model_dump(mode="json"))
        return answer

    def _audit(self, event_type: str, trace_id: str, payload: dict[str, object]) -> None:
        occurred_at = datetime.now(UTC)
        self.audit.append(
            AuditRecord(
                audit_id=stable_id("audit", trace_id, event_type, occurred_at.isoformat()),
                trace_id=trace_id,
                event_type=event_type,
                payload=payload,
                occurred_at=occurred_at,
            )
        )


def _scope_plan(plan: QueryPlan, source_ids: list[str]) -> QueryPlan:
    """把用户明确指定的文件范围应用到文档与事实检索。

    :param plan: 规则或模型生成的原始计划。
    :param source_ids: 已由知识库来源别名解析的稳定标识。
    :return: 保留原问题和指标/期间、限定来源的计划。
    """
    filter_updates: dict[str, object] = {"source_ids": sorted(set(source_ids))}
    operations = plan.structured_operations
    if plan.route in {QueryRoute.STRUCTURED, QueryRoute.MIXED}:
        # 文件标题中的机构词不等于表格行实体。来源已明确时读取该文件内候选事实。
        filter_updates["entities"] = []
        operations = [operation.model_copy(update={"entity": None}) for operation in operations]
    return plan.model_copy(
        update={
            "filters": plan.filters.model_copy(update=filter_updates),
            "structured_operations": operations,
        }
    )


_OPTION_LINE = re.compile(r"(?m)^([A-D])[.．、]\s*(.+?)\s*$")


def _multiple_choice_queries(question: str, complete_question: str) -> list[str]:
    """按怡佳选择题检索思路构造四个选项级查询。

    :param question: 不含选项的原始题干。
    :param complete_question: 含 A 至 D 四个选项的完整题面。
    :return: 选项完整时返回四个“题干+单项”查询，否则返回空列表。
    """
    options = {label: text.strip() for label, text in _OPTION_LINE.findall(complete_question)}
    if set(options) != {"A", "B", "C", "D"}:
        return []
    return [f"{question}\n{label}. {options[label]}" for label in ("A", "B", "C", "D")]
