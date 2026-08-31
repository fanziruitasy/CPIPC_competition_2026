from __future__ import annotations

import calendar
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from model_api import api_endpoint, post_json
from question_spec import QuestionSpec, analyze_question
from query_schema import PLAN_FILTERS, SOURCE_FILTERS, normalize


def split_source_name(file_name: str) -> tuple[str, str | None]:
    parts = file_name.split("_", 2)
    if len(parts) == 3 and parts[0].isdigit():
        return parts[1], parts[2]
    return Path(file_name).stem, None


class QuestionError(ValueError):
    pass


class CapabilityCoverageError(QuestionError):
    """A complete source profile proves that a requested operand is absent."""

    def __init__(self, gaps: list[str]):
        self.gaps = gaps
        super().__init__("能力覆盖检查未通过：" + "；".join(gaps))


class AmbiguousQuestionError(QuestionError):
    pass


class ClarificationQuestionError(AmbiguousQuestionError):
    """An ambiguity that can be resolved with business-facing questions."""

    def __init__(
        self,
        message: str,
        questions: list[str] | None = None,
        candidates: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.questions = questions or []
        self.candidates = candidates or []


@dataclass
class AnalysisStep:
    """One independently resolvable operand in a controlled cross-table formula."""

    step_id: str
    source_title: str = ""
    sheet_name: str | None = None
    focus: str | None = None
    context: str | None = None
    filters: dict[str, str] = field(default_factory=dict)
    source_filters: dict[str, str] = field(default_factory=dict)


@dataclass
class QueryPlan:
    intent: str
    source_title: str
    sheet_name: str | None
    focus: str | None
    context: str | None
    from_term: str | None
    to_term: str | None
    operation: str
    options: dict[str, str]
    group_by: str | None = None
    filters: dict[str, str] = field(default_factory=dict)
    source_filters: dict[str, str] = field(default_factory=dict)
    answer_mode: str = "structured_query"
    planner: str = "rule"
    route: str = "fact_observation"
    rejection_reason: str | None = None
    analysis_steps: list[AnalysisStep] = field(default_factory=list)
    question_spec: QuestionSpec | None = None

    @property
    def rejected(self) -> bool:
        return self.route == "reject"


@dataclass
class AnswerResult:
    answer: str
    choice: str | None
    answer_text: str
    source_file: str
    dataset_family: str
    evidence: list[dict[str, Any]]
    plan: QueryPlan
    explanation: str | None = None


def rejected_plan(question: str, reason: str) -> QueryPlan:
    return QueryPlan(
        intent="reject",
        source_title="",
        sheet_name=None,
        focus=None,
        context=None,
        from_term=None,
        to_term=None,
        operation="reject",
        options={},
        answer_mode="clarify_or_reject",
        route="reject",
        rejection_reason=reason,
    )


def grounded_document_plan(
    question: str, options: dict[str, str] | None = None
) -> QueryPlan | None:
    """Create a retrieval plan for an open question naming an explicit source.

    This plan is only enabled when a grounded answer generator is available;
    otherwise returning the best matching row as a final answer would be unsafe.
    """
    title_match = re.search(r"《([^》]+)》", question)
    if not title_match:
        return None
    return grounded_source_plan(question, title_match.group(1).strip(), options)


def grounded_source_plan(
    question: str,
    source_title: str,
    options: dict[str, str] | None = None,
    *,
    sheet_name: str | None = None,
) -> QueryPlan:
    """Create an evidence-boundary plan for a source already identified."""
    return QueryPlan(
        intent="document_row",
        source_title=source_title,
        sheet_name=sheet_name,
        focus=None,
        context=question,
        from_term=None,
        to_term=None,
        operation="grounded_document_qa",
        options={key.upper(): value for key, value in (options or {}).items() if value},
        answer_mode="semantic_retrieval",
        planner="grounded_retrieval",
        route="document_row",
        question_spec=analyze_question(question),
    )


def _answer_mode(source_title: str, default: str = "structured_query") -> str:
    """Missing source metadata turns an otherwise direct task into a hybrid one."""
    return default if source_title else "hybrid"


GROUP_BY_ALIASES = {
    "region": ("地区", "区域", "省份", "省市", "哪个省", "哪一省"),
    "entity": ("机构", "公司", "银行", "哪家", "主体"),
    "product_line": ("险种", "产品线", "产品", "业务类型"),
    "metric": ("指标", "项目", "哪一项"),
}


def infer_group_by(question: str) -> str | None:
    target = normalize(question)
    for group_by, aliases in GROUP_BY_ALIASES.items():
        if any(normalize(alias) in target for alias in aliases):
            return group_by
    return None


def infer_rank_metric(question: str, group_by: str | None) -> str | None:
    if not group_by:
        return None
    quoted = [
        term.strip() for term in re.findall(r"“([^”]+)”", question) if term.strip()
    ]
    if quoted:
        return quoted[-1]
    group_words = "|".join(re.escape(value) for value in GROUP_BY_ALIASES[group_by])
    match = re.search(
        rf"(?:{group_words})(?:的)?(.+?)(?:数值|金额|余额|收入|规模)?(?:最高|最低|最大|最小|最多|最少)",
        question,
    )
    if not match:
        return None
    metric = re.sub(r"^(?:中|里|在)", "", match.group(1)).strip("，,。？?的 ")
    return metric or None


def extract_period_end(question: str) -> str | None:
    iso_dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
    if iso_dates:
        return iso_dates[0]
    day_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", question)
    if day_match:
        year, month, day = map(int, day_match.groups())
        return date(year, month, day).isoformat()
    month_match = re.search(r"(\d{4})年(\d{1,2})月", question)
    if month_match:
        year, month = map(int, month_match.groups())
        return date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    quarter_match = re.search(
        r"(\d{4})年(?:第)?([一二三四1234])季度", question
    )
    if quarter_match:
        year = int(quarter_match.group(1))
        quarter_token = quarter_match.group(2)
        quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(
            quarter_token, int(quarter_token) if quarter_token.isdigit() else 0
        )
        month = quarter * 3
        return date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    return None


def reasoning_boundary_plan(question: str) -> QueryPlan | None:
    """Reject inference tasks before source ambiguity can mask the real issue."""
    spec = analyze_question(question)
    if spec.boundary_reason:
        plan = rejected_plan(question, spec.boundary_reason)
        plan.planner = "boundary_rule"
        plan.question_spec = spec
        return plan
    return None


def _parse_question_execution(
    question: str, options: dict[str, str] | None = None
) -> QueryPlan:
    boundary = reasoning_boundary_plan(question)
    if boundary is not None:
        return boundary
    ratio_match = re.search(
        r"(.+?)占(.+?)(?:的)?比例(?:是(?:多少|多大)|为多少|多少)?[？?。]*$",
        question,
    )
    if ratio_match:
        numerator = re.sub(
            r"^(?:根据)?(?:\d{4}年\d{1,2}月(?:\d{1,2}日)?|\d{4}-\d{2}-\d{2})",
            "",
            ratio_match.group(1),
        ).strip("，,。的 ")
        denominator = ratio_match.group(2).strip("，,。的 ")
        period = extract_period_end(question)
        filters = {"period_end": period} if period else {}
        return QueryPlan(
            intent="cross_table_analysis",
            source_title="",
            sheet_name=None,
            focus="比例",
            context=question,
            from_term=None,
            to_term=None,
            operation="ratio",
            options={},
            answer_mode="analysis_pipeline",
            route="cross_table_analysis",
            analysis_steps=[
                AnalysisStep("numerator", focus=numerator, filters=dict(filters)),
                AnalysisStep("denominator", focus=denominator, filters=dict(filters)),
            ],
        )
    provided_options = {
        key.upper(): value for key, value in (options or {}).items() if value
    }
    metadata_match = re.search(r"在(.+?)“(.+?)”主题的\s*Excel\s*中", question)
    if metadata_match:
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
        metric_match = re.search(r"的“([^”]+)”记录", question)
        dimension_match = re.search(r"\d{4}-\d{2}-\d{2}[、,，]?\s*(.*?)的“", question)
        if not dates or not metric_match:
            return rejected_plan(question, "元数据检索缺少期间或指标")
        return QueryPlan(
            intent="metadata_retrieval",
            source_title="",
            sheet_name=None,
            focus=dimension_match.group(1).strip("、， ") if dimension_match else None,
            context=metadata_match.group(1).strip(),
            from_term=metadata_match.group(2).strip(),
            to_term=None,
            operation="metadata_retrieval",
            options=provided_options,
            filters={"period_end": dates[0], "metric": metric_match.group(1)},
            source_filters={
                "domain": metadata_match.group(1).strip(),
                "topic": metadata_match.group(2).strip(),
            },
            answer_mode="hybrid",
            route="metadata_retrieval",
        )

    title_match = re.search(r"《([^》]+)》", question)
    source_title = title_match.group(1).strip() if title_match else ""
    sheet_match = re.search(r"工作表\s*[：:]?\s*[“]?([^）)；;”]+)", question)
    sheet_name = sheet_match.group(1).strip() if sheet_match else None
    quoted = [term.strip() for term in re.findall(r"“([^”]+)”", question)]
    focus = quoted[-1] if quoted else None

    if "如何定义" in question and quoted:
        return QueryPlan(
            intent="dictionary",
            source_title=source_title,
            sheet_name=sheet_name,
            focus=quoted[-1],
            context=question,
            from_term=None,
            to_term=None,
            operation="metric_definition",
            options=provided_options,
            answer_mode=_answer_mode(source_title),
            route="dictionary",
        )
    if "机构范围是什么" in question and quoted:
        return QueryPlan(
            intent="dictionary",
            source_title=source_title,
            sheet_name=sheet_name,
            focus=quoted[-1],
            context=question,
            from_term=None,
            to_term=None,
            operation="institution_scope",
            options=provided_options,
            answer_mode=_answer_mode(source_title),
            route="dictionary",
        )
    if "通常何时发布" in question and quoted:
        return QueryPlan(
            intent="dictionary",
            source_title=source_title,
            sheet_name=sheet_name,
            focus=quoted[-1],
            context=question,
            from_term=None,
            to_term=None,
            operation="release_schedule",
            options=provided_options,
            answer_mode=_answer_mode(source_title),
            route="dictionary",
        )

    lookup_match = re.search(
        r"(\d{4}-\d{2}-\d{2})\s*的\s*(.*?)\s*“([^”]+)”数值是多少",
        question,
    )
    if "数值是多少" in question and lookup_match:
        entity = lookup_match.group(2).strip()
        metric = lookup_match.group(3).strip()
        if not entity or not metric:
            return rejected_plan(question, "取数题无法识别查询指标或实体")
        return QueryPlan(
            intent="lookup",
            source_title=source_title,
            sheet_name=sheet_name,
            focus=metric,
            context=entity,
            from_term=None,
            to_term=None,
            operation="lookup",
            options=provided_options,
            filters={"period_end": lookup_match.group(1)},
            answer_mode=_answer_mode(source_title),
        )

    if "数值变化约为多少" in question or "变化了多少" in question:
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
        context = None
        context_match = re.search(r"[，,]([^，。《》()（）]+?)的“", question)
        if context_match:
            context = context_match.group(1).strip()
        if quoted and len(dates) >= 2:
            focus, from_term, to_term = quoted[-1], dates[0], dates[1]
        elif len(quoted) >= 3:
            focus, from_term, to_term = quoted[-3], quoted[-2], quoted[-1]
        else:
            return rejected_plan(question, "计算题无法识别焦点、起点和终点")
        return QueryPlan(
            intent="delta",
            source_title=source_title,
            sheet_name=sheet_name,
            focus=focus,
            context=context,
            from_term=from_term,
            to_term=to_term,
            operation="to_minus_from",
            options=provided_options,
            answer_mode=_answer_mode(source_title),
        )

    high_words = ("数值最高", "最大", "最多", "最高")
    low_words = ("数值最低", "最小", "最少", "最低")
    comparison_text = re.sub(r"《[^》]+》", "", question)
    if any(token in comparison_text for token in high_words + low_words) and any(
        token in comparison_text
        for token in ("哪一项", "哪个", "哪些", "哪家", "哪类", "哪一")
    ):
        if not provided_options and len(quoted) >= 2:
            provided_options = {
                chr(ord("A") + index): term for index, term in enumerate(quoted[:-1])
            }
        group_by = None if provided_options else infer_group_by(comparison_text)
        rank_metric = (
            quoted[-1] if quoted else infer_rank_metric(comparison_text, group_by)
        )
        if not provided_options and not group_by:
            return rejected_plan(question, "比较题缺少可枚举的比较维度")
        if not rank_metric and not provided_options:
            return rejected_plan(question, "比较题无法识别要排名的金融指标")
        operation = (
            "argmax"
            if any(token in comparison_text for token in high_words)
            else "argmin"
        )
        return QueryPlan(
            intent="compare",
            source_title=source_title,
            sheet_name=sheet_name,
            focus=None,
            context=rank_metric,
            from_term=None,
            to_term=None,
            operation=operation,
            options=provided_options,
            group_by=group_by,
            filters={"period_end": extract_period_end(question)}
            if extract_period_end(question)
            else {},
            answer_mode=_answer_mode(source_title),
        )

    if "数值是多少" in question:
        if not quoted:
            return rejected_plan(question, "取数题无法识别查询指标或实体")
        return QueryPlan(
            intent="lookup",
            source_title=source_title,
            sheet_name=sheet_name,
            focus=quoted[-2] if len(quoted) >= 2 else quoted[-1],
            context=quoted[-1] if len(quoted) >= 2 else None,
            from_term=None,
            to_term=None,
            operation="lookup",
            options=provided_options,
            answer_mode=_answer_mode(source_title),
        )

    if "属于哪类机构" in question and quoted:
        operation = "reference_header"
        focus = quoted[-1]
    elif any(
        token in question
        for token in (
            "是什么？",
            "有哪些？",
            "包含哪些",
            "区分了哪些",
            "哪一岗位",
            "按哪些险种拆分",
            "最高分是多少",
            "指什么",
            "权重达到多少",
            "最低总分和最高总分",
            "请列出",
            "列出",
            "栏目",
            "填报项",
        )
    ):
        if any(
            token in question
            for token in (
                "最高分是多少",
                "指什么",
                "权重达到多少",
                "最低总分和最高总分",
            )
        ):
            operation = "rule_lookup"
            focus = quoted[-1] if quoted else None
            if not focus:
                rule_focus_matches = re.findall(
                    r"[\u4e00-\u9fffA-Za-z0-9]{2,}?(?:指标|情况|机制)", question
                )
                rule_focus_matches = [
                    item for item in rule_focus_matches if item not in source_title
                ]
                focus = rule_focus_matches[-1] if rule_focus_matches else None
        else:
            operation = "document_row"
    else:
        return rejected_plan(
            question,
            "暂不支持该题型；当前支持受控事实取数、词典/模板/规则检索、元数据定位和明确拒答。",
        )
    return QueryPlan(
        intent="document_row",
        source_title=source_title,
        sheet_name=sheet_name,
        focus=focus,
        context=question,
        from_term=None,
        to_term=None,
        operation=operation,
        options=provided_options,
        answer_mode=_answer_mode(source_title, "semantic_retrieval"),
        route="document_row",
    )


def parse_question(question: str, options: dict[str, str] | None = None) -> QueryPlan:
    """Build one execution plan and attach the shared semantic contract."""

    plan = _parse_question_execution(question, options)
    if plan.question_spec is None:
        plan.question_spec = analyze_question(question)
    return plan


ANSWER_MODES = {
    "structured_query",
    "semantic_retrieval",
    "hybrid",
    "analysis_pipeline",
    "clarify_or_reject",
}


def validated_query_plan(
    payload: Any, options: dict[str, str] | None = None
) -> QueryPlan:
    if not isinstance(payload, dict):
        raise QuestionError("Qwen 未返回 JSON 对象")
    external_intent = payload.get("intent") or payload.get("task_type")
    operation = payload.get("operation")
    intent_specs: dict[str, tuple[str, set[str], str, str]] = {
        "lookup": ("lookup", {"lookup"}, "fact_observation", "structured_query"),
        "compare": (
            "compare",
            {"argmax", "argmin"},
            "fact_observation",
            "structured_query",
        ),
        "rank": (
            "compare",
            {"argmax", "argmin"},
            "fact_observation",
            "structured_query",
        ),
        "delta": ("delta", {"to_minus_from"}, "fact_observation", "structured_query"),
        "aggregate": (
            "analysis",
            {"sum", "avg", "min", "max", "count"},
            "fact_analysis",
            "analysis_pipeline",
        ),
        "trend": ("analysis", {"trend"}, "fact_analysis", "analysis_pipeline"),
        "cross_table": (
            "cross_table_analysis",
            {"ratio", "difference", "sum", "average", "growth_rate"},
            "cross_table_analysis",
            "analysis_pipeline",
        ),
        "definition": (
            "dictionary",
            {"metric_definition"},
            "dictionary",
            "structured_query",
        ),
        "institution_scope": (
            "dictionary",
            {"institution_scope"},
            "dictionary",
            "structured_query",
        ),
        "release_schedule": (
            "dictionary",
            {"release_schedule"},
            "dictionary",
            "structured_query",
        ),
        "document_qa": (
            "document_row",
            {"document_row", "rule_lookup", "reference_header"},
            "document_row",
            "semantic_retrieval",
        ),
        "find_source": (
            "metadata_retrieval",
            {"find_source", "metadata_retrieval"},
            "resource_discovery",
            "hybrid",
        ),
    }
    if (
        external_intent in {"clarify", "reject"}
        or payload.get("needs_clarification") is True
    ):
        missing = payload.get("missing_conditions") or []
        if not isinstance(missing, list):
            missing = [str(missing)]
        reason = str(
            payload.get("rejection_reason")
            or "、".join(map(str, missing))
            or "问题条件不足，需要补充信息"
        )
        plan = rejected_plan("", reason)
        plan.planner = "llm"
        return plan
    if external_intent not in intent_specs:
        raise QuestionError(f"Qwen 返回了未支持的题型：{external_intent}")
    intent, allowed_operations, route, default_mode = intent_specs[str(external_intent)]
    if operation not in allowed_operations:
        raise QuestionError(f"Qwen 返回了与题型不匹配的操作：{operation}")

    def optional_text(name: str) -> str | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise QuestionError(f"Qwen 字段 {name} 必须是字符串或 null")
        return value.strip() or None

    source_title = optional_text("source_title")
    group_by = optional_text("group_by")
    if group_by not in {None, *GROUP_BY_ALIASES}:
        raise QuestionError(f"Qwen 返回了不允许的 group_by：{group_by}")
    filters = payload.get("filters") or {}
    if not isinstance(filters, dict) or set(filters) - PLAN_FILTERS:
        raise QuestionError("Qwen 返回了不允许的查询条件")
    if any(
        value is not None and not isinstance(value, str) for value in filters.values()
    ):
        raise QuestionError("Qwen 查询条件的值必须是字符串或 null")
    clean_filters = {
        key: value.strip()
        for key, value in filters.items()
        if isinstance(value, str) and value.strip()
    }
    raw_source_filters = payload.get("source_filters") or {}
    if (
        not isinstance(raw_source_filters, dict)
        or set(raw_source_filters) - SOURCE_FILTERS
    ):
        raise QuestionError("Qwen 返回了不允许的来源过滤条件")
    clean_source_filters = {
        key: value.strip()
        for key, value in raw_source_filters.items()
        if isinstance(value, str) and value.strip()
    }
    period_basis_aliases = {
        normalize("本年累计"): "YTD",
        normalize("本年累计/截至当期"): "YTD",
        normalize("季度"): "quarter_end",
        normalize("quarter end"): "quarter_end",
        normalize("point in time"): "point_in_time",
    }
    misplaced_basis = period_basis_aliases.get(
        normalize(clean_filters.get("measure_type"))
    )
    if misplaced_basis:
        clean_filters.pop("measure_type")
        clean_filters.setdefault("period_basis", misplaced_basis)
    if "period_basis" in clean_filters:
        clean_filters["period_basis"] = period_basis_aliases.get(
            normalize(clean_filters["period_basis"]), clean_filters["period_basis"]
        )
    if normalize(clean_filters.get("measure_type")) in {
        normalize("账面余额"),
        normalize("截至当期-账面余额"),
    }:
        clean_filters["measure_type"] = "balance"
    for key in ("period_end", "from_period", "to_period", "start_period", "end_period"):
        if key in clean_filters:
            try:
                date.fromisoformat(clean_filters[key])
            except ValueError as exc:
                raise QuestionError(f"Qwen 返回的 {key} 不是 ISO 日期") from exc
    has_range_period = "from_period" in clean_filters or "to_period" in clean_filters
    has_bounded_range = "start_period" in clean_filters or "end_period" in clean_filters
    if has_bounded_range and (
        ("start_period" in clean_filters) != ("end_period" in clean_filters)
    ):
        raise QuestionError("Qwen 返回了不完整的趋势期间范围")
    if intent in {"delta", "analysis"}:
        if "period_end" in clean_filters or (
            ("from_period" in clean_filters) != ("to_period" in clean_filters)
        ):
            if intent == "delta" or has_range_period:
                raise QuestionError("Qwen 返回了不完整或冲突的期间条件")
    elif has_range_period:
        raise QuestionError("Qwen 在非差值题中返回了两期条件")

    raw_candidates = payload.get("candidates") or payload.get("options") or {}
    if isinstance(raw_candidates, list):
        candidate_options = {
            chr(ord("A") + index): str(value).strip()
            for index, value in enumerate(raw_candidates)
            if str(value).strip()
        }
    elif isinstance(raw_candidates, dict):
        candidate_options = {
            str(key).upper(): str(value).strip()
            for key, value in raw_candidates.items()
            if str(value).strip()
        }
    else:
        raise QuestionError("Qwen candidates 必须是数组或对象")
    final_options = {
        key.upper(): value for key, value in (options or {}).items() if value
    } or candidate_options

    analysis_steps: list[AnalysisStep] = []
    raw_steps = payload.get("analysis_steps") or []
    if intent == "cross_table_analysis":
        if not isinstance(raw_steps, list) or not 2 <= len(raw_steps) <= 8:
            raise QuestionError("跨表分析必须包含2至8个受控步骤")
        seen_step_ids: set[str] = set()
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                raise QuestionError("跨表分析步骤必须是JSON对象")
            step_id = str(raw_step.get("id") or raw_step.get("step_id") or f"step_{index}").strip()
            if not step_id or step_id in seen_step_ids:
                raise QuestionError("跨表分析步骤ID为空或重复")
            seen_step_ids.add(step_id)
            step_filters = raw_step.get("filters") or {}
            if not isinstance(step_filters, dict) or set(step_filters) - PLAN_FILTERS:
                raise QuestionError(f"跨表步骤{step_id}包含不允许的查询条件")
            clean_step_filters = {
                str(key): str(value).strip()
                for key, value in step_filters.items()
                if value is not None and str(value).strip()
            }
            if "period_basis" in clean_step_filters:
                clean_step_filters["period_basis"] = period_basis_aliases.get(
                    normalize(clean_step_filters["period_basis"]),
                    clean_step_filters["period_basis"],
                )
            for key in (
                "period_end",
                "from_period",
                "to_period",
                "start_period",
                "end_period",
            ):
                if key in clean_step_filters:
                    try:
                        date.fromisoformat(clean_step_filters[key])
                    except ValueError as exc:
                        raise QuestionError(
                            f"跨表步骤{step_id}的{key}不是ISO日期"
                        ) from exc
            step_source_filters = raw_step.get("source_filters") or {}
            if (
                not isinstance(step_source_filters, dict)
                or set(step_source_filters) - SOURCE_FILTERS
            ):
                raise QuestionError(f"跨表步骤{step_id}包含不允许的来源条件")
            step_focus = raw_step.get("focus")
            if step_focus is not None and not isinstance(step_focus, str):
                raise QuestionError(f"跨表步骤{step_id}的focus必须是字符串")
            step_context = raw_step.get("context")
            if step_context is not None and not isinstance(step_context, str):
                raise QuestionError(f"跨表步骤{step_id}的context必须是字符串")
            if not (str(step_focus or "").strip() or clean_step_filters):
                raise QuestionError(f"跨表步骤{step_id}缺少指标或过滤条件")
            analysis_steps.append(
                AnalysisStep(
                    step_id=step_id,
                    source_title=str(raw_step.get("source_title") or "").strip(),
                    sheet_name=str(raw_step.get("sheet_name") or "").strip() or None,
                    focus=str(step_focus or "").strip() or None,
                    context=str(step_context or "").strip() or None,
                    filters=clean_step_filters,
                    source_filters={
                        str(key): str(value).strip()
                        for key, value in step_source_filters.items()
                        if value is not None and str(value).strip()
                    },
                )
            )

    requested_mode = payload.get("answer_mode")
    if requested_mode is not None and requested_mode not in ANSWER_MODES:
        raise QuestionError(f"Qwen 返回了未知回答模式：{requested_mode}")
    answer_mode = str(requested_mode or default_mode)
    if not source_title and answer_mode in {"structured_query", "semantic_retrieval"}:
        answer_mode = "hybrid"

    plan = QueryPlan(
        intent=intent,
        source_title=source_title or "",
        sheet_name=optional_text("sheet_name"),
        focus=optional_text("focus"),
        context=optional_text("context"),
        from_term=optional_text("from_term"),
        to_term=optional_text("to_term"),
        operation=operation,
        options=final_options,
        group_by=group_by,
        filters=clean_filters,
        source_filters=clean_source_filters,
        answer_mode=answer_mode,
        route=route,
        analysis_steps=analysis_steps,
    )
    required = {
        "lookup": (plan.focus,),
        "compare": (plan.focus or plan.context, plan.options or plan.group_by),
        "delta": (plan.focus, plan.from_term, plan.to_term),
        "dictionary": (plan.focus,)
        if plan.operation != "release_schedule"
        else (plan.context,),
        "document_row": (plan.context,),
        "metadata_retrieval": (plan.source_filters or plan.filters or plan.context,),
        "analysis": (plan.focus or plan.context,),
        "cross_table_analysis": (plan.analysis_steps,),
    }[intent]
    if not all(required):
        raise QuestionError("Qwen 返回的查询计划缺少必要条件")
    return plan


class QwenQueryPlanner:
    def __init__(self, api_key: str, model: str, endpoint: str, timeout: int = 30):
        self.api_key = api_key
        self.model = model
        self.endpoint = api_endpoint(endpoint, "chat/completions")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> QwenQueryPlanner | None:
        api_key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            model=os.environ.get("QWEN_MODEL", "qwen3.7-plus"),
            endpoint=os.environ.get(
                "DASHSCOPE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
        )

    def __call__(
        self, question: str, options: dict[str, str] | None = None
    ) -> QueryPlan:
        system_prompt = """
你是金融 Excel 问答系统的查询规划器，只生成受控计划，不回答、不计算、不生成 SQL。
只输出 JSON 对象，字段为：
intent、answer_mode、source_title、sheet_name、source_filters、focus、context、from_term、to_term、operation、filters、group_by、candidates、analysis_steps、rejection_reason。

intent 只能是：
lookup、compare、rank、delta、aggregate、trend、cross_table、definition、institution_scope、release_schedule、document_qa、find_source、clarify、reject。
对应 operation：
- lookup -> lookup
- compare/rank -> argmax 或 argmin
- delta -> to_minus_from
- aggregate -> sum、avg、min、max 或 count
- trend -> trend
- cross_table -> ratio、difference、sum、average 或 growth_rate；必须给出 analysis_steps，每步包含 id、source_title、sheet_name、source_filters、focus、context、filters。每步只描述一个可独立查询的操作数，不得写 SQL 或任意公式
- definition -> metric_definition
- institution_scope -> institution_scope
- release_schedule -> release_schedule
- document_qa -> document_row、rule_lookup 或 reference_header
- find_source -> find_source

answer_mode 只能是 structured_query、semantic_retrieval、hybrid、analysis_pipeline、clarify_or_reject。
filters 只允许 metric、entity、entity_type、region、region_type、period_end、from_period、to_period、start_period、end_period、period_basis、scope、product_line、measure_type、unit。
source_filters 只允许 domain、topic、dataset_family、content_type、frequency。
题目未给文件标题时 source_title 必须为 null，不得猜文件名；此时通常选择 hybrid。题目中的候选比较项写入 candidates。
开放式最高/最低问题不需要虚构 candidates：把要比较的指标写入 focus，公共业务口径写入 context，并把 group_by 设为 region、entity、product_line 或 metric。
单期查询用 period_end，两点变化用 from_period/to_period，连续趋势范围用 start_period/end_period；日期使用 YYYY-MM-DD。只填明确条件，不得猜测。
确实缺少期间、机构、口径等必要条件时使用 clarify，并在 rejection_reason 中写明缺项。
""".strip()
        result = post_json(
            self.endpoint,
            api_key=self.api_key,
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"question": question, "options": options or {}},
                            ensure_ascii=False,
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=self.timeout,
            service_name="Qwen ",
            error_type=QuestionError,
        )
        try:
            content = result["choices"][0]["message"]["content"]
            plan = validated_query_plan(json.loads(content), options)
            plan.planner = f"qwen:{self.model}"
            plan.question_spec = analyze_question(question)
            return plan
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise QuestionError("Qwen 返回了无效的查询计划") from exc


