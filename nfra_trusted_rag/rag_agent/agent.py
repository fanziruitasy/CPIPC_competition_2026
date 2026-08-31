"""只使用 DuckDB 仓储的可溯源结构化问答入口。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from bank_sql_qa import (
    AmbiguousQuestionError,
    AnswerEngine,
    BailianEmbeddingClient,
    ClarificationQuestionError,
    QwenQueryPlanner,
    QuestionError,
)
from bank_sql_qa import split_source_name
from question_spec import evidence_boundary

from .config import DEFAULT_DB_PATH
from .grounded_answer import GroundedAnswer, QwenGroundedAnswerer
from .knowledge_base import KnowledgeBase


def _evidence_brief(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    brief = []
    for item in evidence or []:
        compact = {}
        for key in (
            "role",
            "option",
            "rank",
            "group_by",
            "group_value",
            "resolution",
            "metric",
            "entity",
            "entity_type",
            "region",
            "product_line",
            "measure_type",
            "value",
            "value_storage",
            "value_display",
            "unit",
            "period_end",
            "period_basis",
            "source_sheet",
            "source_cell",
            "source_locator",
            "source_title",
            "row_number",
            "answer",
            "evidence_kind",
            "coverage_scope",
            "record_count",
            "period_start",
            "available_metrics",
            "available_entities",
            "available_entity_types",
            "available_entity_levels",
            "available_regions",
            "available_region_types",
            "available_product_lines",
            "available_measure_types",
            "available_period_bases",
            "available_units",
            "available_periods",
            "footnotes",
            "dimension_cardinality",
            "listed_values_complete",
            "metric_name",
            "definition",
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
            if item.get(key) not in (None, ""):
                compact[key] = item[key]
        brief.append(compact)
    return brief


def _response(status: str, **values: Any) -> dict[str, Any]:
    """Create every public response from one canonical shape."""
    response = {
        "status": status,
        "answer": None,
        "answer_text": None,
        "choice": None,
        "route": "clarify_or_reject",
        "intent": None,
        "operation": None,
        "planner": None,
        "source_title": None,
        "source_file": None,
        "dataset_family": None,
        "explanation": None,
        "evidence": [],
        "clarification_questions": [],
        "clarification_candidates": [],
        "missing_information": [],
        "evidence_citations": [],
        "answer_backend": None,
        "generation_error": None,
        "reason": None,
        "question_spec": None,
    }
    response.update(values)
    return response


def _result_metadata(
    result: Any, evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    """Map an engine result to the fields shared by every public response."""
    return {
        "route": result.plan.answer_mode,
        "intent": result.plan.intent,
        "operation": result.plan.operation,
        "planner": result.plan.planner,
        "source_title": result.source_file
        and split_source_name(result.source_file)[0],
        "source_file": result.source_file,
        "dataset_family": result.dataset_family,
        "evidence": evidence,
        "question_spec": asdict(result.plan.question_spec)
        if result.plan.question_spec is not None
        else None,
    }


class RagAgent:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        *,
        strict: bool = True,
        use_llm_planner: bool = True,
        query_planner: Any | None = None,
        use_bailian_embeddings: bool = True,
        vector_provider: Any | None = None,
        use_llm_answerer: bool = True,
        answer_generator: Any | None = None,
    ):
        self.kb = KnowledgeBase(db_path)
        planner = query_planner
        if planner is None and use_llm_planner:
            planner = QwenQueryPlanner.from_env()
        embeddings = vector_provider
        if embeddings is None and use_bailian_embeddings:
            embeddings = BailianEmbeddingClient.from_env()
        generator = answer_generator
        if generator is None and use_llm_answerer:
            generator = QwenGroundedAnswerer.from_env()
        self.answer_generator = generator
        self.engine = AnswerEngine(
            self.kb.repo,
            strict=strict,
            actor=None,
            planner=planner,
            vector_provider=embeddings,
            allow_grounded_document_qa=generator is not None,
        )

    def ask(
        self,
        question: str,
        options: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = self.engine.answer(question, options)
            if result.plan.rejected:
                return self._reject(result.answer_text)
            evidence = _evidence_brief(result.evidence)
            metadata = _result_metadata(result, evidence)
            boundary = evidence_boundary(
                result.plan.question_spec, result.evidence, question=question
            )
            if boundary is not None:
                generated = GroundedAnswer(
                    answer_type="refuse",
                    answer=boundary.answer,
                    reason=boundary.reason,
                    missing_information=list(boundary.missing_information),
                    evidence_indices=[],
                    confidence=1.0,
                    backend="deterministic:evidence_boundary",
                )
                return self._grounded_response(result, evidence, generated)
            if self.answer_generator is not None and result.evidence:
                try:
                    generated = self.answer_generator(
                        question=question,
                        evidence=result.evidence,
                        draft_answer=result.answer_text,
                        source_file=result.source_file,
                        options=options,
                    )
                except Exception as exc:  # noqa: BLE001
                    if result.plan.operation == "grounded_document_qa":
                        return _response(
                            "rejected",
                            answer_backend="generation_failed",
                            generation_error=str(exc),
                            reason=f"错误：证据回答生成失败：{exc}",
                            **metadata,
                        )
                    generation_error = str(exc)
                    generated = None
                else:
                    generation_error = None
                if generated is not None:
                    return self._grounded_response(result, evidence, generated)
            else:
                generation_error = None
            return _response(
                "answered",
                answer=result.answer,
                answer_text=result.answer_text,
                choice=result.choice,
                explanation=result.explanation,
                answer_backend="deterministic"
                if self.answer_generator is None
                else "deterministic_fallback",
                generation_error=generation_error,
                **metadata,
            )
        except ClarificationQuestionError as exc:
            return self._clarify(str(exc), exc.questions, exc.candidates)
        except AmbiguousQuestionError as exc:
            return self._reject(f"澄清：{exc}")
        except QuestionError as exc:
            return self._reject(f"拒答：{exc}")
        except Exception as exc:  # noqa: BLE001
            return self._reject(f"错误：{exc}")

    @staticmethod
    def _grounded_response(
        result: Any,
        evidence: list[dict[str, Any]],
        generated: GroundedAnswer,
    ) -> dict[str, Any]:
        common = {
            **_result_metadata(result, evidence),
            "answer_text": generated.answer,
            "evidence_citations": generated.evidence_indices,
            "missing_information": generated.missing_information,
            "answer_backend": generated.backend,
            "reason": generated.reason,
        }
        if generated.answer_type == "clarify":
            return _response(
                "clarification_required",
                clarification_questions=generated.missing_information,
                **common,
            )
        if generated.answer_type == "refuse":
            return _response("rejected", **common)
        preserve_controlled_answer = bool(result.choice) or result.plan.answer_mode in {
            "structured_query",
            "hybrid",
            "analysis_pipeline",
        }
        return _response(
            "answered",
            answer=result.answer if preserve_controlled_answer else generated.answer,
            choice=result.choice,
            explanation=result.explanation,
            **common,
        )

    @staticmethod
    def _clarify(
        reason: str,
        questions: list[str],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return _response(
            "clarification_required",
            clarification_questions=questions,
            clarification_candidates=candidates,
            reason=f"澄清：{reason}",
        )

    @staticmethod
    def _reject(reason: str) -> dict[str, Any]:
        return _response("rejected", reason=reason)

    def close(self) -> None:
        self.kb.close()

    def __enter__(self) -> RagAgent:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
