from __future__ import annotations

import unittest

from bank_sql_qa import parse_question, validated_query_plan


class QueryPlanningTests(unittest.TestCase):
    def test_missing_source_becomes_hybrid_instead_of_immediate_reject(self) -> None:
        plan = parse_question(
            "2020-01-31 的 全国保险业汇总 “原保险保费收入”数值是多少？"
        )

        self.assertEqual(plan.intent, "lookup")
        self.assertEqual(plan.source_title, "")
        self.assertEqual(plan.answer_mode, "hybrid")
        self.assertNotEqual(plan.route, "reject")

    def test_maximum_synonym_routes_to_compare(self) -> None:
        plan = parse_question(
            "根据《2020年1月保险业经营情况表》，财产险和寿险中哪个数值最大？",
            {"A": "财产险", "B": "寿险"},
        )

        self.assertEqual(plan.intent, "compare")
        self.assertEqual(plan.operation, "argmax")
        self.assertEqual(plan.answer_mode, "structured_query")

    def test_lowest_in_source_title_does_not_hijack_document_question(self) -> None:
        plan = parse_question(
            "根据《附件2：巨灾风险损失因子表和最低资本计算模板》，请列出主要栏目。"
        )

        self.assertEqual(plan.intent, "document_row")
        self.assertEqual(plan.answer_mode, "semantic_retrieval")

    def test_llm_plan_allows_source_to_be_absent(self) -> None:
        plan = validated_query_plan(
            {
                "intent": "lookup",
                "answer_mode": "hybrid",
                "source_title": None,
                "sheet_name": None,
                "source_filters": {"domain": "保险业", "topic": "保险业经营"},
                "focus": "原保险保费收入",
                "context": "全国保险业汇总",
                "from_term": None,
                "to_term": None,
                "operation": "lookup",
                "operations": ["resolve_source", "resolve_sheet", "lookup"],
                "filters": {"period_end": "2020-01-31", "product_line": "合计"},
                "candidates": [],
                "needs_clarification": False,
                "missing_conditions": [],
            }
        )

        self.assertEqual(plan.source_title, "")
        self.assertEqual(plan.answer_mode, "hybrid")
        self.assertEqual(plan.source_filters["topic"], "保险业经营")


if __name__ == "__main__":
    unittest.main()