class BailianEmbeddingClient:
    """Cached batch client for Bailian's OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v4",
        endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        *,
        dimensions: int | None = None,
        timeout: int = 30,
        batch_size: int = 10,
        max_input_chars: int = 6000,
    ):
        self.api_key = api_key
        self.model = model
        self.endpoint = api_endpoint(endpoint, "embeddings")
        self.dimensions = dimensions
        self.timeout = timeout
        self.batch_size = max(1, batch_size)
        self.max_input_chars = max(256, max_input_chars)
        self._cache: dict[str, tuple[float, ...]] = {}
        self._lock = RLock()

    @staticmethod
    def _optional_int(name: str) -> int | None:
        value = os.environ.get(name)
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise QuestionError(f"环境变量 {name} 必须是整数") from exc

    @classmethod
    def from_env(cls) -> BailianEmbeddingClient | None:
        provider = os.environ.get("RAG_VECTOR_PROVIDER", "auto").strip().lower()
        if provider in {"off", "false", "none", "local", "offline"}:
            return None
        api_key = (
            os.environ.get("BAILIAN_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
            or os.environ.get("QWEN_API_KEY")
        )
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            model=os.environ.get("BAILIAN_EMBEDDING_MODEL", "text-embedding-v4"),
            endpoint=os.environ.get(
                "BAILIAN_BASE_URL",
                os.environ.get(
                    "DASHSCOPE_BASE_URL",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                ),
            ),
            dimensions=cls._optional_int("BAILIAN_EMBEDDING_DIMENSIONS"),
            timeout=cls._optional_int("BAILIAN_EMBEDDING_TIMEOUT") or 30,
            batch_size=cls._optional_int("BAILIAN_EMBEDDING_BATCH_SIZE") or 10,
            max_input_chars=cls._optional_int("BAILIAN_EMBEDDING_MAX_CHARS") or 6000,
        )

    def _request_embeddings(self, inputs: list[str]) -> list[tuple[float, ...]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": inputs,
            "encoding_format": "float",
        }
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        result = post_json(
            self.endpoint,
            api_key=self.api_key,
            payload=payload,
            timeout=self.timeout,
            service_name="百炼向量",
            error_type=QuestionError,
        )
        try:
            ordered = sorted(result["data"], key=lambda item: int(item["index"]))
            vectors = [
                tuple(float(value) for value in item["embedding"]) for item in ordered
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise QuestionError("百炼返回了无效的向量结果") from exc
        if len(vectors) != len(inputs) or any(not vector for vector in vectors):
            raise QuestionError("百炼返回的向量数量或维度不正确")
        return vectors

    def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        prepared = [str(text or "")[: self.max_input_chars] for text in texts]
        with self._lock:
            missing = list(
                dict.fromkeys(text for text in prepared if text not in self._cache)
            )
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            vectors = self._request_embeddings(batch)
            with self._lock:
                self._cache.update(zip(batch, vectors))
        with self._lock:
            return [self._cache[text] for text in prepared]

    @staticmethod
    def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        if len(left) != len(right):
            raise QuestionError("百炼查询向量和资源向量维度不一致")
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return max(
            0.0, sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
        )

    def similarities(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        vectors = self.embed([query, *documents])
        return [self._cosine(vectors[0], vector) for vector in vectors[1:]]


def title_score(query: str, source: dict[str, Any]) -> float:
    target = normalize(query)
    candidates = [
        normalize(source.get("source_title")),
        normalize(Path(str(source.get("attachment_name") or "")).stem),
        normalize(Path(str(source.get("file_name") or "")).stem),
    ]
    if target in candidates:
        return 1.0
    if any(
        target and (target in candidate or candidate in target)
        for candidate in candidates
    ):
        return 0.95
    return max(
        (SequenceMatcher(None, target, candidate).ratio() for candidate in candidates),
        default=0.0,
    )


def source_named_in_question(
    question: str, sources: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Find a workbook title written inline, even without Chinese book quotes."""
    removable_publishers = (
        "国家金融监督管理总局",
        "中国银保监会",
        "银保监会",
    )

    def relaxed(value: Any) -> str:
        text = normalize(value)
        for publisher in removable_publishers:
            text = text.replace(normalize(publisher), "")
        return text

    target = relaxed(question)
    matches: list[tuple[int, dict[str, Any]]] = []
    for source in sources:
        candidate = relaxed(source.get("source_title"))
        if candidate and candidate in target:
            matches.append((len(candidate), source))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    best_length = matches[0][0]
    best = [source for length, source in matches if length == best_length]
    return best[0] if len(best) == 1 else None


