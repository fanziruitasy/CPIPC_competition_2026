from __future__ import annotations

import unittest
from pathlib import Path

from bank_sql_qa import validated_query_plan
from rag_agent.agent import RagAgent


DB_PATH = Path(__file__).resolve().parents[1] / "nfra.duckdb"


@unittest.skipUnless(DB_PATH.is_file(), "integration database is not available")
class HybridResourceResolutionTests(unittest.TestCase):
    def test_llm_plan_resolves_workbook_and_sheet_without_source_title(self) -> None:
        payload = {
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

        def planner(question: str, options: dict[str, str] | None = None):
            plan = validated_query_plan(payload, options)
            plan.planner = "test-llm"
            return plan

        agent = RagAgent(DB_PATH, query_planner=planner)
        try:
            result = agent.ask(
                "请在保险业的保险业经营主题中，查询2020-01-31"
                "全国保险业汇总的原保险保费收入合计。"
            )
        finally:
            agent.close()

        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["route"], "hybrid")
        self.assertEqual(result["planner"], "test-llm")
        # These constants are test oracles only. The planner receives no
        # source_title/sheet_name and production resolution never reads them.
        self.assertEqual(result["answer_text"], "9080.68")
        self.assertEqual(result["source_title"], "2020年1月保险业经营情况表")
        self.assertEqual(result["evidence"][0]["source_sheet"], "保险业经营数据（月度）")

    def test_analysis_pipeline_executes_a_bounded_trend(self) -> None:
        payload = {
            "intent": "trend",
            "answer_mode": "analysis_pipeline",
            "source_title": "2020年银行业总资产、总负债（月度）",
            "sheet_name": None,
            "source_filters": {},
            "focus": "总资产",
            "context": "银行业金融机构",
            "from_term": None,
            "to_term": None,
            "operation": "trend",
            "operations": ["resolve_source", "resolve_sheet", "query", "group_by_period"],
            "filters": {"start_period": "2020-01-31", "end_period": "2020-12-31"},
            "candidates": [],
            "needs_clarification": False,
            "missing_conditions": [],
        }

        def planner(question: str, options: dict[str, str] | None = None):
            plan = validated_query_plan(payload, options)
            plan.planner = "test-llm"
            return plan

        agent = RagAgent(DB_PATH, query_planner=planner)
        try:
            result = agent.ask("查询2020年银行业金融机构总资产全年趋势")
        finally:
            agent.close()

        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["route"], "analysis_pipeline")
        self.assertEqual(len(result["evidence"]), 12)
        self.assertIn("2020-01-31=2871756", result["answer_text"])
        self.assertIn("2020-12-31=3126737", result["answer_text"])


if __name__ == "__main__":
    unittest.main()
