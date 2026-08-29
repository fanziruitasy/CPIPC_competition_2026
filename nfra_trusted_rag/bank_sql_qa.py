from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
except ModuleNotFoundError:  # DuckDB 部署不需要 PostgreSQL 驱动
    psycopg2 = None
    RealDictCursor = None
    Json = None


def scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes, Decimal)):
        value = value.item()
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.date()
    return value


def text_value(record: dict[str, Any], name: str) -> str | None:
    value = scalar(record.get(name))
    return None if value is None else (str(value).strip() or None)


def split_source_name(file_name: str) -> tuple[str, str | None]:
    parts = file_name.split("_", 2)
    if len(parts) == 3 and parts[0].isdigit():
        return parts[1], parts[2]
    return Path(file_name).stem, None

class QuestionError(ValueError):
    pass


class AmbiguousQuestionError(QuestionError):
    pass


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
    filters: dict[str, str] = field(default_factory=dict)
    source_filters: dict[str, str] = field(default_factory=dict)
    answer_mode: str = "structured_query"
    operations: list[str] = field(default_factory=list)
    missing_conditions: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    planner: str = "rule"
    route: str = "fact_observation"
    rejection_reason: str | None = None


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


def normalize(text: Any) -> str:
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).lower()
    value = value.replace("四季度", "4季度").replace("三季度", "3季度")
    value = value.replace("二季度", "2季度").replace("一季度", "1季度")
    return re.sub(r"[\s\-_/（）()《》“”‘’：:，,。.;；]+", "", value)


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
        operations=["reject"],
        missing_conditions=[reason],
        needs_clarification=True,
        route="reject",
        rejection_reason=reason,
    )


def _answer_mode(source_title: str, default: str = "structured_query") -> str:
    """Missing source metadata turns an otherwise direct task into a hybrid one."""
    return default if source_title else "hybrid"


def parse_question(question: str, options: dict[str, str] | None = None) -> QueryPlan:
    provided_options = {key.upper(): value for key, value in (options or {}).items() if value}
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
            operations=["resolve_source", "query"],
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
            intent="dictionary", source_title=source_title, sheet_name=sheet_name,
            focus=quoted[-1], context=question, from_term=None, to_term=None,
            operation="metric_definition", options=provided_options,
            answer_mode=_answer_mode(source_title), operations=["retrieve_definition"],
            route="dictionary",
        )
    if "机构范围是什么" in question and quoted:
        return QueryPlan(
            intent="dictionary", source_title=source_title, sheet_name=sheet_name,
            focus=quoted[-1], context=question, from_term=None, to_term=None,
            operation="institution_scope", options=provided_options,
            answer_mode=_answer_mode(source_title), operations=["retrieve_definition"],
            route="dictionary",
        )
    if "通常何时发布" in question and quoted:
        return QueryPlan(
            intent="dictionary", source_title=source_title, sheet_name=sheet_name,
            focus=quoted[-1], context=question, from_term=None, to_term=None,
            operation="release_schedule", options=provided_options,
            answer_mode=_answer_mode(source_title), operations=["query"],
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
            operations=["resolve_source", "resolve_sheet", "lookup"],
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
            operations=["resolve_source", "resolve_sheet", "lookup", "difference"],
        )

    high_words = ("数值最高", "最大", "最多", "最高")
    low_words = ("数值最低", "最小", "最少", "最低")
    comparison_text = re.sub(r"《[^》]+》", "", question)
    if any(token in comparison_text for token in high_words + low_words) and (
        "哪一项" in comparison_text or "哪个" in comparison_text or "哪些" in comparison_text
    ):
        if not provided_options and len(quoted) >= 2:
            provided_options = {chr(ord("A") + index): term for index, term in enumerate(quoted[:-1])}
        if not provided_options:
            return rejected_plan(question, "比较题缺少候选选项")
        operation = "argmax" if any(token in comparison_text for token in high_words) else "argmin"
        return QueryPlan(
            intent="compare",
            source_title=source_title,
            sheet_name=sheet_name,
            focus=None,
            context=quoted[-1] if quoted else None,
            from_term=None,
            to_term=None,
            operation=operation,
            options=provided_options,
            answer_mode=_answer_mode(source_title),
            operations=["resolve_source", "resolve_sheet", "rank"],
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
            operations=["resolve_source", "resolve_sheet", "lookup"],
        )

    if "属于哪类机构" in question and quoted:
        operation = "reference_header"
        focus = quoted[-1]
    elif any(token in question for token in (
        "是什么？", "有哪些？", "包含哪些", "区分了哪些", "哪一岗位",
        "按哪些险种拆分", "最高分是多少", "指什么", "权重达到多少",
        "最低总分和最高总分", "请列出", "列出", "栏目", "填报项",
    )):
        if any(token in question for token in (
            "最高分是多少", "指什么", "权重达到多少", "最低总分和最高总分",
        )):
            operation = "rule_lookup"
            focus = quoted[-1] if quoted else None
            if not focus:
                rule_focus_matches = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}?(?:指标|情况|机制)", question)
                rule_focus_matches = [item for item in rule_focus_matches if item not in source_title]
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
        operations=["resolve_source", "resolve_sheet", "retrieve_document"],
        route="document_row",
    )


PLAN_FILTERS = {
    "metric",
    "entity",
    "region",
    "period_end",
    "from_period",
    "to_period",
    "start_period",
    "end_period",
    "period_basis",
    "scope",
    "product_line",
    "measure_type",
    "unit",
}

SOURCE_FILTERS = {
    "domain",
    "topic",
    "dataset_family",
    "content_type",
    "frequency",
}

ANSWER_MODES = {
    "structured_query",
    "semantic_retrieval",
    "hybrid",
    "analysis_pipeline",
    "clarify_or_reject",
}


