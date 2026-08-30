"""实现规则优先、低置信度才调用大模型的受控查询规划。"""

from __future__ import annotations

import calendar
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ConfigDict, Field

from trusted_rag.domain.common import ContractModel
from trusted_rag.domain.enums import QueryIntent, QueryRoute, RetrievalProfile, StructuredOperationType
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.query import QueryFilters, QueryPlan, StructuredOperation

_DOCUMENT_NUMBER = re.compile(r"(?:银发|银保监发|银保监规|金规|金办发|证监会令|国务院令)[〔\[【]?(\d{4})[〕\]】]?\d+号")
_YEAR = re.compile(r"(?<!\d)(20\d{2})年")
_FULL_DATE = re.compile(r"(?<!\d)(20\d{2})年(\d{1,2})月(\d{1,2})日")
_MONTH = re.compile(r"(?<!\d)(20\d{2})年(\d{1,2})月(?!\d*日)")
_QUARTER = re.compile(r"(?<!\d)(20\d{2})年(?:第)?([一二三四1234])季度")
_UNIT = re.compile(
    r"亿元|万元|元|万户|万件|户|家|个|笔|(?<![个每])人(?!身|员|民|寿|均)|%|％|百分点"
)
_CLAUSE = re.compile(r"第[一二三四五六七八九十百千万零〇两\d]+条")
_STATISTIC = re.compile(r"多少|数值|金额|余额|比例|比率|数量|户数|家数|增长率|下降率|合计|平均")
_COMPARISON = re.compile(r"对比|比较|区别|差异|变化|相较|同比|环比|分别")
_REGULATION = re.compile(r"法规|制度|办法|规定|通知|意见|指引|准则|条例|条款|应当|不得|必须")
_AMBIGUOUS = re.compile(r"这个|那个|上述|前者|后者|它们|相关情况")


class PlannerModel(Protocol):
    """受控模型规划器只返回白名单字段补丁。"""

    def __call__(self, question: str, rule_plan: QueryPlan) -> dict[str, Any]:
        """生成规划补丁。

        :param question: 用户原始问题。
        :param rule_plan: 规则规划结果。
        :return: 仅包含允许字段的 JSON 对象。
        """
        ...


