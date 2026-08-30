"""验证可信回答的引用、数字、规范强度和冲突门禁。"""

from datetime import UTC, datetime

from trusted_rag.answering.gates import post_generation_gate, pre_generation_gate
from trusted_rag.domain.common import LineageMetadata, SourceLocation
from trusted_rag.domain.enums import EvidenceType
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import EvidenceUnit
from trusted_rag.domain.ports import AnswerDraft


def test_supported_answer_passes_gates() -> None:
    """回答数字和规范措辞均出现在引用证据时允许返回。"""
    evidence = _evidence("资本充足率不得低于8%", "8", "%")
    draft = AnswerDraft(
        answer_text="资本充足率不得低于8%。",
        cited_evidence_ids=[evidence.evidence_id],
        model_name="test-model",
    )
    assert pre_generation_gate("资本充足率最低是多少？", [evidence]).allowed
    assert post_generation_gate(draft, [evidence]).allowed


def test_unsupported_number_is_refused() -> None:
    """模型新增证据中不存在的数字时必须拒答。"""
    evidence = _evidence("资本充足率不得低于8%", "8", "%")
    draft = AnswerDraft(
        answer_text="资本充足率不得低于10%。",
        cited_evidence_ids=[evidence.evidence_id],
        model_name="test-model",
    )
    decision = post_generation_gate(draft, [evidence])
    assert not decision.allowed
    assert decision.reasons == ["unsupported_numbers:10%"]


def test_conflicting_same_label_values_are_refused_before_generation() -> None:
    """同一标签出现两个不同事实值时不得交给模型自行选择。"""
    first = _evidence("商业银行 资本充足率：8%", "8", "%")
    second = _evidence("商业银行 资本充足率：10%", "10", "%", suffix="second")
    decision = pre_generation_gate("资本充足率是多少？", [first, second])
    assert not decision.allowed
    assert decision.reasons[0].startswith("conflicting_values:")


def test_answer_can_repeat_date_numbers_from_question() -> None:
    """日期来自用户问题时，不应被数字门禁误判为模型新增事实。"""
    evidence = _evidence("商业银行 资本充足率：11.94%", "11.94", "%")
    draft = AnswerDraft(
        answer_text="截至2020年3月31日，资本充足率为11.94%。",
        cited_evidence_ids=[evidence.evidence_id],
        model_name="test-model",
    )
    decision = post_generation_gate(
        draft,
        [evidence],
        question="2020年3月31日资本充足率是多少？",
    )
    assert decision.allowed


def test_evidence_identifier_digits_are_not_treated_as_answer_facts() -> None:
    """证据 ID 中的十六进制数字片段不属于回答中的数值事实。"""
    evidence = _evidence("原保险保费收入：31739.18亿元", "31739.18", "亿元")
    draft = AnswerDraft(
        answer_text=f"答案：A。数值为31739.18亿元 [{evidence.evidence_id}]。",
        cited_evidence_ids=[evidence.evidence_id],
        model_name="test-model",
    )

    assert post_generation_gate(draft, [evidence]).allowed


def _evidence(text: str, value: str, unit: str, *, suffix: str = "first") -> EvidenceUnit:
    source_id = stable_id("source", suffix)
    document_id = stable_id("document", source_id)
    return EvidenceUnit(
        evidence_id=stable_id("evidence", source_id, text),
        source_id=source_id,
        document_id=document_id,
        evidence_type=EvidenceType.CELL,
        excerpt=text,
        source_value=value,
        unit=unit,
        location=SourceLocation(sheet_name="Sheet1", cell_range="A1"),
        lineage=LineageMetadata(
            run_id="test",
            producer="test",
            producer_version="1",
            created_at=datetime(2026, 8, 30, tzinfo=UTC),
        ),
    )
