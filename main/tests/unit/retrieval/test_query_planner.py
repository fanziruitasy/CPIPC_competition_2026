"""验证规则优先查询规划和受控模型补丁。"""

from datetime import UTC, datetime

from trusted_rag.application.query_service import _scope_plan
from trusted_rag.domain.enums import QueryIntent, QueryRoute, StructuredOperationType
from trusted_rag.retrieval.query_planner import ControlledQueryPlanner
from trusted_rag.retrieval.service import _document_filters

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def test_numeric_question_uses_rules_and_structured_route() -> None:
    """显式指标、期间和单位应直接形成确定性事实查询。"""
    planner = ControlledQueryPlanner(
        metrics=["商业银行不良贷款余额"],
        entities=["商业银行"],
        clock=lambda: NOW,
    )
    plan = planner.plan(
        "2024年商业银行不良贷款余额是多少亿元？",
        knowledge_base_id="nfra-regulations",
        trace_id="trace-001",
    )
    assert plan.route is QueryRoute.STRUCTURED
    assert plan.intent is QueryIntent.STATISTIC_LOOKUP
    assert plan.filters.periods == ["2024-12-31"]
    assert plan.filters.units == ["亿元"]
    assert plan.structured_operations[0].operation is StructuredOperationType.LOOKUP
    assert plan.planner_mode == "rules_only"


def test_regulation_question_uses_document_route() -> None:
    """法规条款问题应进入文档检索且不构造事实操作。"""
    planner = ControlledQueryPlanner(clock=lambda: NOW)
    plan = planner.plan(
        "商业银行资本管理办法第十条规定了什么？",
        knowledge_base_id="nfra-regulations",
        trace_id="trace-002",
    )
    assert plan.route is QueryRoute.DOCUMENT
    assert plan.intent is QueryIntent.CLAUSE_LOOKUP
    assert plan.structured_operations == []


def test_metric_name_with_ratio_or_average_is_still_lookup() -> None:
    """指标名称含“比例、平均”等词时不得误判为派生计算。"""
    planner = ControlledQueryPlanner(
        metrics=["流动性比例", "平均资产利润率"],
        clock=lambda: NOW,
    )
    ratio = planner.plan(
        "2024年流动性比例是多少？",
        knowledge_base_id="kb",
        trace_id="trace-ratio",
    )
    average = planner.plan(
        "2024年平均资产利润率是多少？",
        knowledge_base_id="kb",
        trace_id="trace-average",
    )
    assert ratio.structured_operations[0].operation is StructuredOperationType.LOOKUP
    assert average.structured_operations[0].operation is StructuredOperationType.LOOKUP


def test_ambiguous_question_calls_model_and_validates_patch() -> None:
    """只有低置信度问题调用模型，返回字段必须通过白名单契约。"""
    calls: list[str] = []

    def model(question: str, _plan: object) -> dict[str, object]:
        calls.append(question)
        return {
            "route": "document",
            "intent": "general_lookup",
            "semantic_queries": ["银行业监管处罚相关规定"],
            "filters": {},
            "structured_operations": [],
            "clarification_required": False,
            "rejection_reason": None,
            "reason": "已消解指代为监管处罚主题",
        }

    planner = ControlledQueryPlanner(model=model, clock=lambda: NOW)
    plan = planner.plan("上述相关情况是什么？", knowledge_base_id="kb", trace_id="trace-003")
    assert calls == ["上述相关情况是什么？"]
    assert plan.planner_mode == "rules_with_model"
    assert plan.semantic_queries == ["银行业监管处罚相关规定"]


def test_model_cannot_inject_unknown_or_sql_fields() -> None:
    """模型不得把 SQL 或未知字段带入执行计划。"""

    def model(_question: str, _plan: object) -> dict[str, object]:
        return {
            "route": "document",
            "intent": "general_lookup",
            "semantic_queries": ["查询"],
            "filters": {},
            "structured_operations": [],
            "clarification_required": False,
            "rejection_reason": None,
            "reason": "测试",
            "sql": "DROP TABLE table_facts",
        }

    planner = ControlledQueryPlanner(model=model, clock=lambda: NOW)
    plan = planner.plan("上述情况？", knowledge_base_id="kb", trace_id="trace-004")
    assert plan.planner_mode == "rules_only"
    assert plan.clarification_required
    assert plan.structured_operations == []
    assert plan.planner_reasons[-1] == "model_planner_failed:ValidationError"


def test_document_retrieval_does_not_hard_filter_dictionary_matches() -> None:
    """机构、指标、期间和单位仅作为语义提示，不得误排除文档候选。"""
    planner = ControlledQueryPlanner(
        metrics=["资本充足率"],
        entities=["商业银行"],
        clock=lambda: NOW,
    )
    plan = planner.plan(
        "商业银行资本充足率的监管要求是什么？",
        knowledge_base_id="kb",
        trace_id="trace-filter",
    )
    filters = _document_filters(plan)
    assert plan.route is QueryRoute.DOCUMENT
    assert filters.metrics == []
    assert filters.entities == []
    assert filters.periods == []
    assert filters.units == []


def test_incidental_metric_and_unit_substrings_do_not_force_numeric_route() -> None:
    """“非寿险、本人”等普通文本命中词典片段时仍应走文档检索。"""
    planner = ControlledQueryPlanner(
        metrics=["寿险"],
        clock=lambda: NOW,
    )
    plan = planner.plan(
        "本人对非寿险业务准备金材料的主要内容进行说明。",
        knowledge_base_id="kb",
        trace_id="trace-incidental",
    )
    assert plan.route is QueryRoute.DOCUMENT
    assert plan.structured_operations == []


def test_month_period_and_person_word_are_parsed_without_substring_false_positive() -> None:
    """年月应落到月末，“人身保险”中的“人”不得成为人数单位。"""
    planner = ControlledQueryPlanner(
        metrics=["原保险保费收入"],
        entities=["人身保险公司"],
        clock=lambda: NOW,
    )
    plan = planner.plan(
        "2023年10月人身保险公司的原保险保费收入是多少？",
        knowledge_base_id="kb",
        trace_id="trace-month",
    )

    assert plan.filters.periods == ["2023-10-31"]
    assert plan.filters.units == []


def test_explicit_source_scope_keeps_metric_period_and_removes_title_entity() -> None:
    """结构化问题限定附件后应保留指标期间并避免标题机构误过滤行。"""
    planner = ControlledQueryPlanner(
        metrics=["原保险保费收入"],
        entities=["人身保险公司"],
        clock=lambda: NOW,
    )
    plan = planner.plan(
        "2023年10月人身保险公司的原保险保费收入是多少？",
        knowledge_base_id="kb",
        trace_id="trace-source-scope",
    )
    scoped = _scope_plan(plan, ["source_1234567890abcdef12345678"])

    assert scoped.filters.source_ids == ["source_1234567890abcdef12345678"]
    assert scoped.filters.metrics == ["原保险保费收入"]
    assert scoped.filters.periods == ["2023-10-31"]
    assert scoped.filters.entities == []
    assert scoped.structured_operations[0].entity is None