class ModelPlanPatch(ContractModel):
    """模型允许修改的最小规划字段，未知字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    route: QueryRoute
    intent: QueryIntent
    semantic_queries: list[str] = Field(min_length=1, max_length=4)
    filters: QueryFilters = Field(default_factory=QueryFilters)
    structured_operations: list[StructuredOperation] = Field(default_factory=list, max_length=4)
    clarification_required: bool = False
    rejection_reason: str | None = None
    reason: str = Field(min_length=1, max_length=500)


class ControlledQueryPlanner:
    """结合确定性规则与可选模型补丁生成可审计 QueryPlan。"""

    def __init__(
        self,
        *,
        metrics: Sequence[str] = (),
        entities: Sequence[str] = (),
        model: PlannerModel | None = None,
        model_threshold: float = 0.72,
        retrieval_profile: RetrievalProfile = RetrievalProfile.DENSE_BM25,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化查询规划器。

        :param metrics: 已审核指标名称词典。
        :param entities: 已审核机构名称词典。
        :param model: 低置信度时使用的受控模型客户端。
        :param model_threshold: 低于该置信度才允许调用模型。
        :param retrieval_profile: 本次检索开关配置。
        :param clock: 测试可注入的 UTC 时钟。
        :return: 无。
        """
        if not 0 <= model_threshold <= 1:
            raise ValueError("model_threshold 必须在 0 到 1 之间。")
        self.metrics = tuple(sorted(set(metrics), key=lambda item: (-len(item), item)))
        self.entities = tuple(sorted(set(entities), key=lambda item: (-len(item), item)))
        self.model = model
        self.model_threshold = model_threshold
        self.retrieval_profile = retrieval_profile
        self.clock = clock or (lambda: datetime.now(UTC))

    def plan(self, question: str, *, knowledge_base_id: str, trace_id: str) -> QueryPlan:
        """先执行规则规划，必要且可用时再应用受模式约束的模型补丁。

        :param question: 用户原始问题。
        :param knowledge_base_id: 查询目标知识库。
        :param trace_id: 当前请求追踪标识。
        :return: 已通过 QueryPlan 契约校验的查询计划。
        """
        normalized = _normalize(question)
        if not normalized:
            raise ValueError("问题不能为空。")
        filters = _extract_filters(normalized, self.metrics, self.entities)
        route, intent, operations, confidence, reasons = _classify(normalized, filters)
        now = self.clock()
        base = QueryPlan(
            query_plan_id=stable_id("query_plan", knowledge_base_id, trace_id, normalized),
            trace_id=trace_id,
            knowledge_base_id=knowledge_base_id,
            original_query=question,
            normalized_query=normalized,
            semantic_queries=[question],
            route=route,
            intent=intent,
            retrieval_profile=self.retrieval_profile,
            filters=filters,
            structured_operations=operations,
            planner_mode="rules_only",
            planner_reasons=reasons,
            rule_confidence=confidence,
            clarification_required=bool(
                confidence < self.model_threshold
                and (
                    _AMBIGUOUS.search(normalized)
                    or (route is QueryRoute.STRUCTURED and not (filters.metrics or filters.entities or filters.periods))
                )
            ),
            created_at=now,
        )
        if confidence >= self.model_threshold or self.model is None:
            return base
        try:
            patch = ModelPlanPatch.model_validate(self.model(question, base))
        except Exception as exc:
            return base.model_copy(
                update={
                    "planner_reasons": [*reasons, f"model_planner_failed:{type(exc).__name__}"],
                    "clarification_required": True,
                }
            )
        return QueryPlan(
            query_plan_id=base.query_plan_id,
            trace_id=trace_id,
            knowledge_base_id=knowledge_base_id,
            original_query=question,
            normalized_query=normalized,
            semantic_queries=patch.semantic_queries,
            route=patch.route,
            intent=patch.intent,
            retrieval_profile=self.retrieval_profile,
            filters=patch.filters,
            structured_operations=patch.structured_operations,
            planner_mode="rules_with_model",
            planner_reasons=[*reasons, f"model:{patch.reason}"],
            rule_confidence=confidence,
            clarification_required=patch.clarification_required,
            rejection_reason=patch.rejection_reason,
            created_at=now,
        )