def resolve_source(query: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    scored = sorted(
        ((title_score(query, source), source) for source in sources),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 0.72:
        raise QuestionError(f"找不到题目指定的Excel：{query}")
    best_score = scored[0][0]
    best = [source for score, source in scored if abs(score - best_score) < 0.0001]
    if len(best) > 1:
        exact_title = [
            source
            for source in best
            if normalize(source.get("source_title")) == normalize(query)
        ]
        if len(exact_title) == 1:
            return exact_title[0]
        names = [str(source.get("file_name")) for source in best[:5]]
        raise AmbiguousQuestionError(f"Excel标题对应多个源文件：{names}")
    return best[0]


def _source_profile(source: dict[str, Any]) -> str:
    return " | ".join(
        str(source.get(field) or "")
        for field in (
            "source_title",
            "attachment_name",
            "file_name",
            "domain_name",
            "topic_name",
            "dataset_family",
            "content_type_code",
            "document_function",
            "frequency",
            "evidence_summary",
            "source_sheet_names",
        )
    )


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise QuestionError(f"环境变量 {name} 必须是数字") from exc


def plan_vector_query(question: str, plan: QueryPlan) -> str:
    """Enrich the semantic query without applying any lexical or metadata score."""
    return " | ".join(
        str(value)
        for value in (
            question,
            plan.focus,
            plan.context,
            *plan.filters.values(),
        )
        if value
    )


def source_business_scope(source: dict[str, Any]) -> str:
    profile = normalize(
        " | ".join(
            str(source.get(field) or "")
            for field in (
                "source_title",
                "attachment_name",
                "topic_name",
                "dataset_family",
            )
        )
    )
    if normalize("全国各地区") in profile or normalize("地区原保险保费") in profile:
        return "各地区保险市场"
    if any(token in profile for token in (normalize("财产保险"), normalize("财险"))):
        return "财产保险公司"
    if any(
        token in profile
        for token in (normalize("人身保险"), normalize("人身险"), normalize("寿险"))
    ):
        return "人身保险公司"
    if normalize("保险业") in profile:
        return "全保险行业"
    if normalize("商业银行") in profile:
        return "商业银行"
    if normalize("银行业金融机构") in profile:
        return "银行业金融机构"
    return str(source.get("topic_name") or source.get("dataset_family") or "").strip()


def source_clarification(
    scored: list[tuple[float, dict[str, Any]]], plan: QueryPlan
) -> ClarificationQuestionError:
    near_best = [
        source for score, source in scored[:8] if scored and scored[0][0] - score < 0.05
    ]
    scopes = list(
        dict.fromkeys(filter(None, (source_business_scope(item) for item in near_best)))
    )
    frequencies = list(
        dict.fromkeys(
            str(item.get("frequency") or "").strip()
            for item in near_best
            if str(item.get("frequency") or "").strip()
        )
    )
    questions: list[str] = []
    if len(scopes) > 1:
        questions.append(f"您要查询的业务范围是{'、'.join(scopes)}中的哪一种？")
    if len(frequencies) > 1:
        questions.append(f"您需要{'、'.join(frequencies)}中的哪种统计频率？")
    if not any(
        plan.filters.get(key)
        for key in (
            "period_end",
            "from_period",
            "to_period",
            "start_period",
            "end_period",
        )
    ):
        questions.append("您要查询哪个日期或统计期间？")
    if not questions:
        questions.append("请补充业务范围、统计期间或数据口径中的任意一项。")
    candidates = [
        {
            "business_scope": source_business_scope(source),
            "topic": source.get("topic_name"),
            "frequency": source.get("frequency"),
            "source_title": source.get("source_title"),
        }
        for source in near_best[:5]
    ]
    return ClarificationQuestionError(
        "根据当前业务条件定位到多个可能的数据来源。",
        questions=questions,
        candidates=candidates,
    )


def _character_ngrams(value: Any, size: int = 2) -> set[str]:
    text = normalize(value)
    if len(text) < size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _source_capability_score(
    repository: Any, source: dict[str, Any], query: str
) -> float:
    """Score explicit dimension values supported by a workbook.

    This complements embeddings with exhaustive table metadata.  A source that
    contains both the requested region and product is a stronger candidate than
    one that merely has a similar title.  Only complete capability values are
    used; unknown coverage never becomes positive proof.
    """

    loader = getattr(repository, "source_capabilities", None)
    if not callable(loader):
        return 0.0
    target = normalize(query)
    target_ngrams = _character_ngrams(target)
    score = 0.0
    field_weights = {
        "available_regions": 4.0,
        "available_entities": 4.0,
        "available_product_lines": 3.0,
        "available_metrics": 2.0,
    }
    for profile in loader(str(source.get("file_name") or ""), None) or []:
        for field, weight in field_weights.items():
            for value in profile.get(field) or []:
                terms = _coverage_terms(value)
                if any(term and term in target for term in terms):
                    score += weight * max(len(term) for term in terms)
                    continue
                overlap = target_ngrams & _character_ngrams(value)
                if field == "available_metrics" and overlap:
                    score += weight * min(len(overlap), 3)
    return score


def resolve_plan_source(
    question: str,
    plan: QueryPlan,
    sources: list[dict[str, Any]],
    repository: Any | None = None,
    vector_provider: Any | None = None,
) -> dict[str, Any]:
    """Use explicit name matching or Bailian-only semantic source retrieval."""
    if plan.source_title:
        return resolve_source(plan.source_title, sources)

    candidates = filter_sources_by_metadata(sources, plan.source_filters)
    if plan.source_filters and not candidates:
        raise QuestionError("来源元数据条件预筛选后没有候选 Excel")
    candidate_loader = getattr(repository, "source_candidates", None)
    if callable(candidate_loader) and plan.route in {
        "fact_observation",
        "fact_analysis",
        "resource_discovery",
    }:
        hard_filters = dict(plan.filters)
        metric_hint = plan.focus or (plan.context if plan.intent == "compare" else None)
        if metric_hint and plan.intent in {"lookup", "delta", "analysis", "compare"}:
            hard_filters.setdefault("metric", metric_hint)
        candidate_files = set(candidate_loader(hard_filters))
        if not candidate_files and "metric" in hard_filters:
            # A natural-language metric phrase can contain dimensions as well
            # as the canonical metric name.  Preserve genuinely hard period or
            # region constraints instead of discarding all pre-filtering.
            candidate_files = set(
                candidate_loader(
                    {key: value for key, value in hard_filters.items() if key != "metric"}
                )
            )
        if candidate_files:
            candidates = [
                source
                for source in candidates
                if str(source.get("file_name") or "") in candidate_files
            ]
    if not candidates:
        raise QuestionError("事实条件预筛选后没有可召回的 Excel")
    if len(candidates) == 1:
        return candidates[0]
    capability_query = " ".join(
        str(value)
        for value in (plan.focus, plan.context, question)
        if value not in (None, "")
    )
    capability_scored = (
        sorted(
            (
                (_source_capability_score(repository, source, capability_query), source)
                for source in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if len(candidates) <= 50
        else []
    )
    if capability_scored and capability_scored[0][0] > 0:
        runner_up = capability_scored[1][0] if len(capability_scored) > 1 else -1.0
        if capability_scored[0][0] - runner_up >= 2.0:
            return capability_scored[0][1]
    if vector_provider is None:
        raise QuestionError(
            "未配置百炼向量服务；未提供文件名时必须设置 DASHSCOPE_API_KEY"
        )
    candidate_scores = list(
        vector_provider.similarities(
            plan_vector_query(question, plan),
            [_source_profile(source) for source in candidates],
        )
    )
    if len(candidate_scores) != len(candidates):
        raise QuestionError("百炼返回的文件向量分数数量不正确")
    scored = sorted(
        zip(candidate_scores, candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    minimum_score = env_float("BAILIAN_FILE_MIN_SCORE", 0.20)
    minimum_margin = env_float("BAILIAN_FILE_MIN_MARGIN", 0.02)
    if not scored or scored[0][0] < minimum_score:
        raise QuestionError("百炼向量没有召回到足够相关的 Excel")
    if len(scored) > 1 and scored[0][0] - scored[1][0] < minimum_margin:
        raise source_clarification(scored, plan)
    return scored[0][1]


SOURCE_METADATA_FIELDS: dict[str, tuple[str, ...]] = {
    "domain": ("domain_name", "domain_code"),
    "topic": ("topic_name", "topic_code"),
    "dataset_family": ("dataset_family",),
    "content_type": ("content_type_code", "content_type_name"),
    "frequency": ("frequency",),
}


def filter_sources_by_metadata(
    sources: list[dict[str, Any]], filters: dict[str, str] | None
) -> list[dict[str, Any]]:
    """Apply metadata progressively without allowing a taxonomy alias to erase recall."""
    active = filters or {}
    if not active:
        return list(sources)
    output = list(sources)
    for filter_name, requested in active.items():
        narrowed: list[dict[str, Any]] = []
        for source in output:
            columns = SOURCE_METADATA_FIELDS.get(filter_name, ())
            expected = normalize(requested)
            values = [
                normalize(source.get(column))
                for column in (*columns, "evidence_summary")
            ]
            if any(
                expected == value or expected in value or value in expected
                for value in values
                if expected and value
            ):
                narrowed.append(source)
        # Source catalogs can combine multiple dictionary families under one
        # workbook.  Treat an unmatched taxonomy label as a hint, not as proof
        # that every source is impossible.
        if narrowed:
            output = narrowed
    return output


def resolve_plan_sheet(
    question: str,
    plan: QueryPlan,
    repository: Any,
    file_name: str,
    vector_provider: Any | None = None,
) -> str | None:
    profile_loader = getattr(repository, "sheet_profiles", None)
    if not callable(profile_loader):
        return plan.sheet_name
    profiles = profile_loader(file_name)
    if not profiles:
        if plan.sheet_name:
            return plan.sheet_name
        if plan.operation == "grounded_document_qa":
            # Dictionary-only workbooks can have no fact/document profile in
            # older databases.  Open evidence retrieval can still read their
            # metric/scope/release dictionaries directly.
            return None
        raise QuestionError("该文件没有可用于向量召回的 Sheet 画像")
    if plan.sheet_name:
        expected = normalize(plan.sheet_name)
        matches = [
            row
            for row in profiles
            if expected == normalize(row.get("source_sheet"))
            or expected in normalize(row.get("source_sheet"))
            or normalize(row.get("source_sheet")) in expected
        ]
        if len(matches) == 1:
            return str(matches[0]["source_sheet"])
        if not matches:
            raise QuestionError(f"指定工作表不存在：{plan.sheet_name}")
        raise AmbiguousQuestionError(
            f"工作表名称命中多项：{[row['source_sheet'] for row in matches]}"
        )
    if len(profiles) == 1:
        return str(profiles[0]["source_sheet"])
    if vector_provider is None:
        raise QuestionError(
            "未配置百炼向量服务；未提供 Sheet 名时必须设置 DASHSCOPE_API_KEY"
        )
    profile_scores = list(
        vector_provider.similarities(
            plan_vector_query(question, plan),
            [
                f"{profile.get('source_sheet') or ''} | {profile.get('content') or ''}"
                for profile in profiles
            ],
        )
    )
    if len(profile_scores) != len(profiles):
        raise QuestionError("百炼返回的 Sheet 向量分数数量不正确")
    ranked = sorted(
        zip(profile_scores, profiles), key=lambda item: item[0], reverse=True
    )
    minimum_score = env_float("BAILIAN_SHEET_MIN_SCORE", 0.20)
    minimum_margin = env_float("BAILIAN_SHEET_MIN_MARGIN", 0.02)
    if ranked[0][0] < minimum_score:
        raise QuestionError("百炼向量没有召回到足够相关的 Sheet")
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < minimum_margin:
        raise ClarificationQuestionError(
            "百炼向量召回到多个相近的工作表。",
            questions=["请补充业务指标、机构范围或直接说明工作表名称。"],
            candidates=[
                {"source_sheet": profile.get("source_sheet"), "score": score}
                for score, profile in ranked[:5]
            ],
        )
    return str(ranked[0][1]["source_sheet"])


CAPABILITY_FILTERS: dict[str, tuple[str, str | None]] = {
    "metric": ("available_metrics", "metrics"),
    "entity": ("available_entities", "entities"),
    "entity_type": ("available_entity_types", "entity_types"),
    "region": ("available_regions", "regions"),
    "region_type": ("available_region_types", "region_types"),
    "product_line": ("available_product_lines", "product_lines"),
    "measure_type": ("available_measure_types", None),
    "period_basis": ("available_period_bases", None),
    "unit": ("available_units", None),
}

CANONICAL_VALUE_ALIASES: dict[str, tuple[str, ...]] = {
    "originalpremiumincome": ("原保险保费收入", "原保险保费收入合计"),
    "healthinsurance": ("健康险",),
    "lifeinsurance": ("寿险",),
    "accidentinsurance": ("意外险", "人身意外伤害险"),
    "propertyinsurance": ("财产险", "财产保险"),
    "all": ("合计", "总计"),
}


def _coverage_terms(value: Any) -> set[str]:
    term = normalize(value)
    terms = {term} if term else set()
    terms.update(normalize(alias) for alias in CANONICAL_VALUE_ALIASES.get(term, ()))
    if term.endswith(normalize("市")):
        terms.add(term[: -len(normalize("市"))])
    return {item for item in terms if item}


def _profile_has_value(profile: dict[str, Any], field: str, requested: str) -> bool:
    available_field, _ = CAPABILITY_FILTERS[field]
    requested_terms = _coverage_terms(requested)
    available_terms = {
        alias
        for value in profile.get(available_field) or []
        for alias in _coverage_terms(value)
    }
    return bool(requested_terms & available_terms)


def capability_gaps(profiles: list[dict[str, Any]], plan: QueryPlan) -> list[str]:
    """Return only gaps proven by exhaustive profiles; top-k misses are ignored."""
    if not profiles:
        return []
    gaps: list[str] = []
    for filter_name, requested in plan.filters.items():
        if filter_name not in CAPABILITY_FILTERS or not requested:
            continue
        _, completeness_key = CAPABILITY_FILTERS[filter_name]
        complete_profiles = [
            profile
            for profile in profiles
            if completeness_key is None
            or bool((profile.get("listed_values_complete") or {}).get(completeness_key))
        ]
        if complete_profiles and not any(
            _profile_has_value(profile, filter_name, requested)
            for profile in complete_profiles
        ):
            gaps.append(f"缺少{filter_name}={requested}")

    available_periods = {
        str(period)
        for profile in profiles
        for period in profile.get("available_periods") or []
        if period not in (None, "")
    }
    for period_filter in ("period_end", "from_period", "to_period"):
        requested = plan.filters.get(period_filter)
        if requested and available_periods and requested not in available_periods:
            gaps.append(f"缺少{period_filter}={requested}")
    period_start = min(
        (str(profile.get("period_start")) for profile in profiles if profile.get("period_start")),
        default=None,
    )
    period_end = max(
        (str(profile.get("period_end")) for profile in profiles if profile.get("period_end")),
        default=None,
    )
    if plan.filters.get("start_period") and period_start:
        requested = plan.filters["start_period"]
        if requested < period_start:
            gaps.append(f"期间起点早于覆盖范围={period_start}")
    if plan.filters.get("end_period") and period_end:
        requested = plan.filters["end_period"]
        if requested > period_end:
            gaps.append(f"期间终点晚于覆盖范围={period_end}")

    dimension_keys = {
        "entity": "entities",
        "region": "regions",
        "product_line": "product_lines",
        "metric": "metrics",
    }
    dimension_key = dimension_keys.get(plan.group_by or "")
    if plan.intent == "compare" and dimension_key:
        maximum = max(
            (
                int((profile.get("dimension_cardinality") or {}).get(dimension_key) or 0)
                for profile in profiles
            ),
            default=0,
        )
        complete = all(
            bool((profile.get("listed_values_complete") or {}).get(dimension_key))
            for profile in profiles
        )
        if complete and maximum < 2:
            gaps.append(f"{plan.group_by}维度不足两个可比较对象")
    requested_granularity = (
        plan.question_spec.requested_granularity if plan.question_spec else None
    )
    if requested_granularity in {"institution", "institution_or_result_list"}:
        level_profiles = [
            profile
            for profile in profiles
            if bool(
                (profile.get("listed_values_complete") or {}).get("entity_levels")
            )
        ]
        available_levels = {
            str(level)
            for profile in level_profiles
            for level in profile.get("available_entity_levels") or []
        }
        if level_profiles and available_levels and "institution" not in available_levels:
            gaps.append("源表仅含机构类别或汇总，不含单家机构明细")
    return list(dict.fromkeys(gaps))


def enforce_capability_coverage(
    repository: Any, source: dict[str, Any], plan: QueryPlan
) -> list[dict[str, Any]]:
    loader = getattr(repository, "source_capabilities", None)
    if not callable(loader):
        return []
    profiles = loader(str(source.get("file_name") or ""), plan.sheet_name) or []
    gaps = capability_gaps(profiles, plan)
    if gaps:
        raise CapabilityCoverageError(gaps)
    return profiles


def row_terms(row: dict[str, Any]) -> set[str]:
    fields = [
        "entity_code",
        "entity_name",
        "entity_type",
        "region_code",
        "region_name",
        "region_type",
        "metric_code",
        "metric_name",
        "metric_name_raw",
        "metric_group",
        "metric_path",
        "product_line",
        "product_line_name",
        "measure_type",
        "period_basis",
        "scope",
    ]
    terms = {
        normalize(row.get(field)) for field in fields if row.get(field) is not None
    }
    basis = normalize(row.get("period_basis"))
    if basis in {"ytd", "yeartodate"}:
        terms.update(
            {
                normalize("本年累计"),
                normalize("截至当期"),
                normalize("本年累计/截至当期"),
            }
        )
    if basis == "quarterend":
        terms.add(normalize("季度"))
    if basis == "pointintime":
        terms.add(normalize("截至当期"))
    if normalize(row.get("region_name")) == normalize("全国"):
        terms.add(normalize("全国合计"))
    return {term for term in terms if term}


def term_score(row: dict[str, Any], term: str | None) -> int:
    if not term:
        return 1
    if re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", str(term)):
        return 100 if normalize(row.get("period_end")) == normalize(term) else 0
    target = normalize(term)
    if target == normalize("合计"):
        product = normalize(row.get("product_line_name"))
        metric = normalize(row.get("metric_name"))
        return (
            100
            if product == target or (metric.endswith(target) and metric != target)
            else 0
        )
    if target == normalize("全国合计") and normalize(
        row.get("region_name")
    ) == normalize("全国"):
        return 100
    if target == normalize("截至当期-账面余额"):
        return 100 if normalize(row.get("measure_type")) == normalize("balance") else 0
    if target in {normalize("年-季度"), normalize("季度"), normalize("季度-季度")}:
        return (
            90 if normalize(row.get("period_basis")) == normalize("quarter_end") else 0
        )

    candidates = row_terms(row)
    if target in candidates:
        return 100
    component_score = 0
    for candidate in candidates:
        if len(target) < 2 or len(candidate) < 2:
            continue
        if target in candidate or candidate in target:
            component_score += 20 + min(len(target), len(candidate), 20)
            continue
        overlap = _character_ngrams(target) & _character_ngrams(candidate)
        if overlap:
            component_score += min(len(overlap) * 3, 12)
    return min(component_score, 99)


def sheet_matches(row: dict[str, Any], sheet_name: str | None) -> bool:
    if not sheet_name:
        return True
    expected = normalize(sheet_name)
    actual = normalize(row.get("source_sheet"))
    return expected == actual or expected in actual or actual in expected


def excel_cell_key(cell: Any) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", str(cell or ""))
    if not match:
        return (10**9, 10**9)
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + ord(char) - 64
    return int(match.group(2)), column


def evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": format_decimal(row.get("value")),
        "value_storage": format_decimal(row.get("value_storage"))
        if row.get("value_storage") is not None
        else None,
        "value_display": row.get("value_display"),
        "unit": row.get("unit"),
        "period_end": str(row.get("period_end") or ""),
        "period_basis": row.get("period_basis"),
        "entity": row.get("entity_name"),
        "entity_type": row.get("entity_type"),
        "region": row.get("region_name"),
        "metric": row.get("metric_name"),
        "product_line": row.get("product_line_name"),
        "measure_type": row.get("measure_type"),
        "scope": row.get("scope"),
        "scope_version": row.get("scope_version"),
        "statistical_scope_version": row.get("statistical_scope_version"),
        "accounting_basis_version": row.get("accounting_basis_version"),
        "comparability_flag": row.get("comparability_flag"),
        "source_sheet": row.get("source_sheet"),
        "source_cell": row.get("source_cell"),
        "quality_flags": row.get("quality_flags"),
        "footnotes": row.get("footnotes"),
        "source_quality_status": row.get("source_quality_status")
        or row.get("quality_status"),
    }


NUMERIC_BLOCKING_QUALITY = {"FORMULA_CACHE_MISSING", "FORMULA_ERROR"}
QUALITY_DISCLOSURE = {
    "CHECK_WARNING": "源表存在校验提示",
    "COMPARABILITY_CAVEAT": "源表存在口径可比性提示",
    "CUMULATIVE_DECREASE": "累计值存在下降，需注意统计口径",
}


def source_quality(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("source_quality_status") or row.get("quality_status") or "CLEAN"),
        str(row.get("source_quality_detail") or row.get("quality_detail") or ""),
    )


def validate_numeric_quality(rows: list[dict[str, Any]], intent: str) -> str | None:
    if not rows:
        return None
    status, detail = source_quality(rows[0])
    if status in NUMERIC_BLOCKING_QUALITY:
        raise QuestionError(f"来源公式缓存缺失或有错误，不能作为数字证据：{status}")
    if intent == "delta" and status == "COMPARABILITY_CAVEAT":
        raise QuestionError("来源存在口径可比性提示，禁止直接计算跨期变化")
    if intent == "delta":
        for field in (
            "scope_version",
            "statistical_scope_version",
            "accounting_basis_version",
        ):
            values = {
                str(row.get(field) or "") for row in rows if row.get(field) is not None
            }
            if len(values) > 1:
                raise QuestionError(f"两期数据的{field}不一致，禁止直接计算变化")
    if status in QUALITY_DISCLOSURE:
        return f"来源质量提示（{status}）：{detail or QUALITY_DISCLOSURE[status]}"
    return None


def text_similarity_score(question: str, row: dict[str, Any]) -> float:
    target = normalize(str(row.get("row_text") or ""))
    query = normalize(str(question or ""))
    if not target:
        return -1.0
    score = SequenceMatcher(None, query, target).ratio()
    if target and (target in query or query in target):
        score += 0.5
    focus = normalize(str(row.get("_focus") or ""))
    if focus and focus in target:
        score += 1.0
    try:
        score += min(int(row.get("nonempty_cell_count") or 0), 30) / 1000
    except (TypeError, ValueError):
        pass
    return score


def _schedule_frequency_from_question(question: str) -> str:
    if re.search(r"(?:第?[一二三四1234]季度|季后|季度)", question):
        return "季"
    if re.search(r"(?:\d{4}年\d{1,2}月|月后|月度|月份)", question):
        return "月"
    if re.search(r"(?:年度|年后)", question):
        return "年"
    return ""


def _release_schedule_score(question: str, row: dict[str, Any]) -> float:
    target = normalize(question)
    score = _dictionary_relevance(question, row)
    frequency = _schedule_frequency_from_question(question)
    row_frequency = normalize(row.get("frequency"))
    if frequency:
        score += 40.0 if normalize(frequency) == row_frequency else -40.0
    year_match = re.search(r"(20\d{2})年", question)
    if year_match and row.get("release_year") not in (None, ""):
        score += 20.0 if str(row.get("release_year")) == year_match.group(1) else -20.0
    indicator_text = " ".join(
        str(row.get(key) or "")
        for key in ("indicator_names", "institution_scope", "data_scope")
    )
    score += 3.0 * len(_character_ngrams(target) & _character_ngrams(indicator_text))
    return score


def dictionary_answer(rows: list[dict[str, Any]], plan: QueryPlan) -> dict[str, Any]:
    if plan.operation == "metric_definition":
        scored = sorted(
            rows,
            key=lambda row: (
                normalize(row.get("metric_name")) == normalize(plan.focus),
                normalize(plan.focus) in normalize(row.get("metric_name")),
                row.get("release_year") or 0,
            ),
        )
        field = "definition"
    elif plan.operation == "institution_scope":
        scored = sorted(
            rows,
            key=lambda row: (
                normalize(row.get("institution_type")) == normalize(plan.focus),
                normalize(plan.focus) in normalize(row.get("institution_type")),
                row.get("release_year") or 0,
            ),
        )
        field = "scope_definition"
    else:
        question = str(plan.context or plan.focus or "")
        scored = sorted(rows, key=lambda row: _release_schedule_score(question, row))
        field = "release_timing"
    if not scored:
        raise QuestionError("没有找到词典记录")
    selected = scored[-1]
    answer = str(selected.get(field) or "").strip()
    if not answer:
        raise QuestionError("词典记录缺少答案字段")
    return selected, answer


def document_answer(rows: list[dict[str, Any]], plan: QueryPlan) -> dict[str, Any]:
    if plan.operation == "reference_header":
        if not plan.focus:
            raise QuestionError("名单题缺少机构名称")
        target = normalize(plan.focus)
        matches = [
            row for row in rows if target and target in normalize(row.get("row_text"))
        ]
        if not matches:
            raise QuestionError("名单中没有找到指定机构")
        matches.sort(key=lambda row: int(row.get("row_number") or 0))
        item = matches[0]
        sheet = str(item.get("source_sheet") or "")
        headers = [
            row
            for row in rows
            if str(row.get("source_sheet") or "") == sheet
            and int(row.get("row_number") or 0) < int(item.get("row_number") or 0)
            and str(row.get("row_text") or "").strip()
            and not re.match(r"^\s*\d", str(row.get("row_text") or ""))
        ]
        if not headers:
            raise QuestionError("名单机构行上方没有分类标题")
        selected = max(headers, key=lambda row: int(row.get("row_number") or 0))
        answer = str(selected.get("row_text") or "").strip()
        return selected, answer

    if plan.operation == "rule_lookup":
        return rule_answer(rows, plan)

    question = plan.context or ""
    structure_signals = (
        "表头" in question
        or "字段" in question
        or "包含哪些" in question
        or "区分了哪些" in question
        or "按哪些险种拆分" in question
        or "统计年度" in question
        or "统计范围" in question
        or "岗位签章" in question
    )
    if structure_signals:
        return structure_answer(rows, plan)

    enriched = [dict(row, _focus=plan.focus) for row in rows]
    selected = max(
        enriched, key=lambda row: text_similarity_score(plan.context or "", row)
    )
    answer = str(selected.get("row_text") or "").strip()
    if not answer:
        raise QuestionError("命中的文本行为空")
    return selected, answer


def grounded_document_evidence(
    rows: list[dict[str, Any]], plan: QueryPlan, limit: int = 5
) -> list[dict[str, Any]]:
    """Retrieve several relevant rows for evidence-grounded open answering."""
    candidates = [row for row in rows if str(row.get("row_text") or "").strip()]
    if not candidates:
        raise QuestionError("指定文件没有可用于开放问答的文本证据")
    source_title = normalize(plan.source_title)
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for row in candidates:
        row_text = str(row.get("row_text") or "").strip()
        score = text_similarity_score(plan.context or "", row)
        if source_title and normalize(row_text) == source_title:
            score -= 1.0
        scored.append((score, -int(row.get("row_number") or 0), row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, row in scored:
        text = normalize(row.get("row_text"))
        if not text or text in seen:
            continue
        seen.add(text)
        selected.append(row)
        if len(selected) >= max(1, limit):
            break
    return selected


def grounded_fact_evidence(
    rows: list[dict[str, Any]], plan: QueryPlan, limit: int = 12
) -> list[dict[str, Any]]:
    """Rank structured facts for an open question over an Excel workbook."""
    if not rows:
        raise QuestionError("指定文件没有可用于开放问答的结构化事实证据")
    question = normalize(plan.context or "")
    scored: list[tuple[float, tuple[int, int], dict[str, Any]]] = []
    for row in rows:
        terms = [
            row.get("metric_name"),
            row.get("entity_name"),
            row.get("region_name"),
            row.get("product_line_name"),
            row.get("measure_type"),
            row.get("scope"),
            row.get("period_basis"),
            row.get("unit"),
        ]
        score = 0.0
        for term in terms:
            normalized = normalize(term)
            if normalized and normalized in question:
                score += min(len(normalized), 12)
        period = str(row.get("period_end") or "")
        if period[:4] and period[:4] in question:
            score += 3.0
        if period[:7] and period[:7] in question:
            score += 5.0
        scored.append((score, excel_cell_key(row.get("source_cell")), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[: max(1, limit)]]


def grounded_retrieval_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Expose either a document row or structured fact as model evidence."""
    if row.get("evidence_kind"):
        return dict(row)
    row_text = str(row.get("row_text") or "").strip()
    if row_text:
        return {
            "source_title": row.get("source_title"),
            "source_sheet": row.get("source_sheet"),
            "source_locator": row.get("source_cell") or row.get("source_range"),
            "row_number": row.get("row_number"),
            "answer": row_text,
        }
    return {"source_title": row.get("source_title"), **evidence(row)}


def dictionary_retrieval_evidence(
    row: dict[str, Any], dictionary_kind: str
) -> dict[str, Any]:
    """Expose a compact dictionary row without leaking storage-only columns."""
    output = {
        "evidence_kind": dictionary_kind,
        "source_title": row.get("source_title"),
        "source_sheet": row.get("source_sheet"),
        "source_cell": row.get("source_cell"),
    }
    for column_name in (
        "metric_name",
        "definition",
        "unit",
        "measure_type",
        "institution_type",
        "scope_definition",
        "release_year",
        "frequency",
        "release_timing",
        "institution_scope",
        "data_scope",
        "indicator_names",
        "notes",
    ):
        if row.get(column_name) not in (None, ""):
            output[column_name] = row[column_name]
    return output


def _dictionary_relevance(question: str, row: dict[str, Any]) -> float:
    target = normalize(question)
    score = 0.0
    for value in row.values():
        if value in (None, ""):
            continue
        candidate = normalize(value)
        if not candidate:
            continue
        if candidate in target:
            score += min(len(candidate), 20)
        elif target in candidate:
            score += min(len(target), 20)
        else:
            score = max(score, SequenceMatcher(None, target, candidate).ratio())
    return score


def grounded_source_evidence(
    repository: Any,
    source: dict[str, Any],
    plan: QueryPlan,
) -> list[dict[str, Any]]:
    """Retrieve positive facts and exhaustive source-coverage evidence together.

    Top-k fact retrieval cannot prove that a requested metric or dimension is
    absent.  A compact capability profile is therefore always included when the
    repository can provide one.  Documents, facts and dictionaries supplement
    that profile instead of competing with one another.
    """
    file_name = str(source.get("file_name") or "")
    selected: list[dict[str, Any]] = []

    capability_loader = getattr(repository, "source_capabilities", None)
    if callable(capability_loader):
        selected.extend(capability_loader(file_name, plan.sheet_name) or [])

    document_rows = repository.document_rows(file_name, plan.sheet_name)
    if document_rows:
        selected.extend(
            grounded_retrieval_evidence(row)
            for row in grounded_document_evidence(document_rows, plan)
        )

    fact_rows = repository.facts_for_source(file_name, plan.sheet_name)
    if fact_rows:
        selected.extend(
            grounded_retrieval_evidence(row)
            for row in grounded_fact_evidence(fact_rows, plan)
        )

    dictionary_specs = (
        ("metric_definition", "metric_definitions"),
        ("institution_scope", "institution_scopes"),
        ("release_schedule", "release_schedules"),
    )
    dictionary_candidates: list[tuple[float, dict[str, Any]]] = []
    for evidence_kind, loader_name in dictionary_specs:
        loader = getattr(repository, loader_name, None)
        if not callable(loader):
            continue
        for row in loader(file_name) or []:
            item = dictionary_retrieval_evidence(row, evidence_kind)
            dictionary_candidates.append(
                (_dictionary_relevance(plan.context or "", item), item)
            )
    dictionary_candidates.sort(key=lambda item: item[0], reverse=True)
    selected.extend(item for _, item in dictionary_candidates[:12])

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in selected:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    if not unique:
        raise QuestionError("指定文件没有可用于证据边界判断的内容")
    return unique


def structure_answer(rows: list[dict[str, Any]], plan: QueryPlan) -> dict[str, Any]:
    """Rank table metadata rows by their structure, not by the question/title text."""
    question = normalize(plan.context or "")
    candidates = [row for row in rows if str(row.get("row_text") or "").strip()]
    if not candidates:
        raise QuestionError("没有可用的结构行")

    if "岗位签章" in question:
        role_matches: list[tuple[int, int, dict[str, Any], re.Match[str]]] = []
        for row in candidates:
            match = re.search(
                r"(总精算师|财务负责人|填\s*表\s*人)", str(row.get("row_text") or "")
            )
            if match:
                role_rank = 0 if match.group(1) == "总精算师" else 1
                role_matches.append(
                    (role_rank, int(row.get("row_number") or 0), row, match)
                )
        if role_matches:
            _, _, row, match = min(role_matches, key=lambda item: item[:2])
            return row, re.sub(r"\s+", "", match.group(1))

    if "统计年度" in question or "统计范围" in question:
        note_rows = [
            row
            for row in candidates
            if normalize(str(row.get("row_text") or "")).startswith("备注")
            and (
                "统计年度" in str(row.get("row_text") or "")
                or "统计范围" in str(row.get("row_text") or "")
            )
        ]
        if note_rows:
            return note_rows[0], str(note_rows[0].get("row_text") or "").strip()

    def cells(row: dict[str, Any]) -> list[str]:
        return [cell.strip() for cell in str(row.get("row_text") or "").split(" | ")]

    def numeric_like(cell: str) -> bool:
        return bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?%?", cell.replace(",", "")))

    insurance_terms = {
        "机动车辆保险",
        "企业财产保险",
        "家庭财产保险",
        "工程保险",
        "责任保险",
        "信用保险",
        "保证保险",
        "船舶保险",
        "货物运输保险",
        "特殊风险保险",
        "农业保险",
        "健康险",
        "意外伤害保险",
        "其他险",
    }

    if "表头" in question:
        qualifier_match = re.search(
            r"([\u4e00-\u9fffA-Za-z0-9]{2,})和([\u4e00-\u9fffA-Za-z0-9]{2,})的",
            str(plan.context or ""),
        )
        if qualifier_match:
            qualifiers = [qualifier_match.group(1), qualifier_match.group(2)]
            grouping_rows = [
                row
                for row in candidates
                if 2 <= len([cell for cell in cells(row) if cell]) <= 5
                and all(
                    any(normalize(cell) == normalize(term) for cell in cells(row))
                    for term in qualifiers
                )
            ]
            for grouping_row in sorted(
                grouping_rows, key=lambda row: int(row.get("row_number") or 0)
            ):
                next_headers = [
                    row
                    for row in candidates
                    if int(row.get("row_number") or 0)
                    > int(grouping_row.get("row_number") or 0)
                    and len([cell for cell in cells(row) if cell]) >= 3
                ]
                if next_headers:
                    selected = min(
                        next_headers, key=lambda row: int(row.get("row_number") or 0)
                    )
                    return selected, str(selected.get("row_text") or "").strip()

    if "基础字段" in question:
        base_rows = []
        for row in candidates:
            row_cells = cells(row)
            nonempty = [cell for cell in row_cells if cell]
            hits = sum(
                normalize(cell) and normalize(cell) in question for cell in nonempty
            )
            administrative = sum(
                any(
                    token in cell
                    for token in ("填报单位", "填报日期", "联系人", "联系电话")
                )
                for cell in row_cells
            )
            subsection = sum(
                normalize(cell).startswith(normalize("其中："))
                or normalize(cell).startswith(normalize("其中:"))
                for cell in row_cells
            )
            if len(nonempty) >= 3 and hits and not administrative and not subsection:
                base_rows.append(row)
        if base_rows:
            selected = min(base_rows, key=lambda row: int(row.get("row_number") or 0))
            return selected, str(selected.get("row_text") or "").strip()

    scored: list[tuple[float, int, dict[str, Any]]] = []
    for row in candidates:
        row_cells = cells(row)
        row_text = str(row.get("row_text") or "").strip()
        separators = max(len(row_cells) - 1, 0)
        nonempty = [cell for cell in row_cells if cell]
        text_cells = [cell for cell in nonempty if not numeric_like(cell)]
        repeated = len(nonempty) - len({normalize(cell) for cell in nonempty})
        numeric_cells = len(nonempty) - len(text_cells)
        question_hits = sum(
            len(cell) >= 2 and normalize(cell) in question
            for cell in {normalize(cell) for cell in nonempty}
        )

        score = float(min(separators, 12))
        score += min(repeated * 0.8, 4.0)
        if nonempty:
            score += len(text_cells) / len(nonempty)
        score -= min(numeric_cells, 12)
        score += min(question_hits * 3.0, 12.0)
        subsection_cells = sum(
            normalize(cell).startswith(normalize("其中："))
            or normalize(cell).startswith(normalize("其中:"))
            for cell in row_cells
        )
        score -= min(subsection_cells * 2.0, 12.0)
        if any(len(cell) > 160 for cell in nonempty):
            score -= 2.0

        scenario_cells = sum("情景" in cell for cell in row_cells)
        if ("情景" in question or "压力情景" in question) and scenario_cells:
            score += min(scenario_cells * 1.5, 8.0)

        if "按哪些险种拆分" in question or "险种" in question:
            insurance_cells = sum(
                normalize(cell) in {normalize(term) for term in insurance_terms}
                for cell in row_cells
            )
            score += min(insurance_cells * 0.8, 10.0)

        # A work-sheet or report title usually occurs as a single-cell row. Such
        # rows duplicate the question text and otherwise beat the real header.
        title_like_values = [plan.focus, plan.sheet_name, plan.source_title]
        if not separators and any(
            value
            and (
                normalize(row_text) == normalize(value)
                or normalize(value) in normalize(row_text)
            )
            for value in title_like_values
        ):
            score -= 12.0

        # Generic similarity remains useful for a phrase such as "巨灾超赔再保
        # 条件", but its weight is deliberately small after structural signals.
        score += text_similarity_score(question, dict(row, _focus=plan.focus)) * 0.2
        scored.append((score, int(row.get("row_number") or 0), row))

    selected = min(scored, key=lambda item: (-item[0], item[1]))[2]
    answer = str(selected.get("row_text") or "").strip()
    if not answer:
        raise QuestionError("结构行为空")
    return selected, answer


def rule_answer(rows: list[dict[str, Any]], plan: QueryPlan) -> dict[str, Any]:
    question = plan.context or ""
    focus = plan.focus or ""
    data_rows = [row for row in rows if str(row.get("row_text") or "").strip()]
    if not data_rows:
        raise QuestionError("规则表没有可用行")

    def cells(row: dict[str, Any]) -> list[str]:
        return [cell.strip() for cell in str(row.get("row_text") or "").split(" | ")]

    def is_rule_row(row: dict[str, Any]) -> bool:
        first_cell = cells(row)[0] if cells(row) else ""
        return bool(re.fullmatch(r"\d+", first_cell)) or normalize(
            first_cell
        ) == normalize("合计")

    real_rule_rows = [row for row in data_rows if is_rule_row(row)]
    if real_rule_rows:
        data_rows = real_rule_rows

    if "最低总分和最高总分" in question:
        candidates = [
            row for row in data_rows if "合计" in normalize(row.get("row_text"))
        ]
    elif focus:
        candidates = [
            row
            for row in data_rows
            if normalize(focus) in normalize(row.get("row_text"))
        ]
    else:
        candidates = []

    if not candidates:
        candidates = data_rows

    def row_rank(row: dict[str, Any]) -> tuple[float, int]:
        score = text_similarity_score(question, dict(row, _focus=focus))
        if focus and focus in normalize(row.get("row_text")):
            score += 2
        if focus and any(normalize(cell) == normalize(focus) for cell in cells(row)):
            score += 3
        return score, int(row.get("row_number") or 0)

    selected = max(candidates, key=row_rank)

    if "指什么" in question and focus:
        match = re.search(
            r"[“\"]" + re.escape(focus) + r"[”\"]\s*[：:]\s*指([^，。；\n]+)",
            str(selected.get("row_text") or ""),
        )
        if match:
            return selected, match.group(1).strip()

    if "权重达到多少" in question:
        match = re.search(
            r"\d+(?:\.\d+)?%（含）以上", str(selected.get("row_text") or "")
        )
        if match:
            return selected, match.group(0)

    score_cells: list[int] = []
    score_cells_source = cells(selected)[1:] if real_rule_rows else cells(selected)
    for cell in score_cells_source:
        match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)分?\s*", cell)
        if match:
            score_cells.append(int(Decimal(match.group(1))))
    if "最低总分和最高总分" in question and score_cells:
        return selected, f"{min(score_cells)}分、{max(score_cells)}分"
    if score_cells and ("最高分" in question or "最低总分和最高总分" in question):
        return selected, f"{max(score_cells)}分"
    if score_cells and "最低分" in question:
        return selected, f"{min(score_cells)}分"
    raise QuestionError("规则表中没有找到对应答案")


def resolve_one(
    rows: list[dict[str, Any]],
    labels: list[str | None],
    sheet_name: str | None,
    strict: bool,
) -> dict[str, Any]:
    scored: list[tuple[int, dict[str, Any]]] = []
    active_labels = [label for label in labels if label]
    for row in rows:
        if not sheet_matches(row, sheet_name):
            continue
        scores = [term_score(row, label) for label in active_labels]
        if scores and min(scores) <= 0:
            continue
        score = sum(scores)
        if normalize(row.get("product_line_name")) == normalize("合计"):
            score += 5
        scored.append((score, row))
    if not scored:
        raise QuestionError(
            f"没有找到满足条件的数据：{[label for label in labels if label]}"
        )
    best_score = max(score for score, _ in scored)
    matches = [row for score, row in scored if score == best_score]
    distinct = {
        (
            str(row.get("value")),
            str(row.get("period_end")),
            row.get("metric_code"),
            row.get("entity_code"),
            row.get("region_code"),
            row.get("product_line"),
        )
        for row in matches
    }
    if len(distinct) > 1 and strict:
        periods = sorted({str(row.get("period_end") or "") for row in matches})
        scopes = sorted(
            {str(row.get("scope") or "") for row in matches if row.get("scope")}
        )
        bases = sorted(
            {
                str(row.get("period_basis") or "")
                for row in matches
                if row.get("period_basis")
            }
        )
        entities = sorted(
            {
                str(row.get("entity_name") or row.get("region_name") or "")
                for row in matches
                if row.get("entity_name") or row.get("region_name")
            }
        )
        questions: list[str] = []
        if len(periods) > 1:
            questions.append(f"您要查询哪个统计期间：{'、'.join(periods[:6])}？")
        if len(entities) > 1:
            questions.append(f"您要查询哪个机构或地区：{'、'.join(entities[:6])}？")
        if len(scopes) > 1:
            questions.append(f"您要采用哪种统计范围：{'、'.join(scopes[:6])}？")
        if len(bases) > 1:
            questions.append(f"您要采用哪种期间口径：{'、'.join(bases[:6])}？")
        if not questions:
            questions.append("请补充更具体的期间、机构范围或统计口径。")
        raise ClarificationQuestionError(
            "当前条件命中多条不同的金融数据。",
            questions=questions,
            candidates=[
                {
                    "period_end": row.get("period_end"),
                    "entity": row.get("entity_name") or row.get("region_name"),
                    "metric": row.get("metric_name"),
                    "scope": row.get("scope"),
                    "period_basis": row.get("period_basis"),
                }
                for row in matches[:5]
            ],
        )
    matches.sort(
        key=lambda row: (
            str(row.get("source_sheet") or ""),
            excel_cell_key(row.get("source_cell")),
        )
    )
    return matches[0]


def ranked_candidates(
    rows: list[dict[str, Any]], labels: list[str | None], sheet_name: str | None
) -> list[dict[str, Any]]:
    """Return every equally best fact row instead of silently choosing the first one."""
    scored: list[tuple[int, dict[str, Any]]] = []
    active_labels = [label for label in labels if label]
    for row in rows:
        if not sheet_matches(row, sheet_name):
            continue
        scores = [term_score(row, label) for label in active_labels]
        if scores and min(scores) <= 0:
            continue
        score = sum(scores)
        if normalize(row.get("product_line_name")) == normalize("合计"):
            score += 5
        scored.append((score, row))
    if not scored:
        raise QuestionError(f"没有找到满足条件的数据：{active_labels}")
    best_score = max(score for score, _ in scored)
    matches = [row for score, row in scored if score == best_score]
    matches.sort(
        key=lambda row: (
            str(row.get("source_sheet") or ""),
            excel_cell_key(row.get("source_cell")),
        )
    )
    return matches


def numeric_options(options: dict[str, str]) -> dict[str, Decimal]:
    parsed: dict[str, Decimal] = {}
    for key, raw_value in options.items():
        text = str(raw_value).strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            value = Decimal(text)
        except InvalidOperation:
            continue
        if value.is_finite():
            parsed[key] = value
    return parsed


def matching_numeric_choices(
    value: Any, options: dict[str, str], tolerance: Decimal = Decimal("0.011")
) -> list[str]:
    number = as_decimal(value)
    return [
        key
        for key, option_value in numeric_options(options).items()
        if abs(number - option_value) <= tolerance
    ]


def lookup_with_options(
    rows: list[dict[str, Any]], plan: QueryPlan
) -> tuple[dict[str, Any], str, str]:
    candidates = ranked_candidates(rows, [plan.focus, plan.context], plan.sheet_name)
    matched: list[tuple[str, dict[str, Any]]] = []
    for row in candidates:
        matched.extend(
            (choice, row)
            for choice in matching_numeric_choices(row.get("value"), plan.options)
        )
    choices = {choice for choice, _ in matched}
    if len(choices) != 1:
        sample = [evidence(row) for row in candidates[:8]]
        raise AmbiguousQuestionError(
            f"候选数据无法被选项唯一消歧；匹配选项={sorted(choices)}，候选={sample}"
        )
    choice = next(iter(choices))
    selected = next(row for matched_choice, row in matched if matched_choice == choice)
    return selected, choice, "option_unique"


def exact_field_match(row: dict[str, Any], field: str, label: str | None) -> bool:
    return bool(label) and normalize(row.get(field)) == normalize(label)


def compare_with_options(
    rows: list[dict[str, Any]], plan: QueryPlan
) -> tuple[str, dict[str, dict[str, Any]], str]:
    context_rows = [
        row
        for row in rows
        if sheet_matches(row, plan.sheet_name) and term_score(row, plan.context) > 0
    ]
    if not context_rows:
        raise QuestionError(f"没有找到比较口径：{plan.context}")

    compare_entities = (
        sum(
            any(
                exact_field_match(row, field, label)
                for row in context_rows
                for field in ("entity_name", "region_name")
            )
            for label in plan.options.values()
        )
        >= 2
    )
    instances: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in context_rows:
        instance_id = (
            str(row.get("source_sheet") or ""),
            str(row.get("period_end") or ""),
            str(row.get("unit") or ""),
        )
        if not compare_entities:
            instance_id += (
                row.get("entity_code") or row.get("entity_name"),
                row.get("region_code") or row.get("region_name"),
            )
        instances.setdefault(instance_id, []).append(row)

    evaluated: list[dict[str, Any]] = []
    for instance_key, group_rows in instances.items():
        selected: dict[str, dict[str, Any]] = {}
        for choice, label in plan.options.items():
            matches = [(term_score(row, label), row) for row in group_rows]
            matches = [(score, row) for score, row in matches if score > 0]
            if not matches:
                continue
            best_score = max(score for score, _ in matches)
            best_rows = [row for score, row in matches if score == best_score]
            totals = [
                row
                for row in best_rows
                if normalize(row.get("product_line_name")) == normalize("合计")
            ]
            candidates = totals or best_rows
            selected[choice] = (max if plan.operation == "argmax" else min)(
                candidates, key=lambda row: as_decimal(row.get("value"))
            )
        if len(selected) != len(plan.options):
            continue
        values = {
            choice: as_decimal(row.get("value")) for choice, row in selected.items()
        }
        winner = (max if plan.operation == "argmax" else min)(values, key=values.get)
        earliest = min(
            excel_cell_key(row.get("source_cell")) for row in selected.values()
        )
        evaluated.append(
            {
                "instance": instance_key,
                "winner": winner,
                "selected": selected,
                "earliest": earliest,
            }
        )
    if not evaluated:
        raise AmbiguousQuestionError("无法在同期、同比较范围和同单位下取齐全部选项")

    winners = {item["winner"] for item in evaluated}
    if len(winners) != 1:
        raise AmbiguousQuestionError("不同合理期间得出不同选项，不能猜测期间")
    chosen = min(evaluated, key=lambda item: item["earliest"])
    resolution = (
        "option_period_consensus" if len(evaluated) > 1 else "option_literal_values"
    )
    return chosen["winner"], chosen["selected"], resolution


OPEN_RANK_DIMENSIONS: dict[str, tuple[str, str]] = {
    "region": ("region_code", "region_name"),
    "entity": ("entity_code", "entity_name"),
    "product_line": ("product_line", "product_line_name"),
    "metric": ("metric_code", "metric_name"),
}


def dimension_identity(row: dict[str, Any], group_by: str) -> tuple[str, str] | None:
    code_field, name_field = OPEN_RANK_DIMENSIONS[group_by]
    name = str(row.get(name_field) or "").strip()
    code = str(row.get(code_field) or name).strip()
    if not name:
        return None
    normalized = normalize(name)
    aggregate_labels = {
        "region": {
            normalize("全国"),
            normalize("全国合计"),
            normalize("合计"),
            normalize("公司本级"),
        },
        "entity": {normalize("全国保险业汇总"), normalize("合计"), normalize("总计")},
        "product_line": {normalize("合计"), normalize("总计")},
        "metric": set(),
    }[group_by]
    if normalized in aggregate_labels:
        return None
    return code, name


def open_rank_instance_key(row: dict[str, Any], group_by: str) -> tuple[Any, ...]:
    fields = [
        "source_sheet",
        "period_end",
        "unit",
        "period_basis",
        "scope",
        "scope_version",
        "statistical_scope_version",
        "accounting_basis_version",
        "measure_type",
        "metric_code",
        "metric_name",
        "entity_code",
        "entity_name",
        "region_code",
        "region_name",
        "product_line",
        "product_line_name",
    ]
    excluded = set(OPEN_RANK_DIMENSIONS[group_by])
    return tuple(row.get(field) for field in fields if field not in excluded)


def open_rank_clarification(
    instances: list[dict[str, Any]],
) -> ClarificationQuestionError:
    periods = sorted(
        {str(item["rows"][0].get("period_end") or "") for item in instances}
    )
    sheets = sorted(
        {str(item["rows"][0].get("source_sheet") or "") for item in instances}
    )
    scopes = sorted({str(item["rows"][0].get("scope") or "") for item in instances})
    questions: list[str] = []
    if len(periods) > 1:
        questions.append(f"您要比较哪个统计期间：{'、'.join(periods[:6])}？")
    if len(scopes) > 1:
        questions.append(f"您要采用哪种统计口径：{'、'.join(filter(None, scopes))}？")
    if len(sheets) > 1:
        questions.append("当前指标存在多个业务数据表，您能补充机构范围或业务类型吗？")
    if not questions:
        questions.append("当前条件对应多组可比较数据，请补充期间、机构范围或统计口径。")
    return ClarificationQuestionError(
        "开放式排名命中多组不同口径的数据。",
        questions=questions,
        candidates=[
            {
                "period_end": item["rows"][0].get("period_end"),
                "scope": item["rows"][0].get("scope"),
                "source_sheet": item["rows"][0].get("source_sheet"),
                "winner": item["winner"],
            }
            for item in instances[:8]
        ],
    )


def compare_open_dimension(
    rows: list[dict[str, Any]], plan: QueryPlan
) -> tuple[str, list[dict[str, Any]], str]:
    group_by = plan.group_by or ""
    if group_by not in OPEN_RANK_DIMENSIONS:
        raise QuestionError("开放式排名缺少有效的比较维度")
    metric_label = plan.focus or plan.context
    relevant = [
        row
        for row in rows
        if sheet_matches(row, plan.sheet_name)
        and term_score(row, metric_label) > 0
        and dimension_identity(row, group_by) is not None
    ]
    if not relevant:
        raise QuestionError(f"没有找到可按{group_by}枚举的比较数据：{metric_label}")

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in relevant:
        grouped.setdefault(open_rank_instance_key(row, group_by), []).append(row)

    evaluated: list[dict[str, Any]] = []
    for instance_rows in grouped.values():
        by_dimension: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in instance_rows:
            identity = dimension_identity(row, group_by)
            if identity:
                by_dimension.setdefault(identity, []).append(row)
        selected: list[dict[str, Any]] = []
        for (code, name), candidate_rows in by_dimension.items():
            values = {as_decimal(row.get("value")) for row in candidate_rows}
            if len(values) != 1:
                continue
            chosen = min(
                candidate_rows, key=lambda row: excel_cell_key(row.get("source_cell"))
            )
            chosen = {**chosen, "_group_code": code, "_group_name": name}
            selected.append(chosen)
        if len(selected) < 2:
            continue
        units = {str(row.get("unit") or "") for row in selected}
        if len(units) != 1:
            continue
        selected.sort(
            key=lambda row: as_decimal(row.get("value")),
            reverse=plan.operation == "argmax",
        )
        evaluated.append({"winner": selected[0]["_group_name"], "rows": selected})

    if not evaluated:
        raise ClarificationQuestionError(
            "没有找到至少两个同期间、同单位、同口径的可比较对象。",
            questions=["请补充明确的统计期间、指标口径或业务范围。"],
        )
    if len(evaluated) != 1:
        raise open_rank_clarification(evaluated)
    chosen = evaluated[0]
    return str(chosen["winner"]), chosen["rows"], "open_dimension_rank"


def is_period_term(term: str | None) -> bool:
    target = normalize(term)
    return any(
        token in target
        for token in (normalize("季度"), normalize("本年累计"), normalize("截至当期"))
    )


def is_quarter_endpoint_delta(plan: QueryPlan) -> bool:
    """识别旧题库中用模糊口径表示一季度到四季度变化的题目。"""
    return (
        plan.intent == "delta"
        and normalize(plan.from_term) == normalize("年-季度")
        and normalize(plan.to_term) in {normalize("季度"), normalize("季度-季度")}
    )


def quarter_endpoint_explanation(plan: QueryPlan) -> str | None:
    if not is_quarter_endpoint_delta(plan):
        return None
    return (
        f"题目中的“{plan.from_term}”自动按第一季度处理，"
        f"“{plan.to_term}”自动按第四季度处理；变化值按第四季度减第一季度计算。"
    )


def same_dimension(left: dict[str, Any], right: dict[str, Any], field: str) -> bool:
    left_value, right_value = left.get(field), right.get(field)
    return not left_value or not right_value or left_value == right_value


def term_matches_any_field(
    rows: list[dict[str, Any]], term: str | None, fields: tuple[str, ...]
) -> bool:
    return any(exact_field_match(row, field, term) for row in rows for field in fields)


def delta_pairs(
    rows: list[dict[str, Any]], plan: QueryPlan
) -> list[tuple[dict[str, Any], dict[str, Any], Decimal]]:
    starts = ranked_candidates(
        rows, [plan.focus, plan.context, plan.from_term], plan.sheet_name
    )
    ends = ranked_candidates(
        rows, [plan.focus, plan.context, plan.to_term], plan.sheet_name
    )
    temporal = is_period_term(plan.from_term) or is_period_term(plan.to_term)
    entity_changes = term_matches_any_field(
        rows, plan.from_term, ("entity_name", "region_name")
    ) and term_matches_any_field(rows, plan.to_term, ("entity_name", "region_name"))
    metric_changes = term_matches_any_field(
        rows, plan.from_term, ("metric_name", "product_line_name")
    ) and term_matches_any_field(
        rows, plan.to_term, ("metric_name", "product_line_name")
    )
    fixed_dimensions = ["metric_code", "entity_code", "region_code", "product_line"]
    if entity_changes:
        fixed_dimensions = [
            field
            for field in fixed_dimensions
            if field not in {"entity_code", "region_code"}
        ]
    if metric_changes:
        fixed_dimensions = [
            field
            for field in fixed_dimensions
            if field not in {"metric_code", "product_line"}
        ]
    pairs: list[tuple[dict[str, Any], dict[str, Any], Decimal]] = []
    for start in starts:
        for end in ends:
            if start.get("source_cell") == end.get("source_cell"):
                continue
            if start.get("unit") != end.get("unit"):
                continue
            if not all(same_dimension(start, end, field) for field in fixed_dimensions):
                continue
            same_period = str(start.get("period_end") or "") == str(
                end.get("period_end") or ""
            )
            if temporal and same_period:
                continue
            if temporal and str(start.get("period_end") or "") > str(
                end.get("period_end") or ""
            ):
                continue
            if not temporal and not same_period:
                continue
            pairs.append(
                (
                    start,
                    end,
                    as_decimal(end.get("value")) - as_decimal(start.get("value")),
                )
            )
    if is_quarter_endpoint_delta(plan) and pairs:
        first_period = min(str(start.get("period_end") or "") for start, _, _ in pairs)
        fourth_period = max(str(end.get("period_end") or "") for _, end, _ in pairs)
        pairs = [
            pair
            for pair in pairs
            if str(pair[0].get("period_end") or "") == first_period
            and str(pair[1].get("period_end") or "") == fourth_period
        ]
    return pairs


def delta_with_options(
    rows: list[dict[str, Any]], plan: QueryPlan
) -> tuple[dict[str, Any], dict[str, Any], Decimal, str, str]:
    matches: list[tuple[str, dict[str, Any], dict[str, Any], Decimal]] = []
    for start, end, delta in delta_pairs(rows, plan):
        for choice in matching_numeric_choices(delta, plan.options):
            matches.append((choice, start, end, delta))
    choices = {choice for choice, _, _, _ in matches}
    if len(choices) != 1:
        raise AmbiguousQuestionError(
            f"同口径组合无法被选项唯一消歧；匹配选项={sorted(choices)}"
        )
    choice = next(iter(choices))
    candidates = [item for item in matches if item[0] == choice]
    candidates.sort(
        key=lambda item: (
            excel_cell_key(item[1].get("source_cell")),
            excel_cell_key(item[2].get("source_cell")),
        )
    )
    _, start, end, delta = candidates[0]
    resolution = (
        "option_quarter_endpoint_auto"
        if is_quarter_endpoint_delta(plan)
        else "option_unique_pair"
    )
    return start, end, delta, choice, resolution


def as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise QuestionError(f"查询结果不是可计算数值：{value}") from exc


def format_decimal(value: Any, places: int | None = None) -> str:
    if value is None:
        return ""
    number = as_decimal(value)
    if places is not None:
        number = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _metric_focus_supported(focus: str | None, row: dict[str, Any]) -> bool:
    """Require the requested operand to match the selected metric itself.

    Region/entity matches are useful for ranking, but they cannot prove that a
    denominator exists.  This guard prevents a nearby fact from silently
    replacing an unavailable financial metric.
    """

    if not focus:
        return True
    target = normalize(focus)
    metric_values = [
        str(row.get(field) or "")
        for field in ("metric_name", "metric_name_raw", "metric_path")
        if row.get(field) not in (None, "")
    ]
    for metric in metric_values:
        normalized_metric = normalize(metric)
        if normalized_metric in target or target in normalized_metric:
            return True
        overlap = _character_ngrams(target) & _character_ngrams(normalized_metric)
        if overlap:
            return True
    return False


def _partial_analysis_result(
    plan: QueryPlan,
    operands: list[tuple[AnalysisStep, dict[str, Any], dict[str, Any]]],
    missing_step: AnalysisStep,
    error: Exception,
) -> AnswerResult:
    source_files = list(
        dict.fromkeys(str(source.get("file_name") or "") for _, source, _ in operands)
    )
    families = list(
        dict.fromkeys(
            str(source.get("dataset_family") or "") for _, source, _ in operands
        )
    )
    return AnswerResult(
        answer="无法完成受控跨表计算",
        choice=None,
        answer_text=f"缺少操作数{missing_step.focus or missing_step.step_id}：{error}",
        source_file=" | ".join(source_files),
        dataset_family=" | ".join(families),
        evidence=[
            {
                "analysis_step": step.step_id,
                "source_title": source.get("source_title"),
                "resolution": "cross_table_operand",
                **evidence(row),
            }
            for step, source, row in operands
        ]
        + [
            {
                "evidence_kind": "missing_operand",
                "analysis_step": missing_step.step_id,
                "requested_operand": missing_step.focus,
                "reason": str(error),
            }
        ],
        plan=plan,
        explanation=str(error),
    )


def execute_cross_table_analysis(
    repository: Any,
    question: str,
    plan: QueryPlan,
    vector_provider: Any | None,
    strict: bool,
) -> AnswerResult:
    """Resolve each operand independently, then apply one whitelisted formula."""
    operands: list[tuple[AnalysisStep, dict[str, Any], dict[str, Any]]] = []
    sources = repository.list_sources()
    for step in plan.analysis_steps:
        step_plan = QueryPlan(
            intent="lookup",
            source_title=step.source_title,
            sheet_name=step.sheet_name,
            focus=step.focus,
            context=step.context,
            from_term=None,
            to_term=None,
            operation="lookup",
            options={},
            filters=dict(step.filters),
            source_filters=dict(step.source_filters),
            answer_mode="hybrid" if not step.source_title else "structured_query",
            planner=plan.planner,
            route="fact_observation",
        )
        try:
            source = resolve_plan_source(
                step.focus or question, step_plan, sources, repository, vector_provider
            )
            resolved_sheet = resolve_plan_sheet(
                step.focus or question,
                step_plan,
                repository,
                str(source.get("file_name") or ""),
                vector_provider,
            )
            if resolved_sheet:
                step_plan.sheet_name = resolved_sheet
            enforce_capability_coverage(repository, source, step_plan)
            rows = repository.facts_for_source(
                str(source.get("file_name") or ""),
                step_plan.sheet_name,
                step_plan.filters,
            )
            if not rows:
                raise CapabilityCoverageError(
                    [f"步骤{step.step_id}没有满足全部条件的事实"]
                )
            selected = resolve_one(
                rows,
                [step.focus, step.context],
                step_plan.sheet_name,
                strict,
            )
            if not _metric_focus_supported(step.focus, selected):
                raise CapabilityCoverageError(
                    [f"步骤{step.step_id}缺少目标指标={step.focus}"]
                )
        except QuestionError as exc:
            if operands:
                return _partial_analysis_result(plan, operands, step, exc)
            raise
        operands.append((step, source, selected))

    values = [as_decimal(row.get("value")) for _, _, row in operands]
    units = [str(row.get("unit") or "") for _, _, row in operands]
    operation = plan.operation
    if operation in {"ratio", "growth_rate"}:
        if len(values) != 2:
            raise QuestionError(f"{operation}必须且只能包含两个操作数")
        if units[0] != units[1]:
            raise QuestionError(f"跨表操作数单位不一致：{units}")
        if values[1] == 0:
            raise QuestionError("跨表计算的分母为0")
        computed = (
            values[0] / values[1] * Decimal("100")
            if operation == "ratio"
            else (values[0] / values[1] - Decimal("1")) * Decimal("100")
        )
        result_unit = "%"
    elif operation in {"difference", "sum", "average"}:
        if len(set(units)) != 1:
            raise QuestionError(f"跨表操作数单位不一致：{units}")
        if operation == "difference":
            if len(values) != 2:
                raise QuestionError("difference必须且只能包含两个操作数")
            computed = values[0] - values[1]
        elif operation == "sum":
            computed = sum(values, Decimal("0"))
        else:
            computed = sum(values, Decimal("0")) / Decimal(len(values))
        result_unit = units[0]
    else:
        raise QuestionError(f"未知跨表分析操作：{operation}")

    answer_text = format_decimal(computed, 2) + result_unit
    source_files = list(
        dict.fromkeys(str(source.get("file_name") or "") for _, source, _ in operands)
    )
    families = list(
        dict.fromkeys(str(source.get("dataset_family") or "") for _, source, _ in operands)
    )
    return AnswerResult(
        answer=answer_text,
        choice=None,
        answer_text=answer_text,
        source_file=" | ".join(source_files),
        dataset_family=" | ".join(families),
        evidence=[
            {
                "analysis_step": step.step_id,
                "source_title": source.get("source_title"),
                "resolution": "cross_table_operand",
                **evidence(row),
            }
            for step, source, row in operands
        ],
        plan=plan,
        explanation=(
            f"受控跨表计算：{operation}；操作数="
            + "、".join(f"{step.step_id}:{format_decimal(row.get('value'))}{row.get('unit') or ''}" for step, _, row in operands)
        ),
    )


class QueryRepository(Protocol):
    """AnswerEngine 所需的最小只读数据接口。"""

    def list_sources(self) -> list[dict[str, Any]]: ...

    def facts_for_source(
        self,
        file_name: str,
        sheet_name: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def metric_definitions(
        self, file_name: str, metric_name: str | None = None
    ) -> list[dict[str, Any]]: ...

    def institution_scopes(
        self, file_name: str, institution: str | None = None
    ) -> list[dict[str, Any]]: ...

    def release_schedules(self, file_name: str) -> list[dict[str, Any]]: ...

    def document_rows(
        self, file_name: str, sheet_name: str | None = None
    ) -> list[dict[str, Any]]: ...

    def source_capabilities(
        self, file_name: str, sheet_name: str | None = None
    ) -> list[dict[str, Any]]: ...

    def metadata_facts(
        self,
        domain_name: str,
        topic_name: str,
        period_end: str,
        metric_name: str,
        dimension: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def audit(self, *args: Any, **kwargs: Any) -> None: ...


class AnswerEngine:
    def __init__(
        self,
        repository: QueryRepository,
        strict: bool = True,
        actor: str | None = None,
        planner: Any | None = None,
        vector_provider: Any | None = None,
        allow_grounded_document_qa: bool = False,
    ):
        self.repository = repository
        self.strict = strict
        self.actor = actor
        self.planner = planner
        self.vector_provider = vector_provider
        self.allow_grounded_document_qa = allow_grounded_document_qa

    def _grounded_failure_result(
        self,
        question: str,
        options: dict[str, str] | None,
        plan: QueryPlan | None,
        error: QuestionError,
    ) -> AnswerResult | None:
        """Preserve an identified workbook when strict execution cannot answer.

        Missing rows and ambiguous dimensions are often exactly the evidence
        boundary the user is asking about.  Returning a generic exception loses
        that context, so an explicitly identified source is re-opened with its
        capability profile, facts and dictionaries for a grounded refusal or
        clarification.
        """
        if (
            not self.allow_grounded_document_qa
            or plan is None
            or not plan.source_title
        ):
            return None
        try:
            source = resolve_source(plan.source_title, self.repository.list_sources())
            fallback_plan = grounded_source_plan(
                question,
                str(source.get("source_title") or plan.source_title),
                options,
                sheet_name=plan.sheet_name,
            )
            selected_evidence = grounded_source_evidence(
                self.repository, source, fallback_plan
            )
        except QuestionError:
            return None
        return AnswerResult(
            answer="已改用证据边界检索",
            choice=None,
            answer_text=f"结构化查询未能直接完成：{error}",
            source_file=str(source.get("file_name") or ""),
            dataset_family=str(source.get("dataset_family") or ""),
            evidence=selected_evidence,
            plan=fallback_plan,
            explanation=str(error),
        )

    def answer(
        self, question: str, options: dict[str, str] | None = None
    ) -> AnswerResult:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        plan: QueryPlan | None = None
        result: AnswerResult | None = None

        def audit(status: str, error_detail: str | None = None) -> None:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.repository.audit(
                request_id,
                self.actor,
                question,
                plan,
                result,
                status,
                error_detail,
                duration_ms,
            )

        def succeeded(answer_result: AnswerResult) -> AnswerResult:
            nonlocal result
            result = answer_result
            audit("SUCCESS")
            return answer_result

        try:
            rule_plan = parse_question(question, options)
            plan = rule_plan
            if (
                self.planner is not None
                and (rule_plan.rejected or not rule_plan.source_title)
            ):
                try:
                    llm_plan = self.planner(question, options)
                    # An explicit workbook plus a supported deterministic plan
                    # goes straight to execution. LLM planning is reserved for
                    # missing sources or unsupported rule plans.
                    if not (llm_plan.rejected and not rule_plan.rejected):
                        plan = llm_plan
                except QuestionError:
                    plan = rule_plan
            if (
                self.allow_grounded_document_qa
                and rule_plan.planner == "boundary_rule"
                and plan.rejected
                and not plan.source_title
            ):
                # A model planner may conservatively reject an inference merely
                # because the workbook was not named.  Retrieve the most relevant
                # source anyway so the final refusal can verify the factual
                # premise and identify the genuinely missing evidence.
                plan = grounded_source_plan(question, "", options)
                plan.planner = "boundary_retrieval"
                plan.question_spec = rule_plan.question_spec
            named_source = None
            if not plan.source_title:
                named_source = source_named_in_question(
                    question, self.repository.list_sources()
                )
                if named_source is not None:
                    plan.source_title = str(named_source.get("source_title") or "")
            if self.allow_grounded_document_qa:
                open_plan = grounded_document_plan(question, options)
                if open_plan is None and named_source is not None:
                    open_plan = grounded_source_plan(
                        question,
                        str(named_source.get("source_title") or ""),
                        options,
                        sheet_name=plan.sheet_name,
                    )
                if (
                    open_plan is not None
                    and (
                    plan.rejected or plan.route == "resource_discovery"
                    )
                ):
                    # The workbook is already named by the user.  A planner
                    # returning find_source here would only prove that the file
                    # exists; it would not inspect whether the requested data is
                    # present.  Route to source-boundary evidence instead.
                    plan = open_plan
            if plan.rejected:
                result = AnswerResult(
                    answer=f"无法回答：{plan.rejection_reason}",
                    choice=None,
                    answer_text=plan.rejection_reason or "无法回答",
                    source_file="",
                    dataset_family="",
                    evidence=[],
                    plan=plan,
                )
                return succeeded(result)

            if plan.route == "cross_table_analysis":
                return succeeded(
                    execute_cross_table_analysis(
                        self.repository,
                        question,
                        plan,
                        self.vector_provider,
                        self.strict,
                    )
                )

            if plan.route == "resource_discovery":
                source = resolve_plan_source(
                    question,
                    plan,
                    self.repository.list_sources(),
                    self.repository,
                    self.vector_provider,
                )
                source_title = str(source.get("source_title") or "")
                result = AnswerResult(
                    answer=source_title,
                    choice=None,
                    answer_text=source_title,
                    source_file=str(source.get("file_name") or ""),
                    dataset_family=str(source.get("dataset_family") or ""),
                    evidence=[
                        {
                            "resolution": "hybrid_source_discovery",
                            "source_title": source_title,
                            "source_sheet": source.get("source_sheet_names"),
                        }
                    ],
                    plan=plan,
                )
                return succeeded(result)

            if plan.route in {"dictionary", "document_row"}:
                try:
                    source = resolve_plan_source(
                        question,
                        plan,
                        self.repository.list_sources(),
                        self.repository,
                        self.vector_provider,
                    )
                except AmbiguousQuestionError:
                    if plan.operation != "document_row":
                        raise
                    title_sources = [
                        item
                        for item in self.repository.list_sources()
                        if normalize(item.get("source_title"))
                        == normalize(plan.source_title)
                    ]
                    eligible = [
                        item
                        for item in title_sources
                        if self.repository.document_rows(
                            str(item.get("file_name")), plan.sheet_name
                        )
                    ]
                    question_text = normalize(plan.context or "")
                    qualifiers = [
                        term for term in ("人身", "财产") if term in question_text
                    ]
                    if qualifiers:
                        qualified = [
                            item
                            for item in eligible
                            if any(
                                term in normalize(str(item.get("file_name") or ""))
                                or term
                                in normalize(str(item.get("source_title") or ""))
                                for term in qualifiers
                            )
                        ]
                        if len(qualified) == 1:
                            eligible = qualified
                    if len(eligible) != 1:
                        raise
                    source = eligible[0]
                resolved_sheet = resolve_plan_sheet(
                    question,
                    plan,
                    self.repository,
                    str(source["file_name"]),
                    self.vector_provider,
                )
                if resolved_sheet:
                    plan.sheet_name = resolved_sheet
                if plan.route == "dictionary":
                    if plan.operation == "metric_definition":
                        dictionary_rows = self.repository.metric_definitions(
                            source["file_name"], plan.focus
                        )
                    elif plan.operation == "institution_scope":
                        dictionary_rows = self.repository.institution_scopes(
                            source["file_name"], plan.focus
                        )
                    elif plan.operation == "release_schedule":
                        dictionary_rows = self.repository.release_schedules(
                            source["file_name"]
                        )
                    else:
                        raise QuestionError(f"未知词典查询：{plan.operation}")
                    selected, answer = dictionary_answer(dictionary_rows, plan)
                    selected_rows = [selected]
                else:
                    if plan.operation == "grounded_document_qa":
                        selected_rows = grounded_source_evidence(
                            self.repository, source, plan
                        )
                        selected = selected_rows[0]
                        answer = "已检索到可供模型判断的候选证据"
                    else:
                        document_rows = self.repository.document_rows(
                            source["file_name"], plan.sheet_name
                        )
                        selected, answer = document_answer(document_rows, plan)
                        selected_rows = [selected]
                result = AnswerResult(
                    answer=answer,
                    choice=None,
                    answer_text=answer,
                    source_file=str(source["file_name"]),
                    dataset_family=str(source["dataset_family"]),
                    evidence=[
                        evidence_row
                        if plan.operation == "grounded_document_qa"
                        else {
                            "source_title": evidence_row.get("source_title"),
                            "source_sheet": evidence_row.get("source_sheet"),
                            "source_locator": evidence_row.get("source_cell")
                            or evidence_row.get("source_range"),
                            "row_number": evidence_row.get("row_number"),
                            "answer": answer,
                        }
                        for evidence_row in selected_rows
                    ],
                    plan=plan,
                )
                return succeeded(result)

            if plan.route == "metadata_retrieval":
                metadata_rows = self.repository.metadata_facts(
                    plan.context or "",
                    plan.from_term or "",
                    str(plan.filters.get("period_end") or ""),
                    str(plan.filters.get("metric") or ""),
                    plan.focus,
                )
                validate_numeric_quality(metadata_rows, "lookup")
                monthly_rows = [
                    row
                    for row in metadata_rows
                    if normalize(row.get("period_basis")) == normalize("month_end")
                    or "月度" in str(row.get("source_title") or "")
                ]
                if monthly_rows:
                    metadata_rows = monthly_rows
                source_titles = {
                    str(row.get("source_title") or "") for row in metadata_rows
                }
                if len(source_titles) != 1:
                    raise QuestionError(
                        f"元数据条件命中 {len(source_titles)} 份不同文件，不能唯一定位"
                    )
                source_title = next(iter(source_titles))
                result = AnswerResult(
                    answer=source_title,
                    choice=None,
                    answer_text=source_title,
                    source_file=str(metadata_rows[0].get("file_name") or ""),
                    dataset_family=str(metadata_rows[0].get("dataset_family") or ""),
                    evidence=[
                        {
                            "resolution": "metadata_fact_query",
                            **evidence(metadata_rows[0]),
                        }
                    ],
                    plan=plan,
                )
                return succeeded(result)

            source = resolve_plan_source(
                question,
                plan,
                self.repository.list_sources(),
                self.repository,
                self.vector_provider,
            )
            if not plan.source_title:
                plan.source_title = str(source.get("source_title") or "")
            resolved_sheet = resolve_plan_sheet(
                question,
                plan,
                self.repository,
                str(source["file_name"]),
                self.vector_provider,
            )
            if resolved_sheet:
                plan.sheet_name = resolved_sheet

            # A complete capability profile is negative evidence.  Enforce it
            # before touching the fact executor so an absent period, dimension
            # or metric can never be turned into a plausible-looking value.
            enforce_capability_coverage(self.repository, source, plan)
            rows = self.repository.facts_for_source(
                str(source["file_name"]), plan.sheet_name, plan.filters
            )
            if not rows:
                raise QuestionError("该文件中没有满足工作表或过滤条件的统计事实")
            quality_note = validate_numeric_quality(
                rows, "lookup" if plan.intent == "delta" else plan.intent
            )

            if plan.intent == "lookup":
                choice = None
                resolution = "strict_query"
                try:
                    selected = resolve_one(
                        rows, [plan.focus, plan.context], plan.sheet_name, self.strict
                    )
                except AmbiguousQuestionError:
                    if not numeric_options(plan.options):
                        raise
                    selected, choice, resolution = lookup_with_options(rows, plan)
                if choice is None and numeric_options(plan.options):
                    matched_choices = matching_numeric_choices(
                        selected.get("value"), plan.options
                    )
                    if len(matched_choices) == 1:
                        choice = matched_choices[0]
                        resolution = "strict_query_option_verified"
                answer_text = (
                    plan.options[choice]
                    if choice
                    else format_decimal(selected["value"])
                )
                result = AnswerResult(
                    answer=choice or answer_text,
                    choice=choice,
                    answer_text=answer_text,
                    source_file=str(source["file_name"]),
                    dataset_family=str(source["dataset_family"]),
                    evidence=[{"resolution": resolution, **evidence(selected)}],
                    plan=plan,
                    explanation=quality_note,
                )
            elif plan.intent == "compare":
                if plan.options:
                    if self.strict:
                        choice, selected_by_option, resolution = compare_with_options(
                            rows, plan
                        )
                    else:
                        selected_by_option = {
                            key: resolve_one(
                                rows, [plan.context, label], plan.sheet_name, False
                            )
                            for key, label in plan.options.items()
                        }
                        resolution = "first_match_compatibility"
                    units = {
                        str(row.get("unit") or "")
                        for row in selected_by_option.values()
                    }
                    if len(units) > 1 and self.strict:
                        raise QuestionError(
                            f"比较项单位不一致，禁止直接比较：{sorted(units)}"
                        )
                    if not self.strict:
                        ranked = sorted(
                            selected_by_option.items(),
                            key=lambda item: as_decimal(item[1]["value"]),
                            reverse=plan.operation == "argmax",
                        )
                        choice, _ = ranked[0]
                    result = AnswerResult(
                        answer=choice,
                        choice=choice,
                        answer_text=plan.options[choice],
                        source_file=str(source["file_name"]),
                        dataset_family=str(source["dataset_family"]),
                        evidence=[
                            {"option": key, "resolution": resolution, **evidence(row)}
                            for key, row in selected_by_option.items()
                        ],
                        plan=plan,
                        explanation=quality_note,
                    )
                else:
                    winner, ranked_rows, resolution = compare_open_dimension(rows, plan)
                    winning_row = ranked_rows[0]
                    unit = str(winning_row.get("unit") or "")
                    answer_text = (
                        f"{winner}（{format_decimal(winning_row.get('value'))}{unit}）"
                    )
                    result = AnswerResult(
                        answer=winner,
                        choice=None,
                        answer_text=answer_text,
                        source_file=str(source["file_name"]),
                        dataset_family=str(source["dataset_family"]),
                        evidence=[
                            {
                                "rank": index,
                                "group_by": plan.group_by,
                                "group_value": row.get("_group_name"),
                                "resolution": resolution,
                                **evidence(row),
                            }
                            for index, row in enumerate(ranked_rows[:50], start=1)
                        ],
                        plan=plan,
                        explanation=quality_note,
                    )
            elif plan.intent == "analysis":
                labels = [label for label in (plan.focus, plan.context) if label]
                scored_analysis_rows = [
                    (sum(term_score(row, label) for label in labels), row)
                    for row in rows
                    if sheet_matches(row, plan.sheet_name)
                    and all(term_score(row, label) > 0 for label in labels)
                ]
                analysis_rows = [row for _, row in scored_analysis_rows]
                if not analysis_rows:
                    raise QuestionError("没有找到满足分析条件的数据")

                selected_rows: list[dict[str, Any]]
                if plan.operation == "trend":
                    by_period: dict[str, list[dict[str, Any]]] = {}
                    for row in analysis_rows:
                        by_period.setdefault(
                            str(row.get("period_end") or ""), []
                        ).append(row)
                    selected_rows = [
                        resolve_one(
                            group,
                            [plan.focus, plan.context],
                            plan.sheet_name,
                            self.strict,
                        )
                        for _, group in sorted(by_period.items())
                    ]
                    answer_text = "；".join(
                        f"{row.get('period_end')}={format_decimal(row.get('value'))}"
                        for row in selected_rows
                    )
                else:
                    best_score = max(score for score, _ in scored_analysis_rows)
                    selected_rows = [
                        row
                        for score, row in scored_analysis_rows
                        if score == best_score
                    ]
                    units = {str(row.get("unit") or "") for row in selected_rows}
                    if len(units) > 1 and self.strict:
                        raise QuestionError(f"分析数据单位不一致：{sorted(units)}")
                    values = [as_decimal(row.get("value")) for row in selected_rows]
                    if plan.operation == "sum":
                        computed: Decimal | int = sum(values, Decimal("0"))
                    elif plan.operation == "avg":
                        computed = sum(values, Decimal("0")) / Decimal(len(values))
                    elif plan.operation == "min":
                        computed = min(values)
                    elif plan.operation == "max":
                        computed = max(values)
                    elif plan.operation == "count":
                        computed = len(values)
                    else:
                        raise QuestionError(f"未知分析操作：{plan.operation}")
                    answer_text = (
                        str(computed)
                        if isinstance(computed, int)
                        else format_decimal(computed)
                    )
                result = AnswerResult(
                    answer=answer_text,
                    choice=None,
                    answer_text=answer_text,
                    source_file=str(source["file_name"]),
                    dataset_family=str(source["dataset_family"]),
                    evidence=[
                        {"resolution": plan.operation, **evidence(row)}
                        for row in selected_rows[:50]
                    ],
                    plan=plan,
                    explanation=quality_note,
                )
            elif plan.intent == "delta":
                choice = None
                resolution = "strict_query"
                if is_quarter_endpoint_delta(plan):
                    if numeric_options(plan.options):
                        start, end, delta, choice, resolution = delta_with_options(
                            rows, plan
                        )
                    else:
                        pairs = delta_pairs(rows, plan)
                        if len(pairs) != 1:
                            raise AmbiguousQuestionError(
                                f"第一季度到第四季度存在多组同口径数据：{len(pairs)}"
                            )
                        start, end, delta = pairs[0]
                        resolution = "quarter_endpoint_auto"
                else:
                    try:
                        start = resolve_one(
                            rows,
                            [plan.focus, plan.context, plan.from_term],
                            plan.sheet_name,
                            self.strict,
                        )
                        end = resolve_one(
                            rows,
                            [plan.focus, plan.context, plan.to_term],
                            plan.sheet_name,
                            self.strict,
                        )
                        delta = as_decimal(end["value"]) - as_decimal(start["value"])
                    except AmbiguousQuestionError:
                        if not numeric_options(plan.options):
                            raise
                        start, end, delta, choice, resolution = delta_with_options(
                            rows, plan
                        )
                validate_numeric_quality([start, end], "delta")
                if start.get("unit") != end.get("unit"):
                    raise QuestionError(
                        f"两处数据单位不一致：{start.get('unit')} != {end.get('unit')}"
                    )
                if choice is None and numeric_options(plan.options):
                    matched_choices = matching_numeric_choices(delta, plan.options)
                    if len(matched_choices) == 1:
                        choice = matched_choices[0]
                        resolution = "strict_query_option_verified"
                answer_text = (
                    plan.options[choice] if choice else format_decimal(delta, 2)
                )
                result = AnswerResult(
                    answer=choice or answer_text,
                    choice=choice,
                    answer_text=answer_text,
                    source_file=str(source["file_name"]),
                    dataset_family=str(source["dataset_family"]),
                    evidence=[
                        {"role": "from", "resolution": resolution, **evidence(start)},
                        {"role": "to", "resolution": resolution, **evidence(end)},
                    ],
                    plan=plan,
                    explanation="；".join(
                        part
                        for part in (quality_note, quarter_endpoint_explanation(plan))
                        if part
                    ),
                )
            else:
                raise QuestionError(f"未知题型：{plan.intent}")

            return succeeded(result)
        except Exception as exc:
            if isinstance(exc, QuestionError):
                fallback = self._grounded_failure_result(
                    question, options, plan, exc
                )
                if fallback is not None:
                    plan = fallback.plan
                    return succeeded(fallback)
            audit("ERROR", str(exc))
            raise
