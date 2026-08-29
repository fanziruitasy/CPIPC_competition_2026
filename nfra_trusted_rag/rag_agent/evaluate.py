"""对 QA数据.xlsx 中 source_type=='excel' 的 100 道题评测，产出评测报告。"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .agent import RagAgent

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

    for record in frame.to_dict("records"):
        question = str(record["question"])
        options = {
            key: str(record.get(f"option_{key.lower()}") or "").strip()
            for key in ("A", "B", "C", "D")
            if str(record.get(f"option_{key.lower()}") or "").strip()
        }
        expected = str(record["answer"]).strip().upper()
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
                "execution_route": result.get("execution_route"),
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
        return round(sum(bool(r["correct"]) for r in subset) / len(subset), 4) if subset else 0.0

    def _group(rows_: list[dict[str, Any]], key: str) -> dict[str, Any]:
        grouped: dict[str, dict[str, int]] = {}
        for r in rows_:
            item = grouped.setdefault(str(r[key]), {"total": 0, "passed": 0})
            item["total"] += 1
            item["passed"] += int(bool(r["correct"]))
        return {
            name: {"total": v["total"], "passed": v["passed"],
                   "accuracy": round(v["passed"] / v["total"], 4)}
            for name, v in sorted(grouped.items())
        }

    failures = [r for r in rows if not r["correct"]]

    summary = {
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "qa_file": str(qa_file),
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "answered": len(answered),
        "rejected_or_error": len(rejected),
        "evidence_nonempty_rate": round(ev_nonempty / len(answered), 4) if answered else 0.0,
        "evidence_cell_hit_rate": round(len(ev_hit_rows) / len(ev_checked), 4) if ev_checked else 0.0,
        "by_qa_type": _group(rows, "qa_type"),
        "by_difficulty": _group(rows, "difficulty"),
        "by_route": _group(rows, "route"),
        "by_planner": _group(rows, "planner"),
        "failures": [
            {
                "id": r["id"], "qa_type": r["qa_type"], "difficulty": r["difficulty"],
                "choice": r["choice"], "expected": r["expected"],
                "reason": r["reason"], "source_file": r["source_file"],
            }
            for r in failures
        ],
    }
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 银行业监管统计 Excel RAG 问答评测报告\n")
    lines.append(f"- 评测时间：{summary['evaluated_at']}")
    lines.append(f"- 评测集：`{summary['qa_file']}`（source_type=excel）")
    lines.append(f"- 题目数：{summary['total']}，答对：{summary['passed']}，**总体准确率：{summary['accuracy']:.2%}**\n")

    lines.append("## 总体指标\n")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 总体准确率 | {summary['accuracy']:.2%} |")
    lines.append(f"| 证据非空率（可溯源） | {summary['evidence_nonempty_rate']:.2%} |")
    lines.append(f"| 证据单元格命中率 | {summary['evidence_cell_hit_rate']:.2%} |")
    lines.append(f"| 拒答/异常数 | {summary['rejected_or_error']} |")

    lines.append("\n## 按题型\n")
    lines.append("| 题型 | 题目数 | 答对 | 准确率 |")
    lines.append("| --- | --- | --- | --- |")
    for name, item in summary["by_qa_type"].items():
        lines.append(f"| {name} | {item['total']} | {item['passed']} | {item['accuracy']:.2%} |")

    lines.append("\n## 按难度\n")
    lines.append("| 难度 | 题目数 | 答对 | 准确率 |")
    lines.append("| --- | --- | --- | --- |")
    for name, item in summary["by_difficulty"].items():
        lines.append(f"| {name} | {item['total']} | {item['passed']} | {item['accuracy']:.2%} |")

    lines.append("\n## 按能力路由\n")
    lines.append("| 路由 | 题目数 | 答对 | 准确率 |")
    lines.append("| --- | ---: | ---: | ---: |")
    for name, item in summary["by_route"].items():
        lines.append(f"| {name} | {item['total']} | {item['passed']} | {item['accuracy']:.2%} |")

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


def main() -> None:
    parser = argparse.ArgumentParser(description="评测 Excel RAG 问答")
    parser.add_argument("--qa-file", type=Path, default=Path("QA数据.xlsx"))
    parser.add_argument("--db", type=Path, default=Path("nfra.duckdb"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--allow-first-match", action="store_true", help="兼容缺期间的旧评测题")
    args = parser.parse_args()

    agent = RagAgent(args.db, strict=not args.allow_first_match)
    summary = evaluate(agent, args.qa_file)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / "eval_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.report_dir / "eval_report.md").write_text(render_markdown(summary), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报告已写入：{args.report_dir / 'eval_report.md'} 与 {args.report_dir / 'eval_report.json'}")


if __name__ == "__main__":
    main()
