"""Single semantic contract shared by planning, answering, and evaluation.

The query plan describes *how* to execute a supported request.  ``QuestionSpec``
describes *what kind of claim* the user is asking the evidence to support.  The
separation keeps proof-boundary logic out of SQL and avoids reimplementing the
same intent checks in the rule planner and the grounded answerer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class QuestionSpec:
    claim_type: str = "fact"
    answer_policy: str = "answer"
    proof_requirements: tuple[str, ...] = ()
    requested_granularity: str | None = None
    requested_exactness: str = "reported"
    boundary_reason: str | None = None


@dataclass(frozen=True)
class EvidenceBoundary:
    answer: str
    reason: str
    missing_information: tuple[str, ...]


CAUSAL_MARKERS = (
    "主要是因为",
    "主要因为",
    "主要来自",
    "是不是因为",
    "导致",
    "归因",
    "受当地",
    "受政策",
)
INFERENCE_MARKERS = (
    "能否证明",
    "是否说明",
    "能否判断",
    "能不能判断",
    "是否已经触发",
    "是否一定",
    "一定纳入",
)
INFERENCE_SUBJECTS = (
    "原因",
    "改善",
    "有效",
    "漏报",
    "风险客户",
    "盈利压力",
    "监管处置",
    "统计口径仍",
    "拖累",
)


def _requested_granularity(text: str) -> str | None:
    """Return the business level requested by the wording, not a table column.

    ``哪家``/``单家`` asks for an institution instance.  Keeping that semantic
    requirement separate from ``group_by=entity`` prevents an institution class
    (for example a row representing all rural commercial banks) from being
    mistaken for an individual institution.
    """

    if re.search(r"哪(?:一)?家|哪家公司|单家|某家|具体(?:哪家|公司)", text):
        return "institution"
    if re.search(r"名单|各(?:家|个).*(?:评级|结果)", text):
        return "institution_or_result_list"
    return None


def _fact_relevance(row: dict[str, Any], question: str) -> int:
    """Rank fact rows by the business wording used in the question."""

    query = re.sub(r"\s+", "", question or "")
    labels = " ".join(
        str(row.get(key) or "")
        for key in (
            "metric",
            "metric_name",
            "entity",
            "entity_name",
            "region",
            "region_name",
            "product_line",
            "product_line_name",
        )
    )
    labels = re.sub(r"\s+", "", labels)
    query_grams = {query[i : i + 2] for i in range(max(0, len(query) - 1))}
    label_grams = {labels[i : i + 2] for i in range(max(0, len(labels) - 1))}
    score = len(query_grams & label_grams)
    measure_type = str(row.get("measure_type") or "")
    if "同比" in query and measure_type == "year_over_year":
        score += 4
    if "环比" in query and measure_type == "month_over_month":
        score += 4
    quarter_months = {
        "一季度": "03",
        "第一季度": "03",
        "二季度": "06",
        "第二季度": "06",
        "三季度": "09",
        "第三季度": "09",
        "四季度": "12",
        "第四季度": "12",
    }
    period_end = str(row.get("period_end") or "")
    for marker, month in quarter_months.items():
        if marker in query and len(period_end) >= 7 and period_end[5:7] == month:
            score += 5
            break
    return score


def _verified_fact_summary(
    rows: list[dict[str, Any]], question: str = "", limit: int = 3
) -> str:
    """Render a compact, deterministic summary of facts already verified.

    The summary deliberately ignores capability and dictionary rows.  It is
    used only to explain the supported premise before an inference is refused.
    """

    ranked_rows = sorted(
        enumerate(rows),
        key=lambda item: (-_fact_relevance(item[1], question), item[0]),
    )
    facts: list[str] = []
    seen: set[str] = set()
    for _, row in ranked_rows:
        if row.get("evidence_kind"):
            continue
        value = row.get("value_display") or row.get("value")
        if value in (None, ""):
            continue
        labels = [
            str(row.get(key) or "").strip()
            for key in ("period_end", "entity", "entity_name", "region", "region_name", "product_line", "product_line_name", "metric", "metric_name")
        ]
        label = " ".join(dict.fromkeys(item for item in labels if item))
        unit = str(row.get("unit") or "").strip()
        rendered_value = str(value)
        if unit and not rendered_value.endswith(unit):
            rendered_value += unit
        statement = (
            f"{label}为{rendered_value}" if label else f"数值为{rendered_value}"
        )
        if statement in seen:
            continue
        seen.add(statement)
        facts.append(statement)
        if len(facts) >= limit:
            break
    return "；".join(facts)


def analyze_question(question: str) -> QuestionSpec:
    """Classify the requested claim without guessing missing business slots."""

    text = str(question or "").strip()
    requested_granularity = _requested_granularity(text)
    if re.search(r"预测|未来.*(?:会|能)|会不会|会超过", text):
        return QuestionSpec(
            claim_type="forecast",
            answer_policy="refuse",
            proof_requirements=("forecast_model", "forecast_assumptions"),
            requested_granularity=requested_granularity,
            boundary_reason=(
                "历史统计表只能描述已发生数据；缺少预测模型、假设和外部变量，"
                "不能据此给出未来结论。"
            ),
        )

    is_causal = any(marker in text for marker in CAUSAL_MARKERS) or bool(
        re.search(r"(?:解释|分析).{0,12}原因|原因(?:是|为何|是什么)|为什么|为何", text)
    )
    is_inference = any(marker in text for marker in INFERENCE_MARKERS) and any(
        subject in text for subject in INFERENCE_SUBJECTS
    )
    if is_causal or is_inference:
        return QuestionSpec(
            claim_type="inference",
            answer_policy="refuse",
            proof_requirements=("causal_or_decision_evidence",),
            requested_granularity=requested_granularity,
            boundary_reason=(
                "表内统计事实可支持变化或比较，但不能单独证明原因、政策效果、"
                "风险结论或监管处置；还需相关变量、规则或因果识别证据。"
            ),
        )

    exact_date = bool(
        re.search(
            r"到底(?:是)?哪一天|具体(?:是)?哪一天|确切(?:发布)?日期|"
            r"准确(?:发布)?日期|具体发布日期",
            text,
        )
    )
    availability = bool(re.search(r"能否?查到|能否给出|可以查到|查得出", text))
    detailed_results = availability and bool(
        re.search(r"名单|各(?:家|个).*评级|哪家|哪家公司|单家|具体(?:数值|结果)", text)
    )
    if availability:
        return QuestionSpec(
            claim_type="availability",
            proof_requirements=("result_records" if detailed_results else "fact_record",),
            requested_granularity=(
                requested_granularity or "institution_or_result_list"
                if detailed_results
                else requested_granularity
            ),
            requested_exactness="exact" if exact_date else "reported",
        )
    if exact_date:
        return QuestionSpec(
            claim_type="fact",
            proof_requirements=("exact_release_date",),
            requested_exactness="exact",
        )
    if re.search(r"占.+比例|变化了多少|数值变化|相减|增长率|平均", text):
        return QuestionSpec(
            claim_type="calculation", requested_granularity=requested_granularity
        )
    if re.search(r"最高|最低|最大|最小|比较|相比", text):
        return QuestionSpec(
            claim_type="comparison", requested_granularity=requested_granularity
        )
    return QuestionSpec(requested_granularity=requested_granularity)


def evidence_boundary(
    spec: QuestionSpec | None,
    evidence: Iterable[dict[str, Any]],
    question: str = "",
) -> EvidenceBoundary | None:
    """Return a deterministic refusal when evidence cannot entail the claim."""

    if spec is None:
        return None
    rows = list(evidence)
    requirements = set(spec.proof_requirements)

    missing_operands = [
        row for row in rows if row.get("evidence_kind") == "missing_operand"
    ]
    if missing_operands:
        labels = [
            str(row.get("requested_operand") or row.get("analysis_step") or "必要操作数")
            for row in missing_operands
        ]
        return EvidenceBoundary(
            answer=(
                "当前证据只能支持部分操作数，缺少"
                + "、".join(dict.fromkeys(labels))
                + "，不能完成所要求的计算。"
            ),
            reason="受控计算要求全部操作数都有可核验事实；缺失项不能用相近指标替代。",
            missing_information=tuple(dict.fromkeys(labels)),
        )

    if "exact_release_date" in requirements:
        timings = [
            str(row.get("release_timing") or "").strip()
            for row in rows
            if row.get("evidence_kind") == "release_schedule"
            and row.get("release_timing")
        ]
        actual_dates = [
            row
            for row in rows
            if row.get("evidence_kind") == "actual_release_record"
            or row.get("actual_release_date")
        ]
        if timings and not actual_dates and all(
            any(marker in timing for marker in ("左右", "约", "大约"))
            for timing in timings
        ):
            return EvidenceBoundary(
                answer="当前证据只能给出大致发布时间，不能确定具体发布日期。",
                reason="发布日程中的“左右”是约略安排，不是实际发布日期记录。",
                missing_information=("实际公告日期或官网发布记录",),
            )

    level_profiles = [
        row
        for row in rows
        if row.get("evidence_kind") == "source_capability_profile"
        and bool((row.get("listed_values_complete") or {}).get("entity_levels"))
    ]
    available_levels = {
        str(level)
        for row in level_profiles
        for level in row.get("available_entity_levels") or []
    }
    if (
        spec.requested_granularity
        in {"institution", "institution_or_result_list"}
        and level_profiles
        and available_levels
        and "institution" not in available_levels
    ):
        return EvidenceBoundary(
            answer="当前来源只有机构类别或行业汇总，不包含单家机构明细。",
            reason="完整实体层级清单未包含 institution 级记录，不能把机构类别当作具体公司。",
            missing_information=("包含单家机构结果的明细数据",),
        )

    if "result_records" in requirements:
        has_result_record = any(
            row.get("value") not in (None, "")
            or row.get("result") not in (None, "")
            or row.get("rating") not in (None, "")
            for row in rows
            if row.get("evidence_kind")
            not in {
                "release_schedule",
                "metric_definition",
                "institution_scope",
                "source_capability_profile",
            }
        )
        if rows and not has_result_record:
            return EvidenceBoundary(
                answer="当前来源只说明发布安排或指标范围，不包含所询问的具体名单或结果。",
                reason="指标名称出现在发布日程中，不等于该文件提供公司级结果数据。",
                missing_information=("包含具体机构结果的明细数据",),
            )

    if "causal_or_decision_evidence" in requirements:
        supported = any(
            row.get("evidence_kind") in {"causal_analysis", "regulatory_decision"}
            for row in rows
        )
        if not supported:
            verified = _verified_fact_summary(rows, question=question)
            premise = f"当前表内可验证：{verified}。" if verified else ""
            return EvidenceBoundary(
                answer=(
                    premise
                    + "但这些统计事实不足以支持所询问的原因、风险、政策效果或监管结论。"
                ),
                reason=(
                    "已先核对表内事实；现有证据不包含原因变量、因果识别材料、"
                    "适用规则或监管决定，不能从相关或变化直接推出结论。"
                ),
                missing_information=("原因变量、适用规则或监管决定",),
            )
    return None