def _normalize(question: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", question)).strip()


def _extract_filters(question: str, metrics: Sequence[str], entities: Sequence[str]) -> QueryFilters:
    periods = set()
    for year, month, day in _FULL_DATE.findall(question):
        periods.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
    for year, month in _MONTH.findall(question):
        prefix = f"{int(year):04d}-{int(month):02d}-"
        if not any(period.startswith(prefix) for period in periods):
            last_day = calendar.monthrange(int(year), int(month))[1]
            periods.add(f"{int(year):04d}-{int(month):02d}-{last_day:02d}")
    quarter_month_day = {
        "一": (3, 31),
        "1": (3, 31),
        "二": (6, 30),
        "2": (6, 30),
        "三": (9, 30),
        "3": (9, 30),
        "四": (12, 31),
        "4": (12, 31),
    }
    for year, quarter in _QUARTER.findall(question):
        month, day = quarter_month_day[quarter]
        periods.add(f"{int(year):04d}-{month:02d}-{day:02d}")
    for year in _YEAR.findall(question):
        if not any(period.startswith(year) for period in periods):
            periods.add(f"{int(year):04d}-12-31")
    return QueryFilters(
        document_numbers=sorted(set(match.group(0) for match in _DOCUMENT_NUMBER.finditer(question))),
        metrics=_matched_terms(question, metrics),
        entities=_matched_terms(question, entities),
        periods=sorted(periods),
        units=sorted({unit.replace("％", "%") for unit in _UNIT.findall(question)}),
    )


def _matched_terms(question: str, terms: Sequence[str]) -> list[str]:
    matched: list[str] = []
    for term in terms:
        if term in question and not any(term in existing for existing in matched):
            matched.append(term)
    return sorted(matched)


def _classify(
    question: str,
    filters: QueryFilters,
) -> tuple[QueryRoute, QueryIntent, list[StructuredOperation], float, list[str]]:
    statistic = bool(
        _STATISTIC.search(question)
        or (filters.metrics and filters.periods)
    )
    regulation = bool(_REGULATION.search(question) or filters.document_numbers or _CLAUSE.search(question))
    comparison = bool(_COMPARISON.search(question))
    operation_type = _operation_type(question)
    calculation = operation_type is not StructuredOperationType.LOOKUP
    reasons: list[str] = []
    if statistic:
        reasons.append("statistic_signal")
    if regulation:
        reasons.append("regulation_signal")
    if comparison:
        reasons.append("comparison_signal")
    operations: list[StructuredOperation] = []
    if statistic:
        operations.append(
            StructuredOperation(
                operation=operation_type,
                metric=filters.metrics[0] if len(filters.metrics) == 1 else None,
                entity=filters.entities[0] if len(filters.entities) == 1 else None,
                periods=filters.periods,
                unit=filters.units[0] if len(filters.units) == 1 else None,
            )
        )
    if statistic and regulation:
        route = QueryRoute.MIXED
    elif statistic:
        route = QueryRoute.STRUCTURED
    else:
        route = QueryRoute.DOCUMENT
    if statistic and (comparison or operation_type is not StructuredOperationType.LOOKUP):
        intent = QueryIntent.STATISTIC_COMPARISON if comparison else QueryIntent.STATISTIC_CALCULATION
    elif statistic:
        intent = QueryIntent.STATISTIC_LOOKUP
    elif comparison:
        intent = QueryIntent.REGULATION_COMPARISON
    elif _CLAUSE.search(question):
        intent = QueryIntent.CLAUSE_LOOKUP
    elif regulation:
        intent = QueryIntent.REGULATION_LOOKUP
    else:
        intent = QueryIntent.GENERAL_LOOKUP
    explicit = sum(
        bool(value)
        for value in (
            filters.metrics,
            filters.entities,
            filters.periods,
            filters.units,
            filters.document_numbers,
        )
    )
    confidence = min(0.98, 0.58 + explicit * 0.08 + (0.12 if statistic or regulation else 0))
    if _AMBIGUOUS.search(question):
        confidence = max(0.2, confidence - 0.3)
        reasons.append("ambiguous_reference")
    if calculation:
        reasons.append(f"operation:{operation_type.value}")
    if not reasons:
        reasons.append("general_semantic_lookup")
    return route, intent, operations, confidence, reasons


def _operation_type(question: str) -> StructuredOperationType:
    if re.search(r"求和|加总|总和是多少|合计计算", question):
        return StructuredOperationType.SUM
    if re.search(r"求平均|平均值是多少", question):
        return StructuredOperationType.AVERAGE
    if re.search(r"最大值|最高的是|最高值", question):
        return StructuredOperationType.MAXIMUM
    if re.search(r"最小值|最低的是|最低值", question):
        return StructuredOperationType.MINIMUM
    if re.search(r"(?:有|共有|共计)多少(?:家|户|个|笔|人)", question):
        return StructuredOperationType.COUNT
    if re.search(r"相差|差额|变化多少|增长了多少|下降了多少", question):
        return StructuredOperationType.DIFFERENCE
    if re.search(r"占.+(?:比例|比重)|比值", question):
        return StructuredOperationType.RATIO
    if "趋势" in question:
        return StructuredOperationType.TREND
    return StructuredOperationType.LOOKUP


def parse_model_json(content: str) -> dict[str, Any]:
    """从模型纯 JSON 或 Markdown 代码块中提取规划对象。

    :param content: 模型返回文本。
    :return: JSON 对象。
    :raises ValueError: 返回内容不是对象时抛出。
    """
    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = normalized[normalized.find("\n") + 1 :]
        if normalized.endswith("```"):
            normalized = normalized[:-3].rstrip()
    start = normalized.find("{")
    if start < 0:
        raise ValueError("模型规划结果不包含 JSON 对象。")
    value, _ = json.JSONDecoder().raw_decode(normalized[start:])
    if not isinstance(value, dict):
        raise ValueError("模型规划 JSON 根节点必须是对象。")
    return value