def validated_query_plan(payload: Any, options: dict[str, str] | None = None) -> QueryPlan:
    if not isinstance(payload, dict):
        raise QuestionError("Qwen 未返回 JSON 对象")
    external_intent = payload.get("intent") or payload.get("task_type")
    operation = payload.get("operation")
    intent_specs: dict[str, tuple[str, set[str], str, str]] = {
        "lookup": ("lookup", {"lookup"}, "fact_observation", "structured_query"),
        "compare": ("compare", {"argmax", "argmin"}, "fact_observation", "structured_query"),
        "rank": ("compare", {"argmax", "argmin"}, "fact_observation", "structured_query"),
        "delta": ("delta", {"to_minus_from"}, "fact_observation", "structured_query"),
        "aggregate": ("analysis", {"sum", "avg", "min", "max", "count"}, "fact_analysis", "analysis_pipeline"),
        "trend": ("analysis", {"trend"}, "fact_analysis", "analysis_pipeline"),
        "definition": ("dictionary", {"metric_definition"}, "dictionary", "structured_query"),
        "institution_scope": ("dictionary", {"institution_scope"}, "dictionary", "structured_query"),
        "release_schedule": ("dictionary", {"release_schedule"}, "dictionary", "structured_query"),
        "document_qa": ("document_row", {"document_row", "rule_lookup", "reference_header"}, "document_row", "semantic_retrieval"),
        "find_source": ("metadata_retrieval", {"find_source", "metadata_retrieval"}, "resource_discovery", "hybrid"),
    }
    if external_intent in {"clarify", "reject"} or payload.get("needs_clarification") is True:
        missing = payload.get("missing_conditions") or []
        if not isinstance(missing, list):
            missing = [str(missing)]
        reason = str(payload.get("rejection_reason") or "、".join(map(str, missing)) or "问题条件不足，需要补充信息")
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
    filters = payload.get("filters") or {}
    if not isinstance(filters, dict) or set(filters) - PLAN_FILTERS:
        raise QuestionError("Qwen 返回了不允许的查询条件")
    if any(value is not None and not isinstance(value, str) for value in filters.values()):
        raise QuestionError("Qwen 查询条件的值必须是字符串或 null")
    clean_filters = {
        key: value.strip()
        for key, value in filters.items()
        if isinstance(value, str) and value.strip()
    }
    raw_source_filters = payload.get("source_filters") or {}
    if not isinstance(raw_source_filters, dict) or set(raw_source_filters) - SOURCE_FILTERS:
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
    misplaced_basis = period_basis_aliases.get(normalize(clean_filters.get("measure_type")))
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

    raw_operations = payload.get("operations") or [operation]
    if not isinstance(raw_operations, list):
        raise QuestionError("Qwen operations 必须是数组")
    operations: list[str] = []
    for item in raw_operations:
        value = item.get("type") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip():
            operations.append(value.strip())

    requested_mode = payload.get("answer_mode")
    if requested_mode is not None and requested_mode not in ANSWER_MODES:
        raise QuestionError(f"Qwen 返回了未知回答模式：{requested_mode}")
    answer_mode = str(requested_mode or default_mode)
    if not source_title and answer_mode in {"structured_query", "semantic_retrieval"}:
        answer_mode = "hybrid"

    raw_missing = payload.get("missing_conditions") or []
    if not isinstance(raw_missing, list):
        raise QuestionError("Qwen missing_conditions 必须是数组")
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
        filters=clean_filters,
        source_filters=clean_source_filters,
        answer_mode=answer_mode,
        operations=operations,
        missing_conditions=[str(value) for value in raw_missing if str(value).strip()],
        needs_clarification=False,
        route=route,
    )
    required = {
        "lookup": (plan.focus,),
        "compare": (plan.context, plan.options),
        "delta": (plan.focus, plan.from_term, plan.to_term),
        "dictionary": (plan.focus,) if plan.operation != "release_schedule" else (plan.context,),
        "document_row": (plan.context,),
        "metadata_retrieval": (plan.source_filters or plan.filters or plan.context,),
        "analysis": (plan.focus or plan.context,),
    }[intent]
    if not all(required):
        raise QuestionError("Qwen 返回的查询计划缺少必要条件")
    return plan


