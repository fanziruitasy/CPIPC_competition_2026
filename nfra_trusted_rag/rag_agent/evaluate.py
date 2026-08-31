"""对 QA数据.xlsx 中 source_type=='excel' 的 100 道题评测，产出评测报告。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .agent import RagAgent
from .evaluation_validation import validate_reference

CELL_RE = re.compile(r"\b[A-Z]{1,2}\d+\b")


def _evidence_cells(text: Any) -> set[str]:
    if text is None:
        return set()
    return set(CELL_RE.findall(str(text)))


def _answer_cells(result: dict[str, Any]) -> set[str]:
    cells = set()
    for item in result.get("evidence") or []:
        cell = item.get("source_cell")
        if cell:
            cells.add(str(cell))
    return cells


def evaluate(agent: RagAgent, qa_file: Path) -> dict[str, Any]:
    frame = pd.read_excel(qa_file)
    frame = frame[frame["source_type"].eq("excel")].reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    dataset_issues: list[dict[str, Any]] = []

    for record in frame.to_dict("records"):
        question = str(record["question"])
        options = {
            key: str(record.get(f"option_{key.lower()}") or "").strip()
            for key in ("A", "B", "C", "D")
            if str(record.get(f"option_{key.lower()}") or "").strip()
        }
        validation = validate_reference(record, options)
        if validation.status != "valid":
            dataset_issues.append(
                {
                    "id": record["id"],
                    "status": validation.status,
                    "original_expected": validation.original_expected,
                    "corrected_expected": validation.expected,
                    "reasons": list(validation.reasons),
                }
            )
        if not validation.scorable:
            continue
        expected = str(validation.expected or "")
        result = agent.ask(question, options)

        if result["status"] == "answered" and result["choice"]:
            correct = result["choice"] == expected
        else:
            correct = False

        ev_cells = _answer_cells(result)
        q_cells = _evidence_cells(record.get("evidence"))
        cell_hit = bool(ev_cells & q_cells) if (ev_cells or q_cells) else None

        rows.append(
            {
                "id": record["id"],
                "qa_type": record["qa_type"],
                "difficulty": record["difficulty_cn"],
                "correct": correct,
                "status": result["status"],
                "choice": result["choice"],
                "expected": expected,
                "answer_text": result["answer_text"],
                "reason": result["reason"],
                "route": result.get("route"),
                "planner": result.get("planner"),
                "evidence_nonempty": bool(result["evidence"]),
                "evidence_cell_hit": cell_hit,
                "source_file": result["source_file"],
            }
        )

    total = len(rows)
    passed = sum(bool(r["correct"]) for r in rows)
    answered = [r for r in rows if r["status"] == "answered"]
    rejected = [r for r in rows if r["status"] != "answered"]
    ev_nonempty = sum(bool(r["evidence_nonempty"]) for r in answered)
    ev_hit_rows = [r for r in answered if r["evidence_cell_hit"] is True]
    ev_checked = [r for r in answered if r["evidence_cell_hit"] is not None]

    def _accuracy(subset: list[dict[str, Any]]) -> float:
        return (
            round(sum(bool(r["correct"]) for r in subset) / len(subset), 4)
            if subset
            else 0.0
        )

    def _group(rows_: list[dict[str, Any]], key: str) -> dict[str, Any]:
        grouped: dict[str, dict[str, int]] = {}
        for r in rows_:
            item = grouped.setdefault(str(r[key]), {"total": 0, "passed": 0})
            item["total"] += 1
            item["passed"] += int(bool(r["correct"]))
        return {
            name: {
                "total": v["total"],
                "passed": v["passed"],
                "accuracy": round(v["passed"] / v["total"], 4),
            }
            for name, v in sorted(grouped.items())
        }

    failures = [r for r in rows if not r["correct"]]

    summary = {
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "qa_file": str(qa_file),
        "source_total": len(frame),
        "total": total,
        "excluded_invalid": sum(
            issue["status"] == "invalid" for issue in dataset_issues
        ),
        "repaired_references": sum(
            issue["status"] == "repaired" for issue in dataset_issues
        ),
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "answered": len(answered),
        "rejected_or_error": len(rejected),
        "evidence_nonempty_rate": round(ev_nonempty / len(answered), 4)
        if answered
        else 0.0,
        "evidence_cell_hit_rate": round(len(ev_hit_rows) / len(ev_checked), 4)
        if ev_checked
        else 0.0,
        "by_qa_type": _group(rows, "qa_type"),
        "by_difficulty": _group(rows, "difficulty"),
        "by_route": _group(rows, "route"),
        "by_planner": _group(rows, "planner"),
        "failures": [
            {
                "id": r["id"],
                "qa_type": r["qa_type"],
                "difficulty": r["difficulty"],
                "choice": r["choice"],
                "expected": r["expected"],
                "reason": r["reason"],
                "source_file": r["source_file"],
            }
            for r in failures
        ],
        "dataset_issues": dataset_issues,
    }
    return summary


def write_validated_qa(
    qa_file: Path, summary: dict[str, Any], output_path: Path
) -> None:
    """Write a traceable QA workbook without mutating the curated source file."""

    frame = pd.read_excel(qa_file)
    issues = {str(item["id"]): item for item in summary.get("dataset_issues") or []}
    original_answers = frame["answer"].astype(str)
    statuses: list[str] = []
    reasons: list[str] = []
    keep: list[bool] = []
    for index, record in frame.iterrows():
        issue = issues.get(str(record.get("id")))
        if issue is None:
            statuses.append("valid")
            reasons.append("")
            keep.append(True)
            continue
        statuses.append(str(issue["status"]))
        reasons.append("；".join(issue.get("reasons") or []))
        keep.append(issue["status"] != "invalid")
        if issue["status"] == "repaired" and issue.get("corrected_expected"):
            frame.at[index, "answer"] = issue["corrected_expected"]
    frame["original_answer"] = original_answers
    frame["validation_status"] = statuses
    frame["validation_reason"] = reasons
    frame = frame.loc[keep].reset_index(drop=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_excel(output_path, index=False)


def render_markdown(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 银行业监管统计 Excel RAG 问答评测报告\n")
    lines.append(f"- 评测时间：{summary['evaluated_at']}")
    lines.append(f"- 评测集：`{summary['qa_file']}`（source_type=excel）")
    lines.append(
        f"- 原始题目：{summary['source_total']}，排除无效题：{summary['excluded_invalid']}，"
        f"修正标准答案：{summary['repaired_references']}"
    )
    lines.append(
        f"- 题目数：{summary['total']}，答对：{summary['passed']}，**总体准确率：{summary['accuracy']:.2%}**\n"
    )

    lines.append("## 总体指标\n")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 总体准确率 | {summary['accuracy']:.2%} |")
    lines.append(f"| 证据非空率（可溯源） | {summary['evidence_nonempty_rate']:.2%} |")
    lines.append(f"| 证据单元格命中率 | {summary['evidence_cell_hit_rate']:.2%} |")
    lines.append(f"| 拒答/异常数 | {summary['rejected_or_error']} |")

    lines.append("\n## 评测集校验\n")
    if summary["dataset_issues"]:
        lines.append("| id | 处理 | 原答案 | 修正答案 | 原因 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in summary["dataset_issues"]:
            reason = "；".join(item["reasons"]).replace("|", "\\|")
            lines.append(
                f"| {item['id']} | {item['status']} | {item['original_expected']} | "
                f"{item['corrected_expected'] or '-'} | {reason} |"
            )
    else:
        lines.append("未发现题面或标准答案问题。")

    lines.append("\n## 按题型\n")
    lines.append("| 题型 | 题目数 | 答对 | 准确率 |")
    lines.append("| --- | --- | --- | --- |")
    for name, item in summary["by_qa_type"].items():
        lines.append(
            f"| {name} | {item['total']} | {item['passed']} | {item['accuracy']:.2%} |"
        )

    lines.append("\n## 按难度\n")
    lines.append("| 难度 | 题目数 | 答对 | 准确率 |")
    lines.append("| --- | --- | --- | --- |")
    for name, item in summary["by_difficulty"].items():
        lines.append(
            f"| {name} | {item['total']} | {item['passed']} | {item['accuracy']:.2%} |"
        )

    lines.append("\n## 按能力路由\n")
    lines.append("| 路由 | 题目数 | 答对 | 准确率 |")
    lines.append("| --- | ---: | ---: | ---: |")
    for name, item in summary["by_route"].items():
        lines.append(
            f"| {name} | {item['total']} | {item['passed']} | {item['accuracy']:.2%} |"
        )

    lines.append("\n## 未答对明细\n")
    if summary["failures"]:
        lines.append("| id | 题型 | 难度 | 作答 | 期望 | 原因/来源 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for f in summary["failures"]:
            reason = (f["reason"] or "")[:120].replace("|", "\\|")
            lines.append(
                f"| {f['id']} | {f['qa_type']} | {f['difficulty']} | {f['choice']} | "
                f"{f['expected']} | {reason} |"
            )
    else:
        lines.append("无。")
    return "\n".join(lines) + "\n"
