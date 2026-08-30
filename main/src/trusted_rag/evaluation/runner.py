"""运行可断点继续的官方选择题评测并生成可下钻指标。"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from trusted_rag.application.query_service import TrustedRagQueryService
from trusted_rag.domain.common import ContractModel
from trusted_rag.domain.enums import RetrievalProfile
from trusted_rag.evaluation.dataset import EvaluationRecord
from trusted_rag.evaluation.option_resolver import DashScopeOptionResolver, OptionResolution
from trusted_rag.infrastructure.artifacts import write_json_atomic


class EvaluationOutcome(ContractModel):
    """一题的答案、来源召回、引用、用量和评分结果。"""

    schema_version: Literal["evaluation_outcome.v1"] = "evaluation_outcome.v1"
    run_id: str
    profile: RetrievalProfile
    question_id: str
    source_type: str
    difficulty: str
    qa_type: str
    reference_answer: str
    predicted_answer: str | None = None
    classification: Literal["correct", "incorrect", "unanswered", "failed"]
    correct: bool
    answer_status: str
    answer_text: str = ""
    refusal_reason: str | None = None
    option_resolution: OptionResolution = Field(default_factory=lambda: OptionResolution(method="empty"))
    expected_source_ids: list[str] = Field(default_factory=list)
    source_recalled: bool | None = None
    reranked_source_recalled: bool | None = None
    citation_source_matched: bool | None = None
    citation_count: int = 0
    retrieval: dict[str, Any] = Field(default_factory=dict)
    answer_usage: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0
    trace_id: str
    error_code: str | None = None


def evaluate_profile(
    *,
    run_id: str,
    profile: RetrievalProfile,
    records: Sequence[EvaluationRecord],
    service: TrustedRagQueryService,
    resolver: DashScopeOptionResolver,
    source_mapping: Mapping[str, Sequence[str]],
    audit_root: Path,
    output_path: Path,
) -> list[EvaluationOutcome]:
    """逐题运行正式问答链路并追加保存结果。

    :param run_id: 评测运行标识。
    :param profile: 本轮唯一检索剖面。
    :param records: 版本化官方评测记录。
    :param service: 已绑定同一知识库快照的问答服务。
    :param resolver: 规则优先的选项映射器。
    :param source_mapping: 题号到期望 source_id 集合的映射。
    :param audit_root: 问答 trace JSONL 目录。
    :param output_path: 当前剖面逐题 JSONL。
    :return: 包含既有断点和本次新增结果的完整列表。
    """
    existing = _read_outcomes(output_path)
    completed_ids = {item.question_id for item in existing}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for record in records:
        if record.question_id in completed_ids:
            continue
        trace_id = f"eval-{run_id}-{profile.value}-{record.question_id}"
        started = time.monotonic()
        try:
            expected = sorted(set(source_mapping.get(record.question_id, ())))
            answer = service.ask(
                record.question,
                trace_id=trace_id,
                source_ids=expected,
                answer_question=_multiple_choice_question(record),
            )
            resolution = resolver.resolve(answer.answer_text, record.options)
            retrieval_audit = _retrieval_audit(audit_root / f"{trace_id}.jsonl")
            retrieved, reranked = _retrieved_sources(retrieval_audit)
            cited = {citation.source_id for citation in answer.citations}
            predicted = resolution.choice
            correct = predicted == record.reference_answer
            classification: Literal["correct", "incorrect", "unanswered", "failed"] = (
                "unanswered" if predicted is None else "correct" if correct else "incorrect"
            )
            outcome = EvaluationOutcome(
                run_id=run_id,
                profile=profile,
                question_id=record.question_id,
                source_type=record.source_type,
                difficulty=record.difficulty,
                qa_type=record.qa_type,
                reference_answer=record.reference_answer,
                predicted_answer=predicted,
                classification=classification,
                correct=correct,
                answer_status=answer.status.value,
                answer_text=answer.answer_text,
                refusal_reason=answer.refusal_reason,
                option_resolution=resolution,
                expected_source_ids=expected,
                source_recalled=bool(set(expected) & retrieved) if expected else None,
                reranked_source_recalled=bool(set(expected) & reranked) if expected else None,
                citation_source_matched=bool(set(expected) & cited) if expected and cited else None,
                citation_count=len(answer.citations),
                retrieval=answer.retrieval.model_dump(mode="json"),
                answer_usage=answer.usage.model_dump(mode="json"),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                trace_id=trace_id,
            )
        except Exception as exc:
            outcome = EvaluationOutcome(
                run_id=run_id,
                profile=profile,
                question_id=record.question_id,
                source_type=record.source_type,
                difficulty=record.difficulty,
                qa_type=record.qa_type,
                reference_answer=record.reference_answer,
                classification="failed",
                correct=False,
                answer_status="failed",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                trace_id=trace_id,
                error_code=type(exc).__name__,
            )
        with output_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False) + "\n")
        existing.append(outcome)
    return existing


def build_source_mapping(
    records: Sequence[EvaluationRecord],
    aliases_path: Path,
) -> dict[str, list[str]]:
    """根据官方证据编号、文件名和标题映射期望来源。

    :param records: 官方评测记录。
    :param aliases_path: 统一语料 ``source_aliases.jsonl``。
    :return: 题号到一个或多个等价 source_id 的映射。
    """
    aliases = [
        json.loads(line)
        for line in aliases_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mapping: dict[str, list[str]] = {}
    for record in records:
        candidates = [item for item in aliases if _profile_matches(record.source_type, str(item["source_profile"]))]
        attachment_id = _attachment_id(record.evidence_reference)
        if attachment_id:
            matched = [
                item
                for item in candidates
                if re.match(rf"^0*{re.escape(attachment_id)}_", str(item["original_file_name"]))
            ]
        else:
            matched = []
        if not matched:
            matched = _fuzzy_name_matches(record.file_label, candidates)
        if not matched:
            matched = _fuzzy_name_matches(record.source_title, candidates)
        mapping[record.question_id] = sorted({str(item["source_id"]) for item in matched})
    return mapping


def summarize_outcomes(
    *,
    run_id: str,
    snapshot_id: str,
    expected_per_profile: int,
    selected_profile: str,
    profile_paths: Mapping[str, Path],
) -> dict[str, Any]:
    """聚合三剖面的准确率、拒答、来源、Token 和延迟。

    :param run_id: 评测运行标识。
    :param snapshot_id: 固定知识库快照。
    :param expected_per_profile: 每个剖面预期题数。
    :param selected_profile: 最终在线系统采用的检索剖面。
    :param profile_paths: 检索剖面到逐题 JSONL 的映射。
    :return: 可写入 JSON 和 Markdown 的汇总。
    """
    profiles: dict[str, Any] = {}
    for profile, path in profile_paths.items():
        outcomes = _read_outcomes(path)
        profiles[profile] = _profile_summary(outcomes, expected_per_profile)
    if selected_profile not in profiles:
        raise ValueError("selected_profile 不在评测剖面中。")
    sample_ids = sorted(
        {
            question_id
            for item in profiles.values()
            for question_id in item["question_ids"]
        }
    )
    return {
        "schema_version": "evaluation_summary.v1",
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "expected_question_count_per_profile": expected_per_profile,
        "selected_profile": selected_profile,
        "sample_question_ids": sample_ids,
        "profiles": profiles,
    }


def write_evaluation_report(output_root: Path, summary: dict[str, Any]) -> None:
    """写入 JSON 汇总和只含最终采用方法的 Markdown 报告。

    :param output_root: 当前评测运行目录。
    :param summary: ``summarize_outcomes`` 返回值。
    :return: 无。
    """
    write_json_atomic(output_root / "summary.json", summary)
    lines = [
        "# 官方 QA 代表性评测报告",
        "",
        f"- 评测运行：`{summary['run_id']}`",
        f"- 知识库快照：`{summary['snapshot_id']}`",
        "- 数据集：官方 300 道选择题，Excel、Word、PDF 各 100 道",
        f"- 本次样本：{len(summary['sample_question_ids'])} 道；"
        "按来源类型、难度和题型形成 9 个分层，每层 2 道，并尽量选择不同源文件",
        f"- 最终检索剖面：`{summary['selected_profile']}`",
        "- 检索：DashScope text-embedding-v3 Dense、本地 Jieba BM25 或 RRF 双路融合",
        "- 精排与回答：qwen3-rerank、证据门禁、可信回答、规则优先选项映射",
        "",
        "| 检索剖面 | 完成 | 正确 | 错误 | 未答 | 失败 | 总体准确率 | 已答准确率 | 来源召回率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for profile, item in summary["profiles"].items():
        lines.append(
            f"| {profile} | {item['completed_count']}/{item['expected_count']} | "
            f"{item['correct_count']} | {item['incorrect_count']} | {item['unanswered_count']} | "
            f"{item['failed_count']} | {_percent(item['accuracy'])} | "
            f"{_percent(item['answered_accuracy'])} | {_percent(item['source_recall'])} |"
        )
    selected = summary["profiles"][summary["selected_profile"]]
    lines.extend(
        [
            "",
            "## 最终剖面分层结果",
            "",
            "| 来源类型 | 样本数 | 正确 | 准确率 |",
            "|---|---:|---:|---:|",
        ]
    )
    for source_type, item in selected["by_source_type"].items():
        lines.append(
            f"| {source_type} | {item['total']} | {item['correct']} | "
            f"{_percent(item['correct'] / item['total'] if item['total'] else 0)} |"
        )
    lines.extend(
        [
            "",
            "## 模型用量与延迟",
            "",
            "| 检索剖面 | 回答模型调用 | 输入 Token | 输出 Token | 总耗时 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for profile, item in summary["profiles"].items():
        lines.append(
            f"| {profile} | {item['answer_model_call_count']} | "
            f"{item['answer_input_tokens']} | {item['answer_output_tokens']} | "
            f"{item['elapsed_ms'] / 1000:.2f}s |"
        )
    unanswered_ids = selected["question_ids_by_classification"]["unanswered"]
    lines.extend(
        [
            "",
            "## 未答明细",
            "",
            "最终剖面未答题号："
            + ("、".join(f"`{item}`" for item in unanswered_ids) if unanswered_ids else "无")
            + "。拒答原因保存在逐题 JSONL 的 `refusal_reason` 字段。",
            "",
            "金额费用未配置推算单价，以逐题 Token、请求标识和 DashScope 控制台账单复核。",
            "",
            "逐题结果保存在各检索剖面的 JSONL 文件中，可按 `question_id`、`trace_id`、"
            "题型、难度和来源类型下钻到检索、精排、引用与模型用量。",
            "",
        ]
    )
    (output_root / "report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _profile_summary(outcomes: Sequence[EvaluationOutcome], expected: int) -> dict[str, Any]:
    correct = sum(item.classification == "correct" for item in outcomes)
    incorrect = sum(item.classification == "incorrect" for item in outcomes)
    unanswered = sum(item.classification == "unanswered" for item in outcomes)
    failed = sum(item.classification == "failed" for item in outcomes)
    source_values = [item.source_recalled for item in outcomes if item.source_recalled is not None]
    reranked_values = [
        item.reranked_source_recalled
        for item in outcomes
        if item.reranked_source_recalled is not None
    ]
    citation_values = [
        item.citation_source_matched
        for item in outcomes
        if item.citation_source_matched is not None
    ]
    by_source: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    by_difficulty: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "correct": 0}
    )
    by_qa_type: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    question_ids: dict[str, list[str]] = {
        "correct": [],
        "incorrect": [],
        "unanswered": [],
        "failed": [],
    }
    for item in outcomes:
        by_source[item.source_type]["total"] += 1
        by_source[item.source_type]["correct"] += int(item.correct)
        by_difficulty[item.difficulty]["total"] += 1
        by_difficulty[item.difficulty]["correct"] += int(item.correct)
        by_qa_type[item.qa_type]["total"] += 1
        by_qa_type[item.qa_type]["correct"] += int(item.correct)
        question_ids[item.classification].append(item.question_id)
    return {
        "expected_count": expected,
        "completed_count": len(outcomes),
        "correct_count": correct,
        "incorrect_count": incorrect,
        "unanswered_count": unanswered,
        "failed_count": failed,
        "accuracy": correct / expected if expected else 0.0,
        "answered_accuracy": correct / (correct + incorrect) if correct + incorrect else None,
        "source_recall": _boolean_rate(source_values),
        "reranked_source_recall": _boolean_rate(reranked_values),
        "citation_source_match_rate": _boolean_rate(citation_values),
        "answer_model_call_count": sum(
            bool(item.answer_usage.get("model_name")) for item in outcomes
        ),
        "answer_input_tokens": sum(int(item.answer_usage.get("input_tokens", 0)) for item in outcomes),
        "answer_output_tokens": sum(int(item.answer_usage.get("output_tokens", 0)) for item in outcomes),
        "resolver_input_tokens": sum(item.option_resolution.input_tokens for item in outcomes),
        "resolver_output_tokens": sum(item.option_resolution.output_tokens for item in outcomes),
        "elapsed_ms": sum(item.elapsed_ms for item in outcomes),
        "by_source_type": dict(sorted(by_source.items())),
        "by_difficulty": dict(sorted(by_difficulty.items())),
        "by_qa_type": dict(sorted(by_qa_type.items())),
        "question_ids": sorted(item.question_id for item in outcomes),
        "question_ids_by_classification": {
            key: sorted(value) for key, value in question_ids.items()
        },
    }


def _retrieval_audit(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    result: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("event_type") == "retrieval":
            result = dict(item.get("payload") or {})
    return result


def _retrieved_sources(payload: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    candidates = {
        str(item["source_id"])
        for item in payload.get("candidates", [])
        if item.get("source_id")
    }
    evidence = {
        str(item["source_id"])
        for item in payload.get("evidence", [])
        if item.get("source_id")
    }
    reranked = {
        str(item["chunk"]["source_id"])
        for item in payload.get("hits", [])
        if item.get("chunk", {}).get("source_id")
    }
    return candidates | evidence, reranked | evidence


def _read_outcomes(path: Path) -> list[EvaluationOutcome]:
    if not path.is_file():
        return []
    return [
        EvaluationOutcome.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _profile_matches(source_type: str, source_profile: str) -> bool:
    if source_type == "excel":
        return source_profile == "excel"
    if source_type == "pdf":
        return source_profile == "pdf"
    return source_profile in {"native_docx", "converted_docx"}


def _multiple_choice_question(record: EvaluationRecord) -> str:
    """构造仅供回答阶段使用且不含标准答案的完整题面。

    :param record: 官方题目和四个候选项。
    :return: 要求模型返回选项字母并引用直接证据的题面。
    """
    options = "\n".join(
        (
            f"A. {record.options['A']}",
            f"B. {record.options['B']}",
            f"C. {record.options['C']}",
            f"D. {record.options['D']}",
        )
    )
    return (
        f"{record.question}\n{options}\n"
        "请只依据给出的证据选择唯一正确选项。answer_text 以“答案：A/B/C/D”开头，"
        "随后简要说明；cited_evidence_ids 必须引用直接支持所选项的证据。"
    )


def _attachment_id(evidence: str) -> str | None:
    match = re.search(r"(?:^|[/\\])(\d+)_", evidence)
    return match.group(1) if match else None


def _fuzzy_name_matches(label: str, candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    target = _normalized_name(label)
    if not target:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in candidates:
        name = _normalized_name(str(item["original_file_name"]))
        coverage = _substring_coverage(target, name)
        if coverage >= 0.8:
            scored.append((coverage, item))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [item for score, item in scored if score >= max(0.8, best - 0.02)]


def _normalized_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", Path(value).stem.casefold(), flags=re.UNICODE)


def _substring_coverage(target: str, source: str) -> float:
    if target in source:
        return 1.0
    previous = [0] * (len(source) + 1)
    longest = 0
    for target_char in target:
        current = [0]
        for index, source_char in enumerate(source, start=1):
            value = previous[index - 1] + 1 if target_char == source_char else 0
            current.append(value)
            longest = max(longest, value)
        previous = current
    return longest / len(target)


def _boolean_rate(values: Sequence[bool | None]) -> float | None:
    concrete = [value for value in values if value is not None]
    return sum(concrete) / len(concrete) if concrete else None


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.2%}"
