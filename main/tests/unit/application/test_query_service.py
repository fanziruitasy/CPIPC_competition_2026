"""验证统一查询服务对评测完整题面和原始题干的边界处理。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from trusted_rag.application.query_service import TrustedRagQueryService
from trusted_rag.retrieval.query_planner import ControlledQueryPlanner


def test_multiple_choice_options_participate_in_planning_but_not_original_question() -> None:
    """选项应消除规划歧义，但不得扩大可信门禁认可的原始问题数字范围。"""
    captured_questions: list[str] = []

    def model(question: str, _plan: object) -> dict[str, object]:
        captured_questions.append(question)
        return {
            "route": "document",
            "intent": "regulation_lookup",
            "semantic_queries": [question],
            "filters": {},
            "structured_operations": [],
            "clarification_required": False,
            "rejection_reason": None,
            "reason": "选项完整，可以检索法规证据",
        }

    planner = ControlledQueryPlanner(
        model=model,
        clock=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )
    retrieval = Mock()
    retrieval_result = Mock()
    retrieval_result.model_dump.return_value = {}
    retrieval.retrieve.return_value = retrieval_result
    answering = Mock()
    answer = Mock()
    answer.model_dump.return_value = {}
    answering.answer.return_value = answer
    audit = Mock()
    snapshot = SimpleNamespace(knowledge_base_id="nfra-regulations")
    service = TrustedRagQueryService(
        planner=planner,
        retrieval=retrieval,
        answering=answering,
        audit=audit,
        snapshot=snapshot,
    )
    question = "根据《消费金融公司管理办法》，下列哪项表述正确？"
    complete_question = f"{question}\nA. 选项一\nB. 选项二\nC. 选项三\nD. 选项四"

    service.ask(
        question,
        trace_id="trace-multiple-choice",
        answer_question=complete_question,
    )

    assert captured_questions == [complete_question]
    planned = retrieval.retrieve.call_args.args[1]
    assert planned.original_query == question
    assert planned.semantic_queries == [
        f"{question}\nA. 选项一",
        f"{question}\nB. 选项二",
        f"{question}\nC. 选项三",
        f"{question}\nD. 选项四",
    ]
