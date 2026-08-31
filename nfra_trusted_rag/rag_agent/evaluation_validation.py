"""Preflight validation for generated multiple-choice evaluation records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


GENERIC_PERIOD_LABELS = {
    "季度",
    "年-季度",
    "季度-季度",
    "本年累计/截至当期",
}


@dataclass(frozen=True)
class ReferenceValidation:
    status: str
    expected: str | None
    original_expected: str
    reasons: tuple[str, ...] = ()

    @property
    def scorable(self) -> bool:
        return self.status != "invalid"


def _has_specific_period(question: str) -> bool:
    return bool(
        re.search(
            r"\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月|"
            r"(?:第)?[一二三四1234]季度|[一二三四]季(?:度)?末|年末",
            question,
        )
    )


def _quoted_terms(question: str) -> list[str]:
    return [item.strip() for item in re.findall(r"“([^”]+)”", question)]


def _comparison_semantic_class(label: str) -> str:
    if any(token in label for token in ("率", "占比", "比例")):
        return "ratio"
    return "amount"


def _period_semantic_class(label: str) -> str | None:
    if any(token in label for token in ("总资产", "总负债", "保险金额", "余额")):
        return "stock"

        
    if any(token in label for token in ("收入", "支出", "赔付", "新增交费")):
        return "flow"
    return None


def static_reference_issues(
    record: dict[str, Any], options: dict[str, str]
) -> list[str]:
    """Reject malformed generated questions before they affect accuracy."""

    question = str(record.get("question") or "")
    qa_type = str(record.get("qa_type") or "")
    source_title = str(record.get("source_title") or "")
    issues: list[str] = []

    if qa_type == "表格取数" and "季度" in source_title and not _has_specific_period(
        question
    ):
        issues.append("季度取数题未指定具体季度或日期")

    if qa_type == "表格计算" and "从" in question and "到" in question:
        endpoints = [term for term in _quoted_terms(question) if term in GENERIC_PERIOD_LABELS]
        if endpoints:
            issues.append("计算题把统计口径标签当成起止期间：" + "、".join(endpoints))

    if qa_type == "表格比较" and options:
        classes = {_comparison_semantic_class(label) for label in options.values()}
        if len(classes) > 1:
            issues.append("比较选项混用了比率和金额口径")
        period_classes = {
            item
            for label in options.values()
            if (item := _period_semantic_class(label)) is not None
        }
        if len(period_classes) > 1:
            issues.append("比较选项混用了时点存量和期间流量口径")
    return issues


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _evidence_option_values(
    record: dict[str, Any], options: dict[str, str]
) -> tuple[dict[str, Decimal] | None, str | None]:
    evidence = str(record.get("evidence") or "")
    values: dict[str, Decimal] = {}
    for key, label in options.items():
        matches = re.findall(
            rf"{re.escape(label)}\s*=\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*\([A-Z]{{1,3}}\d+\)",
            evidence,
        )
        parsed = {_decimal(item) for item in matches}
        parsed.discard(None)
        if not parsed:
            return None, f"选项“{label}”缺少原始证据"
        if len(parsed) > 1:
            return None, f"选项“{label}”对应多个不同数值，口径不唯一"
        values[key] = parsed.pop()
    return values, None


def _validated_comparison_answer(
    record: dict[str, Any], options: dict[str, str]
) -> tuple[str | None, str | None]:
    values, issue = _evidence_option_values(record, options)
    if issue or values is None:
        return None, issue
    question = str(record.get("question") or "")
    reverse = not any(token in question for token in ("最低", "最小", "最少"))
    ordered = sorted(values, key=lambda key: values[key], reverse=reverse)
    if len(ordered) > 1 and values[ordered[0]] == values[ordered[1]]:
        return None, "比较题存在并列答案"
    return ordered[0], None


def validate_reference(
    record: dict[str, Any],
    options: dict[str, str],
) -> ReferenceValidation:
    original = str(record.get("answer") or "").strip().upper()
    issues = static_reference_issues(record, options)
    if original not in options:
        issues.append("标准答案不在有效选项中")
    if issues:
        return ReferenceValidation("invalid", None, original, tuple(issues))

    if str(record.get("qa_type") or "") == "表格比较":
        answer, comparison_issue = _validated_comparison_answer(record, options)
        if comparison_issue:
            return ReferenceValidation(
                "invalid", None, original, (comparison_issue,)
            )
        if answer and answer != original:
            return ReferenceValidation(
                "repaired",
                answer,
                original,
                (f"标准答案与同口径证据计算结果不一致：{original} -> {answer}",),
            )
    return ReferenceValidation("valid", original, original)
