"""实现证据充分性、冲突、数字和规范强度的确定性门禁。"""

from __future__ import annotations

import re
from collections.abc import Sequence

from trusted_rag.domain.knowledge import EvidenceUnit
from trusted_rag.domain.ports import AnswerDraft
from trusted_rag.retrieval.contracts import EvidenceGateDecision

_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?\d+(?:[,.]\d+)*(?:%|％)?(?![A-Za-z0-9_])"
)
_NORMATIVE_TERMS = ("原则上不得", "不得", "应当", "必须", "严禁", "可以")


def pre_generation_gate(question: str, evidence: Sequence[EvidenceUnit]) -> EvidenceGateDecision:
    """在模型调用前阻断无证据和高风险未复核数字。

    :param question: 用户问题。
    :param evidence: 检索和事实查询合并后的证据。
    :return: 是否允许调用回答模型及原因。
    """
    if not evidence:
        return EvidenceGateDecision(allowed=False, reasons=["no_evidence"])
    numeric = bool(_NUMBER.search(question) or re.search(r"多少|数值|金额|余额|比例|计算", question))
    if numeric and all(item.quality.requires_manual_review for item in evidence):
        return EvidenceGateDecision(allowed=False, reasons=["numeric_evidence_requires_manual_review"])
    conflicts = _conflicting_values(evidence)
    if conflicts:
        return EvidenceGateDecision(allowed=False, reasons=[f"conflicting_values:{item}" for item in conflicts])
    return EvidenceGateDecision(allowed=True)


def post_generation_gate(
    draft: AnswerDraft,
    evidence: Sequence[EvidenceUnit],
    *,
    question: str = "",
) -> EvidenceGateDecision:
    """校验回答引用、数字和规范强度均受证据支持。

    :param draft: 模型生成草稿。
    :param evidence: 模型获得的全部证据。
    :param question: 用户原始问题，允许回答复述其中已有的日期和数值。
    :return: 是否允许形成最终回答及拒绝原因。
    """
    if draft.refusal_reason:
        return EvidenceGateDecision(allowed=False, reasons=["model_refused", draft.refusal_reason])
    by_id = {item.evidence_id: item for item in evidence}
    cited = [by_id[item] for item in draft.cited_evidence_ids if item in by_id]
    if not draft.answer_text or not draft.cited_evidence_ids:
        return EvidenceGateDecision(allowed=False, reasons=["answer_or_citations_missing"])
    if len(cited) != len(set(draft.cited_evidence_ids)):
        return EvidenceGateDecision(allowed=False, reasons=["unknown_or_duplicate_citation"])
    source_text = " ".join(
        f"{item.excerpt} {item.source_value or ''} {item.unit or ''}" for item in cited
    )
    unsupported = _numbers(draft.answer_text) - _numbers(f"{source_text} {question}")
    if unsupported:
        return EvidenceGateDecision(
            allowed=False,
            reasons=["unsupported_numbers:" + ",".join(sorted(unsupported))],
        )
    for term in _NORMATIVE_TERMS:
        if term in draft.answer_text and term not in source_text:
            return EvidenceGateDecision(allowed=False, reasons=[f"unsupported_normative_strength:{term}"])
    return EvidenceGateDecision(allowed=True)


def _numbers(text: str) -> set[str]:
    return {
        match.group(0).replace(",", "").replace("，", "").replace("％", "%")
        for match in _NUMBER.finditer(text)
    }


def _conflicting_values(evidence: Sequence[EvidenceUnit]) -> list[str]:
    groups: dict[str, set[tuple[str, str | None]]] = {}
    for item in evidence:
        if item.source_value is None:
            continue
        label = item.excerpt.split("：", 1)[0].strip()
        groups.setdefault(label, set()).add((item.source_value, item.unit))
    return sorted(label for label, values in groups.items() if label and len(values) > 1)
