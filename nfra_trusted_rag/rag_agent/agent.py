"""只使用 DuckDB 仓储的可溯源结构化问答入口。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bank_sql_qa import (
    AmbiguousQuestionError,
    AnswerEngine,
    QwenQueryPlanner,
    QuestionError,
)
from bank_sql_qa import split_source_name

from .knowledge_base import KnowledgeBase


def _evidence_brief(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    brief = []
    for item in evidence or []:
        compact = {}
        for key in (
            "role", "option", "resolution", "metric", "entity", "product_line",
            "measure_type", "value", "unit", "period_end", "period_basis",
            "source_sheet", "source_cell", "source_locator", "source_title",
            "row_number", "answer",
        ):
            if item.get(key) not in (None, ""):
                compact[key] = item[key]
        brief.append(compact)
    return brief


class RagAgent:
    def __init__(
        self,
        db_path: str | Path = "nfra.duckdb",
        *,
        strict: bool = True,
        use_llm_planner: bool = True,
        query_planner: Any | None = None,
    ):
        self.kb = KnowledgeBase(db_path)
        planner = query_planner
        if planner is None and use_llm_planner:
            planner = QwenQueryPlanner.from_env()
        self.engine = AnswerEngine(self.kb.repo, strict=strict, actor=None, planner=planner)

    def ask(
        self,
        question: str,
        options: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = self.engine.answer(question, options)
            if result.plan.route == "reject" or result.plan.answer_mode == "clarify_or_reject":
                return self._reject(result.answer_text)
            return {
                "status": "answered",
                "answer": result.answer,
                "answer_text": result.answer_text,
                "choice": result.choice,
                "route": result.plan.answer_mode,
                "execution_route": result.plan.route,
                "intent": result.plan.intent,
                "planner": result.plan.planner,
                "operations": result.plan.operations,
                "source_title": result.source_file and split_source_name(result.source_file)[0],
                "source_file": result.source_file,
                "dataset_family": result.dataset_family,
                "explanation": result.explanation,
                "evidence": _evidence_brief(result.evidence),
                "reason": None,
            }
        except AmbiguousQuestionError as exc:
            return self._reject(f"澄清：{exc}")
        except QuestionError as exc:
            return self._reject(f"拒答：{exc}")
        except Exception as exc:  # noqa: BLE001
            return self._reject(f"错误：{exc}")

    @staticmethod
    def _reject(reason: str) -> dict[str, Any]:
        return {
            "status": "rejected",
            "answer": None,
            "answer_text": None,
            "choice": None,
            "route": "clarify_or_reject",
            "execution_route": "reject",
            "intent": None,
            "planner": None,
            "operations": [],
            "source_title": None,
            "source_file": None,
            "dataset_family": None,
            "explanation": None,
            "evidence": [],
            "reason": reason,
        }

    def close(self) -> None:
        self.kb.close()