class QwenQueryPlanner:
    def __init__(self, api_key: str, model: str, endpoint: str, timeout: int = 30):
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.endswith("/chat/completions"):
            self.endpoint += "/chat/completions"
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

    def __call__(self, question: str, options: dict[str, str] | None = None) -> QueryPlan:
        system_prompt = """
你是金融 Excel 问答系统的查询规划器，只生成受控计划，不回答、不计算、不生成 SQL。
只输出 JSON 对象，字段为：
intent、answer_mode、source_title、sheet_name、source_filters、focus、context、from_term、to_term、operation、operations、filters、candidates、needs_clarification、missing_conditions、rejection_reason。

intent 只能是：
lookup、compare、rank、delta、aggregate、trend、definition、institution_scope、release_schedule、document_qa、find_source、clarify、reject。
对应 operation：
- lookup -> lookup
- compare/rank -> argmax 或 argmin
- delta -> to_minus_from
- aggregate -> sum、avg、min、max 或 count
- trend -> trend
- definition -> metric_definition
- institution_scope -> institution_scope
- release_schedule -> release_schedule
- document_qa -> document_row、rule_lookup 或 reference_header
- find_source -> find_source

answer_mode 只能是 structured_query、semantic_retrieval、hybrid、analysis_pipeline、clarify_or_reject。
filters 只允许 metric、entity、region、period_end、from_period、to_period、start_period、end_period、period_basis、scope、product_line、measure_type、unit。
source_filters 只允许 domain、topic、dataset_family、content_type、frequency。
operations 是可组合步骤，例如 resolve_source、resolve_sheet、lookup、query、rank、difference、retrieve_definition、retrieve_document。
题目未给文件标题时 source_title 必须为 null，不得猜文件名；此时通常选择 hybrid。题目中的候选比较项写入 candidates。
单期查询用 period_end，两点变化用 from_period/to_period，连续趋势范围用 start_period/end_period；日期使用 YYYY-MM-DD。只填明确条件，不得猜测。
确实缺少期间、机构、口径等必要条件时使用 clarify，并在 missing_conditions 中列出缺项。
""".strip()
        request = Request(
            self.endpoint,
            data=json.dumps(
                {
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
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise QuestionError(f"Qwen 请求失败（HTTP {exc.code}）：{detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise QuestionError(f"Qwen 请求失败：{exc}") from exc
        try:
            content = result["choices"][0]["message"]["content"]
            plan = validated_query_plan(json.loads(content), options)
            plan.planner = f"qwen:{self.model}"
            return plan
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise QuestionError("Qwen 返回了无效的查询计划") from exc


def title_score(query: str, source: dict[str, Any]) -> float:
    target = normalize(query)
    candidates = [
        normalize(source.get("source_title")),
        normalize(Path(str(source.get("attachment_name") or "")).stem),
        normalize(Path(str(source.get("file_name") or "")).stem),
    ]
    if target in candidates:
        return 1.0
    if any(target and (target in candidate or candidate in target) for candidate in candidates):
        return 0.95
    return max((SequenceMatcher(None, target, candidate).ratio() for candidate in candidates), default=0.0)


def resolve_source(query: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    scored = sorted(((title_score(query, source), source) for source in sources), key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] < 0.72:
        raise QuestionError(f"找不到题目指定的Excel：{query}")
    best_score = scored[0][0]
    best = [source for score, source in scored if abs(score - best_score) < 0.0001]
    if len(best) > 1:
        exact_title = [source for source in best if normalize(source.get("source_title")) == normalize(query)]
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


def source_relevance_score(
    question: str, plan: QueryPlan, source: dict[str, Any]
) -> float:
    """Hybrid metadata/lexical score used before optional semantic reranking."""
    query = normalize(question)
    profile_text = _source_profile(source)
    profile = normalize(profile_text)
    title = normalize(source.get("source_title"))
    attachment = normalize(Path(str(source.get("attachment_name") or "")).stem)
    score = SequenceMatcher(None, query, profile).ratio() * 2.0

    if plan.source_title:
        hint = normalize(plan.source_title)
        if hint in {title, attachment, normalize(Path(str(source.get("file_name") or "")).stem)}:
            score += 100.0
        elif hint and (hint in profile or profile in hint):
            score += 35.0
        else:
            score -= 20.0
    else:
        for alias in (title, attachment):
            if len(alias) >= 5 and alias in query:
                score += 40.0

    source_field_map = {
        "domain": "domain_name",
        "topic": "topic_name",
        "dataset_family": "dataset_family",
        "content_type": "content_type_code",
        "frequency": "frequency",
    }
    for key, value in plan.source_filters.items():
        expected = normalize(value)
        actual = normalize(source.get(source_field_map[key]))
        if expected == actual:
            score += 9.0
        elif expected and actual and (expected in actual or actual in expected):
            score += 5.0
        else:
            score -= 7.0

    if plan.sheet_name:
        expected_sheet = normalize(plan.sheet_name)
        if expected_sheet and expected_sheet in normalize(source.get("source_sheet_names")):
            score += 8.0
        else:
            score -= 3.0
    for term in (plan.focus, plan.context, plan.filters.get("metric"), plan.filters.get("entity")):
        expected = normalize(term)
        if len(expected) >= 2 and expected in profile:
            score += 3.0

    periods = [
        plan.filters.get(name)
        for name in ("period_end", "from_period", "to_period", "start_period", "end_period")
        if plan.filters.get(name)
    ]
    period_start = str(source.get("period_start") or "")[:10]
    period_end = str(source.get("period_end") or "")[:10]
    for period in periods:
        if period_start and period_end and period_start <= str(period) <= period_end:
            score += 5.0
        elif period_start or period_end:
            score -= 6.0
    return score


def resolve_plan_source(
    question: str,
    plan: QueryPlan,
    sources: list[dict[str, Any]],
    repository: Any | None = None,
) -> dict[str, Any]:
    """Resolve an explicit source deterministically or discover it from metadata."""
    if plan.source_title:
        try:
            return resolve_source(plan.source_title, sources)
        except AmbiguousQuestionError:
            # Attachment and sheet semantics can disambiguate repeated page titles.
            candidates = [
                source
                for source in sources
                if normalize(source.get("source_title")) == normalize(plan.source_title)
            ]
            if not candidates:
                raise
    else:
        candidates = sources
        candidate_loader = getattr(repository, "source_candidates", None)
        if callable(candidate_loader) and plan.route in {
            "fact_observation", "fact_analysis", "resource_discovery"
        }:
            hard_filters = dict(plan.filters)
            if plan.focus and plan.intent in {"lookup", "delta", "analysis"}:
                hard_filters.setdefault("metric", plan.focus)
            candidate_files = set(candidate_loader(hard_filters))
            if candidate_files:
                candidates = [
                    source
                    for source in candidates
                    if str(source.get("file_name") or "") in candidate_files
                ]

    scored = sorted(
        ((source_relevance_score(question, plan, source), source) for source in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 8.0:
        raise QuestionError("无法根据问题中的领域、主题、期间和指标唯一定位 Excel")
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.75:
        names = [str(source.get("file_name") or "") for _, source in scored[:5]]
        raise AmbiguousQuestionError(f"Excel 候选不唯一，请补充文件或期间：{names}")
    return scored[0][1]


def resolve_plan_sheet(
    question: str, plan: QueryPlan, repository: Any, file_name: str
) -> str | None:
    profile_loader = getattr(repository, "sheet_profiles", None)
    if not callable(profile_loader):
        return plan.sheet_name
    profiles = profile_loader(file_name)
    if not profiles:
        return plan.sheet_name
    if plan.sheet_name:
        expected = normalize(plan.sheet_name)
        matches = [
            row for row in profiles
            if expected == normalize(row.get("source_sheet"))
            or expected in normalize(row.get("source_sheet"))
            or normalize(row.get("source_sheet")) in expected
        ]
        if len(matches) == 1:
            return str(matches[0]["source_sheet"])
        if not matches:
            raise QuestionError(f"指定工作表不存在：{plan.sheet_name}")
        raise AmbiguousQuestionError(f"工作表名称命中多项：{[row['source_sheet'] for row in matches]}")
    if len(profiles) == 1:
        return str(profiles[0]["source_sheet"])

    query = normalize(question)
    terms = [
        plan.focus,
        plan.context,
        plan.filters.get("metric"),
        plan.filters.get("entity"),
        plan.filters.get("region"),
        plan.filters.get("product_line"),
    ]
    ranked: list[tuple[float, dict[str, Any]]] = []
    for profile in profiles:
        sheet = normalize(profile.get("source_sheet"))
        content = normalize(profile.get("content"))
        score = SequenceMatcher(None, query, sheet + content[:2000]).ratio()
        if len(sheet) >= 2 and sheet in query:
            score += 8.0
        for term in terms:
            expected = normalize(term)
            if len(expected) >= 2 and (expected in sheet or expected in content):
                score += 2.5
        ranked.append((score, profile))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked[0][0] >= 3.0 and (
        len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.5
    ):
        return str(ranked[0][1]["source_sheet"])
    # Do not guess: downstream row-level resolution may still prove uniqueness.
    return None


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
    terms = {normalize(row.get(field)) for field in fields if row.get(field) is not None}
    basis = normalize(row.get("period_basis"))
    if basis in {"ytd", "yeartodate"}:
        terms.update({normalize("本年累计"), normalize("截至当期"), normalize("本年累计/截至当期")})
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
        return 100 if product == target or (metric.endswith(target) and metric != target) else 0
    if target == normalize("全国合计") and normalize(row.get("region_name")) == normalize("全国"):
        return 100
    if target == normalize("截至当期-账面余额"):
        return 100 if normalize(row.get("measure_type")) == normalize("balance") else 0
    if target in {normalize("年-季度"), normalize("季度"), normalize("季度-季度")}:
        return 90 if normalize(row.get("period_basis")) == normalize("quarter_end") else 0

    candidates = row_terms(row)
    if target in candidates:
        return 100
    if any(
        len(target) >= 2 and len(candidate) >= 2 and (target in candidate or candidate in target)
        for candidate in candidates
    ):
        return 50
    return 0


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
        "unit": row.get("unit"),
        "period_end": str(row.get("period_end") or ""),
        "period_basis": row.get("period_basis"),
        "entity": row.get("entity_name") or row.get("region_name"),
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
        "source_quality_status": row.get("source_quality_status") or row.get("quality_status"),
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
        for field in ("scope_version", "statistical_scope_version", "accounting_basis_version"):
            values = {str(row.get(field) or "") for row in rows if row.get(field) is not None}
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
        query = normalize(plan.context or "")
        frequency = "月" if "月度" in query else ("季" if "季度" in query else "")
        data_scope = "境内" if "境内" in query else ("法人" if "法人" in query else "")
        scored = sorted(
            rows,
            key=lambda row: (
                normalize(plan.focus) in normalize(row.get("indicator_names")),
                bool(frequency) and frequency in normalize(row.get("frequency")),
                bool(data_scope) and data_scope in normalize(row.get("data_scope")),
                row.get("release_year") or 0,
            ),
        )
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
        matches = [row for row in rows if target and target in normalize(row.get("row_text"))]
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
    selected = max(enriched, key=lambda row: text_similarity_score(plan.context or "", row))
    answer = str(selected.get("row_text") or "").strip()
    if not answer:
        raise QuestionError("命中的文本行为空")
    return selected, answer


def structure_answer(rows: list[dict[str, Any]], plan: QueryPlan) -> dict[str, Any]:
    """Rank table metadata rows by their structure, not by the question/title text."""
    question = normalize(plan.context or "")
    candidates = [row for row in rows if str(row.get("row_text") or "").strip()]
    if not candidates:
        raise QuestionError("没有可用的结构行")

    if "岗位签章" in question:
        role_matches: list[tuple[int, int, dict[str, Any], re.Match[str]]] = []
        for row in candidates:
            match = re.search(r"(总精算师|财务负责人|填\s*表\s*人)", str(row.get("row_text") or ""))
            if match:
                role_rank = 0 if match.group(1) == "总精算师" else 1
                role_matches.append((role_rank, int(row.get("row_number") or 0), row, match))
        if role_matches:
            _, _, row, match = min(role_matches, key=lambda item: item[:2])
            return row, re.sub(r"\s+", "", match.group(1))

    if "统计年度" in question or "统计范围" in question:
        note_rows = [
            row
            for row in candidates
            if normalize(str(row.get("row_text") or "")).startswith("备注")
            and ("统计年度" in str(row.get("row_text") or "") or "统计范围" in str(row.get("row_text") or ""))
        ]
        if note_rows:
            return note_rows[0], str(note_rows[0].get("row_text") or "").strip()

    def cells(row: dict[str, Any]) -> list[str]:
        return [cell.strip() for cell in str(row.get("row_text") or "").split(" | ")]

    def numeric_like(cell: str) -> bool:
        return bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?%?", cell.replace(",", "")))

    insurance_terms = {
        "机动车辆保险", "企业财产保险", "家庭财产保险", "工程保险", "责任保险",
        "信用保险", "保证保险", "船舶保险", "货物运输保险", "特殊风险保险",
        "农业保险", "健康险", "意外伤害保险", "其他险",
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
            for grouping_row in sorted(grouping_rows, key=lambda row: int(row.get("row_number") or 0)):
                next_headers = [
                    row
                    for row in candidates
                    if int(row.get("row_number") or 0) > int(grouping_row.get("row_number") or 0)
                    and len([cell for cell in cells(row) if cell]) >= 3
                ]
                if next_headers:
                    selected = min(next_headers, key=lambda row: int(row.get("row_number") or 0))
                    return selected, str(selected.get("row_text") or "").strip()

    if "基础字段" in question:
        base_rows = []
        for row in candidates:
            row_cells = cells(row)
            nonempty = [cell for cell in row_cells if cell]
            hits = sum(normalize(cell) and normalize(cell) in question for cell in nonempty)
            administrative = sum(
                any(token in cell for token in ("填报单位", "填报日期", "联系人", "联系电话"))
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
            insurance_cells = sum(normalize(cell) in {normalize(term) for term in insurance_terms} for cell in row_cells)
            score += min(insurance_cells * 0.8, 10.0)

        # A work-sheet or report title usually occurs as a single-cell row. Such
        # rows duplicate the question text and otherwise beat the real header.
        title_like_values = [plan.focus, plan.sheet_name, plan.source_title]
        if not separators and any(
            value and (normalize(row_text) == normalize(value) or normalize(value) in normalize(row_text))
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
        return bool(re.fullmatch(r"\d+", first_cell)) or normalize(first_cell) == normalize("合计")

    real_rule_rows = [row for row in data_rows if is_rule_row(row)]
    if real_rule_rows:
        data_rows = real_rule_rows

    if "最低总分和最高总分" in question:
        candidates = [row for row in data_rows if "合计" in normalize(row.get("row_text"))]
    elif focus:
        candidates = [row for row in data_rows if normalize(focus) in normalize(row.get("row_text"))]
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
        match = re.search(r"\d+(?:\.\d+)?%（含）以上", str(selected.get("row_text") or ""))
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


def resolve_one(rows: list[dict[str, Any]], labels: list[str | None], sheet_name: str | None, strict: bool) -> dict[str, Any]:
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
        raise QuestionError(f"没有找到满足条件的数据：{[label for label in labels if label]}")
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
        sample = [evidence(row) for row in matches[:5]]
        raise AmbiguousQuestionError(f"条件命中多条不同数据，需要补充期间或口径：{sample}")
    matches.sort(key=lambda row: (str(row.get("source_sheet") or ""), excel_cell_key(row.get("source_cell"))))
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
        matched.extend((choice, row) for choice in matching_numeric_choices(row.get("value"), plan.options))
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

    compare_entities = sum(
        any(
            exact_field_match(row, field, label)
            for row in context_rows
            for field in ("entity_name", "region_name")
        )
        for label in plan.options.values()
    ) >= 2
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
        values = {choice: as_decimal(row.get("value")) for choice, row in selected.items()}
        winner = (max if plan.operation == "argmax" else min)(values, key=values.get)
        earliest = min(excel_cell_key(row.get("source_cell")) for row in selected.values())
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
    resolution = "option_period_consensus" if len(evaluated) > 1 else "option_literal_values"
    return chosen["winner"], chosen["selected"], resolution


def is_period_term(term: str | None) -> bool:
    target = normalize(term)
    return any(token in target for token in (normalize("季度"), normalize("本年累计"), normalize("截至当期")))


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


def term_matches_any_field(rows: list[dict[str, Any]], term: str | None, fields: tuple[str, ...]) -> bool:
    return any(exact_field_match(row, field, term) for row in rows for field in fields)


def delta_pairs(rows: list[dict[str, Any]], plan: QueryPlan) -> list[tuple[dict[str, Any], dict[str, Any], Decimal]]:
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
    ) and term_matches_any_field(rows, plan.to_term, ("metric_name", "product_line_name"))
    fixed_dimensions = ["metric_code", "entity_code", "region_code", "product_line"]
    if entity_changes:
        fixed_dimensions = [field for field in fixed_dimensions if field not in {"entity_code", "region_code"}]
    if metric_changes:
        fixed_dimensions = [field for field in fixed_dimensions if field not in {"metric_code", "product_line"}]
    pairs: list[tuple[dict[str, Any], dict[str, Any], Decimal]] = []
    for start in starts:
        for end in ends:
            if start.get("source_cell") == end.get("source_cell"):
                continue
            if start.get("unit") != end.get("unit"):
                continue
            if not all(
                same_dimension(start, end, field)
                for field in fixed_dimensions
            ):
                continue
            same_period = str(start.get("period_end") or "") == str(end.get("period_end") or "")
            if temporal and same_period:
                continue
            if temporal and str(start.get("period_end") or "") > str(end.get("period_end") or ""):
                continue
            if not temporal and not same_period:
                continue
            pairs.append((start, end, as_decimal(end.get("value")) - as_decimal(start.get("value"))))
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


class AnswerEngine:
    def __init__(
        self,
        repository: PostgresRepository,
        strict: bool = True,
        actor: str | None = None,
        planner: Any | None = None,
    ):
        self.repository = repository
        self.strict = strict
        self.actor = actor
        self.planner = planner

    def answer(self, question: str, options: dict[str, str] | None = None) -> AnswerResult:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        plan: QueryPlan | None = None
        result: AnswerResult | None = None
        try:
            rule_plan = parse_question(question, options)
            plan = rule_plan
            if self.planner is not None:
                try:
                    llm_plan = self.planner(question, options)
                    # A deterministic supported plan is preferable to an LLM
                    # refusal, but otherwise the LLM plan drives execution.
                    if not (
                        llm_plan.route == "reject" and rule_plan.route != "reject"
                    ):
                        plan = llm_plan
                except QuestionError:
                    plan = rule_plan
            if plan.route == "reject":
                result = AnswerResult(
                    answer=f"无法回答：{plan.rejection_reason}",
                    choice=None,
                    answer_text=plan.rejection_reason or "无法回答",
                    source_file="",
                    dataset_family="",
                    evidence=[],
                    plan=plan,
                )
                duration_ms = int((time.perf_counter() - started) * 1000)
                self.repository.audit(request_id, self.actor, question, plan, result, "SUCCESS", None, duration_ms)
                return result

            if plan.route == "resource_discovery":
                source = resolve_plan_source(
                    question, plan, self.repository.list_sources(), self.repository
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
                duration_ms = int((time.perf_counter() - started) * 1000)
                self.repository.audit(request_id, self.actor, question, plan, result, "SUCCESS", None, duration_ms)
                return result

            if plan.route in {"dictionary", "document_row"}:
                try:
                    source = resolve_plan_source(
                        question, plan, self.repository.list_sources(), self.repository
                    )
                except AmbiguousQuestionError:
                    if plan.operation != "document_row":
                        raise
                    title_sources = [
                        item for item in self.repository.list_sources()
                        if normalize(item.get("source_title")) == normalize(plan.source_title)
                    ]
                    eligible = [
                        item for item in title_sources
                        if self.repository.document_rows(str(item.get("file_name")), plan.sheet_name)
                    ]
                    question_text = normalize(plan.context or "")
                    qualifiers = [term for term in ("人身", "财产") if term in question_text]
                    if qualifiers:
                        qualified = [
                            item for item in eligible
                            if any(
                                term in normalize(str(item.get("file_name") or ""))
                                or term in normalize(str(item.get("source_title") or ""))
                                for term in qualifiers
                            )
                        ]
                        if len(qualified) == 1:
                            eligible = qualified
                    if len(eligible) != 1:
                        raise
                    source = eligible[0]
                resolved_sheet = resolve_plan_sheet(
                    question, plan, self.repository, str(source["file_name"])
                )
                if resolved_sheet:
                    plan.sheet_name = resolved_sheet
                if plan.route == "dictionary":
                    if plan.operation == "metric_definition":
                        dictionary_rows = self.repository.metric_definitions(source["file_name"], plan.focus)
                    elif plan.operation == "institution_scope":
                        dictionary_rows = self.repository.institution_scopes(source["file_name"], plan.focus)
                    elif plan.operation == "release_schedule":
                        dictionary_rows = self.repository.release_schedules(source["file_name"])
                    else:
                        raise QuestionError(f"未知词典查询：{plan.operation}")
                    selected, answer = dictionary_answer(dictionary_rows, plan)
                else:
                    selected, answer = document_answer(
                        self.repository.document_rows(source["file_name"], plan.sheet_name), plan
                    )
                result = AnswerResult(
                    answer=answer,
                    choice=None,
                    answer_text=answer,
                    source_file=str(source["file_name"]),
                    dataset_family=str(source["dataset_family"]),
                    evidence=[
                        {
                            "source_title": selected.get("source_title"),
                            "source_sheet": selected.get("source_sheet"),
                            "source_locator": selected.get("source_cell") or selected.get("source_range"),
                            "row_number": selected.get("row_number"),
                            "answer": answer,
                        }
                    ],
                    plan=plan,
                )
                duration_ms = int((time.perf_counter() - started) * 1000)
                self.repository.audit(request_id, self.actor, question, plan, result, "SUCCESS", None, duration_ms)
                return result

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
                    row for row in metadata_rows
                    if normalize(row.get("period_basis")) == normalize("month_end")
                    or "月度" in str(row.get("source_title") or "")
                ]
                if monthly_rows:
                    metadata_rows = monthly_rows
                source_titles = {str(row.get("source_title") or "") for row in metadata_rows}
                if len(source_titles) != 1:
                    raise QuestionError(f"元数据条件命中 {len(source_titles)} 份不同文件，不能唯一定位")
                source_title = next(iter(source_titles))
                result = AnswerResult(
                    answer=source_title,
                    choice=None,
                    answer_text=source_title,
                    source_file=str(metadata_rows[0].get("file_name") or ""),
                    dataset_family=str(metadata_rows[0].get("dataset_family") or ""),
                    evidence=[{"resolution": "metadata_fact_query", **evidence(metadata_rows[0])}],
                    plan=plan,
                )
                duration_ms = int((time.perf_counter() - started) * 1000)
                self.repository.audit(request_id, self.actor, question, plan, result, "SUCCESS", None, duration_ms)
                return result

            source = resolve_plan_source(
                question, plan, self.repository.list_sources(), self.repository
            )
            resolved_sheet = resolve_plan_sheet(
                question, plan, self.repository, str(source["file_name"])
            )
            if resolved_sheet:
                plan.sheet_name = resolved_sheet

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
                    selected = resolve_one(rows, [plan.focus, plan.context], plan.sheet_name, self.strict)
                except AmbiguousQuestionError:
                    if not numeric_options(plan.options):
                        raise
                    selected, choice, resolution = lookup_with_options(rows, plan)
                if choice is None and numeric_options(plan.options):
                    matched_choices = matching_numeric_choices(selected.get("value"), plan.options)
                    if len(matched_choices) == 1:
                        choice = matched_choices[0]
                        resolution = "strict_query_option_verified"
                answer_text = plan.options[choice] if choice else format_decimal(selected["value"])
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
                if self.strict:
                    choice, selected_by_option, resolution = compare_with_options(rows, plan)
                else:
                    selected_by_option = {
                        key: resolve_one(rows, [plan.context, label], plan.sheet_name, False)
                        for key, label in plan.options.items()
                    }
                    resolution = "first_match_compatibility"
                units = {str(row.get("unit") or "") for row in selected_by_option.values()}
                if len(units) > 1 and self.strict:
                    raise QuestionError(f"比较项单位不一致，禁止直接比较：{sorted(units)}")
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
                        by_period.setdefault(str(row.get("period_end") or ""), []).append(row)
                    selected_rows = [
                        resolve_one(group, [plan.focus, plan.context], plan.sheet_name, self.strict)
                        for _, group in sorted(by_period.items())
                    ]
                    answer_text = "；".join(
                        f"{row.get('period_end')}={format_decimal(row.get('value'))}"
                        for row in selected_rows
                    )
                else:
                    best_score = max(score for score, _ in scored_analysis_rows)
                    selected_rows = [
                        row for score, row in scored_analysis_rows if score == best_score
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
                    answer_text = str(computed) if isinstance(computed, int) else format_decimal(computed)
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
                        start, end, delta, choice, resolution = delta_with_options(rows, plan)
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
                            rows, [plan.focus, plan.context, plan.from_term], plan.sheet_name, self.strict
                        )
                        end = resolve_one(
                            rows, [plan.focus, plan.context, plan.to_term], plan.sheet_name, self.strict
                        )
                        delta = as_decimal(end["value"]) - as_decimal(start["value"])
                    except AmbiguousQuestionError:
                        if not numeric_options(plan.options):
                            raise
                        start, end, delta, choice, resolution = delta_with_options(rows, plan)
                validate_numeric_quality([start, end], "delta")
                if start.get("unit") != end.get("unit"):
                    raise QuestionError(f"两处数据单位不一致：{start.get('unit')} != {end.get('unit')}")
                if choice is None and numeric_options(plan.options):
                    matched_choices = matching_numeric_choices(delta, plan.options)
                    if len(matched_choices) == 1:
                        choice = matched_choices[0]
                        resolution = "strict_query_option_verified"
                answer_text = plan.options[choice] if choice else format_decimal(delta, 2)
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
                        part for part in (quality_note, quarter_endpoint_explanation(plan)) if part
                    ),
                )
            else:
                raise QuestionError(f"未知题型：{plan.intent}")

            duration_ms = int((time.perf_counter() - started) * 1000)
            self.repository.audit(request_id, self.actor, question, plan, result, "SUCCESS", None, duration_ms)
            return result
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.repository.audit(request_id, self.actor, question, plan, result, "ERROR", str(exc), duration_ms)
            raise


FACT_FILTER_SQL = {
    "metric": ("(metric_code = %s OR metric_name = %s OR metric_name_raw = %s)", 3),
    "entity": ("(entity_code = %s OR entity_name = %s)", 2),
    "region": ("(region_code = %s OR region_name = %s)", 2),
    "period_end": ("period_end = %s", 1),
    "period_basis": ("lower(period_basis) = lower(%s)", 1),
    "scope": ("scope = %s", 1),
    "product_line": ("(product_line = %s OR product_line_name = %s)", 2),
    "measure_type": ("lower(measure_type) = lower(%s)", 1),
    "unit": ("unit = %s", 1),
}


class PostgresRepository:
    def __init__(self, dsn: str):
        if psycopg2 is None:
            raise RuntimeError("当前环境未安装 PostgreSQL 驱动；本项目默认使用 DuckDB")
        self.connection = psycopg2.connect(dsn)

    def close(self) -> None:
        self.connection.close()

    def list_sources(self) -> list[dict[str, Any]]:
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT s.file_name, s.source_title, s.attachment_name, s.file_hash,
                       s.quality_severity, s.quality_status, s.quality_detail, d.dataset_family
                FROM bank_qa.source_file s
                JOIN bank_qa.dataset d ON d.id = s.dataset_id
                ORDER BY s.file_name
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def sheet_profiles(self, file_name: str) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for kind, rows in (
            ("fact", self.facts_for_source(file_name)),
            ("document", self.document_rows(file_name)),
        ):
            for row in rows:
                sheet = str(row.get("source_sheet") or "")
                item = merged.setdefault(
                    sheet,
                    {"source_sheet": sheet, "content_kind": set(), "content": [], "record_count": 0},
                )
                item["content_kind"].add(kind)
                if len(item["content"]) < 40:
                    if kind == "fact":
                        item["content"].extend(
                            str(row.get(field))
                            for field in ("metric_name", "entity_name", "region_name", "product_line_name")
                            if row.get(field)
                        )
                    elif row.get("row_text"):
                        item["content"].append(str(row["row_text"]))
                item["record_count"] += 1
        return [
            {
                **item,
                "content_kind": "|".join(sorted(item["content_kind"])),
                "content": " | ".join(dict.fromkeys(item["content"]))[:12000],
            }
            for item in merged.values()
        ]

    def facts_for_source(
        self,
        file_name: str,
        sheet_name: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["file_name = %s"]
        parameters: list[Any] = [file_name]
        if sheet_name:
            clauses.append(
                "(source_sheet = %s OR position(lower(%s) in lower(source_sheet)) > 0 "
                "OR position(lower(source_sheet) in lower(%s)) > 0)"
            )
            parameters.extend([sheet_name] * 3)
        filters = filters or {}
        from_period, to_period = filters.get("from_period"), filters.get("to_period")
        if from_period or to_period:
            periods = [value for value in (from_period, to_period) if value]
            clauses.append("period_end IN (" + ", ".join(["%s"] * len(periods)) + ")")
            parameters.extend(periods)
        if filters.get("start_period"):
            clauses.append("period_end >= %s")
            parameters.append(filters["start_period"])
        if filters.get("end_period"):
            clauses.append("period_end <= %s")
            parameters.append(filters["end_period"])
        for name, value in filters.items():
            if name in {"from_period", "to_period", "start_period", "end_period"}:
                continue
            if name not in FACT_FILTER_SQL:
                raise QuestionError(f"不允许的 SQL 过滤条件：{name}")
            condition, repeats = FACT_FILTER_SQL[name]
            clauses.append(condition)
            parameters.extend([value] * repeats)
        query = """
                SELECT *
                FROM bank_qa.fact_search_v
                WHERE {}
                ORDER BY source_sheet, source_cell, id
                """.format("\n                  AND ".join(clauses))
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query, parameters)
            return [dict(row) for row in cursor.fetchall()]

    def metric_definitions(self, file_name: str, metric_name: str | None = None) -> list[dict[str, Any]]:
        clauses = ["s.file_name = %s"]
        parameters: list[Any] = [file_name]
        if metric_name:
            clauses.append("(md.metric_name = %s OR md.metric_name ILIKE %s)")
            parameters.extend([metric_name, f"%{metric_name}%"])
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT md.*, s.source_title, d.dataset_family
                FROM bank_qa.metric_definition md
                JOIN bank_qa.source_file s ON s.id = md.source_file_id
                JOIN bank_qa.dataset d ON d.id = s.dataset_id
                WHERE """ + "\n                  AND ".join(clauses) + """
                ORDER BY md.release_year NULLS LAST, md.sequence_no NULLS LAST
                """,
                parameters,
            )
            return [dict(row) for row in cursor.fetchall()]

    def institution_scopes(self, file_name: str, institution: str | None = None) -> list[dict[str, Any]]:
        clauses = ["s.file_name = %s"]
        parameters: list[Any] = [file_name]
        if institution:
            clauses.append("(iscope.institution_type = %s OR iscope.institution_type ILIKE %s)")
            parameters.extend([institution, f"%{institution}%"])
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT iscope.*, s.source_title, d.dataset_family
                FROM bank_qa.institution_scope iscope
                JOIN bank_qa.source_file s ON s.id = iscope.source_file_id
                JOIN bank_qa.dataset d ON d.id = s.dataset_id
                WHERE """ + "\n                  AND ".join(clauses) + """
                ORDER BY iscope.release_year NULLS LAST, iscope.sequence_no NULLS LAST
                """,
                parameters,
            )
            return [dict(row) for row in cursor.fetchall()]

    def release_schedules(self, file_name: str) -> list[dict[str, Any]]:
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT rs.*, s.source_title, d.dataset_family
                FROM bank_qa.release_schedule rs
                JOIN bank_qa.source_file s ON s.id = rs.source_file_id
                JOIN bank_qa.dataset d ON d.id = s.dataset_id
                WHERE s.file_name = %s
                ORDER BY rs.row_order NULLS LAST, rs.source_cell
                """,
                (file_name,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def document_rows(self, file_name: str, sheet_name: str | None = None) -> list[dict[str, Any]]:
        clauses = ["s.file_name = %s"]
        parameters: list[Any] = [file_name]
        if sheet_name:
            clauses.append(
                "(dr.source_sheet = %s OR position(lower(%s) in lower(dr.source_sheet)) > 0 "
                "OR position(lower(dr.source_sheet) in lower(%s)) > 0)"
            )
            parameters.extend([sheet_name] * 3)
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT dr.*, s.source_title, d.dataset_family
                FROM bank_qa.document_row dr
                JOIN bank_qa.source_file s ON s.id = dr.source_file_id
                JOIN bank_qa.dataset d ON d.id = dr.dataset_id
                WHERE """ + "\n                  AND ".join(clauses) + """
                ORDER BY dr.source_sheet, dr.row_number
                """,
                parameters,
            )
            return [dict(row) for row in cursor.fetchall()]

    def metadata_facts(
        self,
        domain_name: str,
        topic_name: str,
        period_end: str,
        metric_name: str,
        dimension: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["domain_name = %s", "topic_name = %s", "period_end = %s::date"]
        parameters: list[Any] = [domain_name, topic_name, period_end]
        clauses.append("(metric_name = %s OR metric_name_raw = %s)")
        parameters.extend([metric_name, metric_name])
        if dimension:
            clauses.append("(entity_name = %s OR region_name = %s)")
            parameters.extend([dimension, dimension])
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT DISTINCT s.file_name, s.source_title, s.quality_severity, s.quality_status,
                       d.dataset_family, f.source_sheet, f.source_cell, f.value, f.unit,
                       f.period_basis
                FROM bank_qa.fact_search_v f
                JOIN bank_qa.source_file s ON s.file_name = f.file_name
                WHERE """ + "\n                  AND ".join(clauses) + """
                ORDER BY s.file_name, f.source_sheet, f.source_cell
                """,
                parameters,
            )
            return [dict(row) for row in cursor.fetchall()]

    def audit(
        self,
        request_id: str,
        actor: str | None,
        question: str,
        plan: QueryPlan | None,
        result: AnswerResult | None,
        status: str,
        error_detail: str | None,
        duration_ms: int,
    ) -> None:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO bank_qa.query_audit
                        (request_id, actor, question, query_plan, generated_sql_name,
                         answer, evidence, status, error_detail, duration_ms)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        request_id,
                        actor,
                        question,
                        Json(asdict(plan)) if plan else None,
                        "filtered_fact_fetch"
                        if plan and (plan.sheet_name or plan.filters)
                        else "source_fact_fetch",
                        result.answer if result else None,
                        Json(result.evidence) if result else None,
                        status,
                        error_detail,
                        duration_ms,
                    ),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


class LocalParquetRepository:
    """Read-only repository over cleaned import files, mainly for offline validation."""

    def __init__(self, clean_dir: Path):
        catalog = pd.read_parquet(clean_dir / "source_catalog.parquet")
        family_meta: dict[str, dict[str, Any]] = {}
        self.sources: list[dict[str, Any]] = []
        source_rows: dict[str, dict[str, Any]] = {}
        for record in catalog.to_dict("records"):
            family = str(record.get("dataset_family") or "")
            meta = {
                "domain_name": text_value(record, "domain_name"),
                "topic_name": text_value(record, "topic_name"),
            }
            family_meta[family] = meta
            file_name = text_value(record, "source_file")
            source_title, attachment_name = split_source_name(str(file_name))
            source = {
                "file_name": file_name,
                "source_title": source_title,
                "attachment_name": attachment_name,
                "file_hash": text_value(record, "source_file_hash"),
                "quality_severity": text_value(record, "quality_severity"),
                "quality_status": text_value(record, "quality_status"),
                "quality_detail": text_value(record, "quality_detail"),
                "dataset_family": family,
                "content_type_code": text_value(record, "content_type_code"),
                "document_function": text_value(record, "document_function"),
                "frequency": text_value(record, "frequency"),
                "evidence_summary": text_value(record, "evidence_summary"),
                "source_sheet_names": text_value(record, "source_sheet_names"),
                "period_start": text_value(record, "period_start"),
                "period_end": text_value(record, "period_end"),
                **meta,
            }
            self.sources.append(source)
            source_rows[str(source["file_name"])] = source

        self.facts: dict[str, list[dict[str, Any]]] = {}
        fact_folders = [clean_dir / "facts"]
        for folder in fact_folders:
            for path in sorted(folder.glob("*.parquet")):
                frame = pd.read_parquet(path)
                for raw in frame.to_dict("records"):
                    row = {key: scalar(raw.get(key)) for key in FACT_FIELDS}
                    source = source_rows[str(raw.get("source_file") or "")]
                    row.update(source)
                    self.facts.setdefault(str(source["file_name"]), []).append(row)

        self.dictionaries = {
            path.stem: [
                self._enriched_row(dict(pd.read_parquet(path).iloc[index]), source_rows)
                for index in range(len(pd.read_parquet(path)))
            ]
            for path in sorted((clean_dir / "dictionaries").glob("*.parquet"))
        }
        self.documents: list[dict[str, Any]] = []
        for folder in (clean_dir / "templates", clean_dir / "references"):
            for path in sorted(folder.glob("*.parquet")):
                frame = pd.read_parquet(path)
                self.documents.extend(
                    self._enriched_row(row, source_rows) for row in frame.to_dict("records")
                )

    @staticmethod
    def _enriched_row(raw: dict[str, Any], source_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
        source = source_rows[str(raw.get("source_file") or "")]
        row = {key: scalar(value) for key, value in raw.items()}
        row.update(source)
        return row

    def close(self) -> None:
        return None

    def audit(self, *args: Any, **kwargs: Any) -> None:
        return None

    def list_sources(self) -> list[dict[str, Any]]:
        return self.sources

    def sheet_profiles(self, file_name: str) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for row in self.facts.get(file_name, []):
            sheet = str(row.get("source_sheet") or "")
            item = merged.setdefault(
                sheet,
                {"source_sheet": sheet, "content_kind": set(), "content": [], "record_count": 0},
            )
            item["content_kind"].add("fact")
            for profile_field in (
                "metric_name", "entity_name", "region_name", "product_line_name"
            ):
                if row.get(profile_field):
                    item["content"].append(str(row[profile_field]))
            item["record_count"] += 1
        for row in self.documents:
            if str(row.get("file_name") or "") != file_name:
                continue
            sheet = str(row.get("source_sheet") or "")
            item = merged.setdefault(
                sheet,
                {"source_sheet": sheet, "content_kind": set(), "content": [], "record_count": 0},
            )
            item["content_kind"].add("document")
            if len(item["content"]) < 40 and row.get("row_text"):
                item["content"].append(str(row["row_text"]))
            item["record_count"] += 1
        return [
            {
                **item,
                "content_kind": "|".join(sorted(item["content_kind"])),
                "content": " | ".join(dict.fromkeys(item["content"]))[:12000],
            }
            for item in merged.values()
        ]

    @staticmethod
    def _sheet_match(row: dict[str, Any], sheet_name: str | None) -> bool:
        if not sheet_name:
            return True
        expected, actual = normalize(sheet_name), normalize(row.get("source_sheet"))
        return expected == actual or expected in actual or actual in expected

    def facts_for_source(
        self,
        file_name: str,
        sheet_name: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = []
        for row in self.facts.get(file_name, []):
            if not self._sheet_match(row, sheet_name):
                continue
            filters = filters or {}
            if filters.get("period_end") and str(row.get("period_end") or "")[:10] != filters["period_end"]:
                continue
            periods = {filters[key] for key in ("from_period", "to_period") if filters.get(key)}
            if periods and str(row.get("period_end") or "")[:10] not in periods:
                continue
            row_period = str(row.get("period_end") or "")[:10]
            if filters.get("start_period") and row_period < filters["start_period"]:
                continue
            if filters.get("end_period") and row_period > filters["end_period"]:
                continue
            if not all(self._field_filter_matches(row, key, value) for key, value in filters.items()
                       if key not in {"period_end", "from_period", "to_period", "start_period", "end_period"}):
                continue
            rows.append(row)
        rows.sort(key=lambda row: (str(row.get("source_sheet") or ""), excel_cell_key(row.get("source_cell"))))
        return rows

    @staticmethod
    def _field_filter_matches(row: dict[str, Any], field: str, value: str) -> bool:
        expected = normalize(value)
        actual_fields = {
            "metric": ("metric_code", "metric_name", "metric_name_raw"),
            "entity": ("entity_code", "entity_name"),
            "region": ("region_code", "region_name"),
            "period_basis": ("period_basis",),
            "scope": ("scope",),
            "product_line": ("product_line", "product_line_name"),
            "measure_type": ("measure_type",),
            "unit": ("unit",),
        }.get(field, (field,))
        return any(expected == normalize(row.get(name)) for name in actual_fields)

    def metric_definitions(self, file_name: str, metric_name: str | None = None) -> list[dict[str, Any]]:
        rows = [
            row for row in self.dictionaries.get("metric_definitions", [])
            if str(row.get("file_name") or "") == file_name
        ]
        if metric_name:
            rows = [row for row in rows if normalize(metric_name) in normalize(row.get("metric_name"))]
        return rows

    def institution_scopes(self, file_name: str, institution: str | None = None) -> list[dict[str, Any]]:
        rows = [
            row for row in self.dictionaries.get("institution_scopes", [])
            if str(row.get("file_name") or "") == file_name
        ]
        if institution:
            rows = [row for row in rows if normalize(institution) in normalize(row.get("institution_type"))]
        return rows

    def release_schedules(self, file_name: str) -> list[dict[str, Any]]:
        return [
            row for row in self.dictionaries.get("release_schedule", [])
            if str(row.get("file_name") or "") == file_name
        ]

    def document_rows(self, file_name: str, sheet_name: str | None = None) -> list[dict[str, Any]]:
        return [
            row
            for row in self.documents
            if str(row.get("file_name") or "") == file_name and self._sheet_match(row, sheet_name)
        ]

    def metadata_facts(
        self,
        domain_name: str,
        topic_name: str,
        period_end: str,
        metric_name: str,
        dimension: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = []
        for source_rows in self.facts.values():
            for row in source_rows:
                if (
                    normalize(row.get("domain_name")) == normalize(domain_name)
                    and normalize(row.get("topic_name")) == normalize(topic_name)
                    and str(row.get("period_end") or "")[:10] == period_end
                    and normalize(row.get("metric_name")) == normalize(metric_name)
                    and (not dimension or normalize(dimension) in {
                        normalize(row.get("entity_name")), normalize(row.get("region_name"))
                    })
                ):
                    rows.append(row)
        return rows


FACT_FIELDS = [
    "dataset_family",
    "source_file",
    "source_sheet",
    "source_cell",
    "period_start",
    "period_end",
    "period_basis",
    "scope",
    "entity_code",
    "entity_name",
    "region_code",
    "region_name",
    "metric_code",
    "metric_name",
    "metric_name_raw",
    "product_line",
    "product_line_name",
    "measure_type",
    "value",
    "unit",
    "scope_version",
    "statistical_scope_version",
    "accounting_basis_version",
    "comparability_flag",
    "quality_flags",
]


def option_map(record: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(record.get(f"option_{key.lower()}") or "").strip()
        for key in ("A", "B", "C", "D")
        if str(record.get(f"option_{key.lower()}") or "").strip()
    }


def evaluate_qa(engine: AnswerEngine, qa_file: Path) -> dict[str, Any]:
    frame = pd.read_excel(qa_file)
    frame = frame[frame["source_type"].eq("excel")]
    results = []
    for record in frame.to_dict("records"):
        try:
            result = engine.answer(str(record["question"]), option_map(record))
            expected = str(record["answer"]).strip().upper()
            if result.choice:
                correct = result.choice == expected
            else:
                correct = abs(as_decimal(result.answer_text) - as_decimal(record["answer_text"])) <= Decimal("0.011")
            results.append(
                {
                    "id": record["id"],
                    "correct": correct,
                    "answer": result.answer,
                    "expected": expected if result.choice else str(record["answer_text"]),
                    "error": None,
                }
            )
        except Exception as exc:
            results.append({"id": record["id"], "correct": False, "answer": None, "expected": str(record.get("answer_text")), "error": str(exc)})
    passed = sum(bool(row["correct"]) for row in results)
    errors = [row for row in results if row["error"]]
    incorrect = [row for row in results if not row["correct"] and not row["error"]]
    return {
        "total": len(results),
        "passed": passed,
        "accuracy": round(passed / len(results), 4) if results else 0,
        "error_count": len(errors),
        "incorrect_count": len(incorrect),
        "errors": errors[:20],
        "incorrect": incorrect[:20],
    }


def answer_text_equivalent(actual: Any, expected: Any) -> bool:
    """Evaluate common source-document aliases as equivalent open-ended answers."""
    alias_pairs = {
        "财产险公司": "产险公司",
    }
    actual_text = str(actual or "")
    expected_text = str(expected or "")
    for source, target in alias_pairs.items():
        actual_text = actual_text.replace(source, target)
        expected_text = expected_text.replace(source, target)
    return normalize(actual_text) == normalize(expected_text)


def evaluate_open(engine: AnswerEngine, qa_file: Path) -> dict[str, Any]:
    frame = pd.read_excel(qa_file)
    results: list[dict[str, Any]] = []
    for record in frame.to_dict("records"):
        answerable = str(record.get("answerable", "")).strip().lower() not in {"false", "0", "nan", ""}
        try:
            result = engine.answer(str(record["question"]))
            rejected = result.plan.route == "reject"
            if not answerable:
                correct = rejected
            elif re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", str(result.answer_text).strip()):
                correct = abs(as_decimal(result.answer_text) - as_decimal(record["expected_answer"])) <= Decimal("0.011")
            elif result.choice:
                correct = answer_text_equivalent(result.answer_text, record["expected_answer"])
            else:
                correct = answer_text_equivalent(result.answer, record["expected_answer"])
            results.append(
                {
                    "id": str(record["id"]),
                    "category": str(record.get("category") or ""),
                    "question_type": str(record.get("question_type") or ""),
                    "correct": correct,
                    "question": str(record["question"]),
                    "answer": result.answer,
                    "answer_text": result.answer_text,
                    "plan": asdict(result.plan),
                    "evidence": result.evidence,
                    "expected": "" if pd.isna(record.get("expected_answer")) else str(record["expected_answer"]),
                    "error": None,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": str(record["id"]),
                    "category": str(record.get("category") or ""),
                    "question_type": str(record.get("question_type") or ""),
                    "correct": False,
                    "question": str(record["question"]),
                    "answer": None,
                    "answer_text": None,
                    "plan": None,
                    "evidence": None,
                    "expected": "" if pd.isna(record.get("expected_answer")) else str(record["expected_answer"]),
                    "error": str(exc),
                }
            )
    passed = sum(bool(row["correct"]) for row in results)

    def group_by(name: str) -> dict[str, dict[str, int]]:
        grouped: dict[str, dict[str, int]] = {}
        for row in results:
            item = grouped.setdefault(row[name], {"total": 0, "passed": 0})
            item["total"] += 1
            item["passed"] += int(row["correct"])
        return grouped

    failures = [row for row in results if not row["correct"]]
    return {
        "total": len(results),
        "passed": passed,
        "accuracy": round(passed / len(results), 4) if results else 0,
        "by_category": group_by("category"),
        "by_question_type": group_by("question_type"),
        "failures": failures,
    }


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="基于 PostgreSQL 的受控 SQL Excel 问答")
    parser.add_argument("--question")
    parser.add_argument("--option-a")
    parser.add_argument("--option-b")
    parser.add_argument("--option-c")
    parser.add_argument("--option-d")
    parser.add_argument("--evaluate", type=Path, help="使用 PostgreSQL 评测 QA Excel")
    parser.add_argument("--evaluate-open", type=Path, help="评测开放 Excel 问答集")
    parser.add_argument("--repository", choices=("postgres", "parquet"), default="postgres")
    parser.add_argument("--clean-dir", type=Path, default=Path("output_excel_reclassified_clean"))
    parser.add_argument("--allow-first-match", action="store_true", help="仅用于兼容缺少期间的旧评测题；生产环境不要开启")
    parser.add_argument("--actor")
    args = parser.parse_args()

    if args.repository == "postgres":
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise SystemExit("缺少环境变量 DATABASE_URL")
        repository: PostgresRepository | LocalParquetRepository = PostgresRepository(dsn)
    else:
        repository = LocalParquetRepository(args.clean_dir.resolve())

    engine = AnswerEngine(
        repository,
        strict=not args.allow_first_match,
        actor=args.actor,
        planner=QwenQueryPlanner.from_env(),
    )
    try:
        if args.evaluate:
            print(json.dumps(evaluate_qa(engine, args.evaluate.resolve()), ensure_ascii=False, indent=2))
            return
        if args.evaluate_open:
            print(json.dumps(evaluate_open(engine, args.evaluate_open.resolve()), ensure_ascii=False, indent=2))
            return
        if not args.question:
            raise SystemExit("请提供 --question 或 --evaluate")
        options = {
            "A": args.option_a,
            "B": args.option_b,
            "C": args.option_c,
            "D": args.option_d,
        }
        result = engine.answer(args.question, options)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
    finally:
        repository.close()


if __name__ == "__main__":
    main()
