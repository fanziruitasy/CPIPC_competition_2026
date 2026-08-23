from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_QA_PATH = Path(r"D:\金融科技大赛\Code\data\QA数据.xlsx")
DEFAULT_RAW_ROOT = Path(r"D:\金融科技大赛\Code\data\nfra_page_attachments_500")
DEFAULT_OUT_DIR = Path(
    r"D:\2\文件\西交\比赛\金融科技创新大赛\docling_rag_chunk_index_retrieval_eval\eval\oos_insufficient_50"
)


QUESTION_TEMPLATES = [
    {
        "oos_type": "future_or_latest_version",
        "template": "截至2026年8月，{hint}是否已经发布了最新修订版？请列出修订后的新增条款和生效日期。",
        "rationale": "资料库只包含已入库文件，不能证明2026年8月时点的最新修订状态。",
    },
    {
        "oos_type": "institution_specific_case",
        "template": "某城商行计划按照{hint}调整内部制度，请判断该行现有方案是否合规，并说明需要整改的具体条款。",
        "rationale": "问题需要银行内部方案材料，现有资料库没有该机构的具体方案。",
    },
    {
        "oos_type": "nonexistent_attachment",
        "template": "请根据{hint}的附件99，说明监管报送字段“专项整改完成率”的填报口径。",
        "rationale": "题干指定的附件99不属于当前资料库内容，不能据此作答。",
    },
    {
        "oos_type": "unpublished_feedback",
        "template": "{hint}在征求意见阶段共收到多少条反馈？监管部门最终采纳了哪些意见？",
        "rationale": "资料库文件通常不包含征求意见反馈明细和采纳过程。",
    },
    {
        "oos_type": "external_comparison",
        "template": "请比较{hint}与2026年欧盟同类监管规则的差异，并指出我国规则在哪些方面更严格。",
        "rationale": "需要外部法规和2026年比较资料，当前资料库不足以支持。",
    },
    {
        "oos_type": "forecast_or_prediction",
        "template": "根据{hint}，预测2027年相关机构的资本充足率均值会提高多少个百分点？",
        "rationale": "资料库中的制度文件不能直接支持未来量化预测。",
    },
    {
        "oos_type": "missing_numeric_fact",
        "template": "{hint}是否披露了2025年第四季度所有适用机构的实际报送错误率？如果披露，请给出具体百分比。",
        "rationale": "问题要求特定时期统计结果，资料库制度文本无法提供该实测数据。",
    },
    {
        "oos_type": "legal_final_judgement",
        "template": "某金融机构违反{hint}中的相关要求时，是否一定会被吊销许可证？请给出最终处罚结论。",
        "rationale": "处罚结论取决于具体事实和执法裁量，资料库不能替代个案法律判断。",
    },
    {
        "oos_type": "implementation_status",
        "template": "{hint}发布后，已有多少家机构完成系统改造并通过监管验收？请列出机构名单。",
        "rationale": "问题要求实施进展和机构名单，当前资料库未提供这类动态数据。",
    },
    {
        "oos_type": "source_conflict_resolution",
        "template": "如果{hint}与某地方金融监管局2026年发布的操作细则冲突，应优先适用哪一份文件？",
        "rationale": "需要未入库的地方细则和具体冲突条款，不能仅凭当前资料库判断。",
    },
]


GLOBAL_OOS_QUESTIONS = [
    ("global_no_target", "请查询南京银行2026年第二季度内部审计发现的RAG系统缺陷数量。"),
    ("global_no_target", "请判断某客户李某的贷款申请是否符合监管要求，并给出审批结论。"),
    ("global_no_target", "请给出2026年所有商业银行因数据安全事件被罚款的完整名单和金额。"),
    ("global_no_target", "请预测未来三年消费金融行业投诉率的变化趋势，并给出精确百分比。"),
    ("global_no_target", "请说明当前比赛团队前端WebUI的线上访问地址和管理员密码。"),
    ("global_no_target", "请查询监管部门尚未公开发布的内部会议纪要中对资本监管的最新表述。"),
    ("global_no_target", "请判断某保险公司正在准备的未公开偿付能力报告是否存在数据造假。"),
    ("global_no_target", "请列出2026年8月以后即将废止的全部银行保险监管规范性文件。"),
    ("global_no_target", "请回答金融监管总局下一次新闻发布会会宣布哪些政策。"),
    ("global_no_target", "请比较本资料库与人民银行全部历史规章，找出所有冲突条款。"),
]


def normalize_title(text: Any) -> str:
    value = "" if pd.isna(text) else str(text).strip()
    value = re.sub(r"\s+", " ", value)
    return value


def make_hint(row: dict[str, Any]) -> str:
    file_label = normalize_title(row.get("file_label"))
    source_title = normalize_title(row.get("source_title"))
    if file_label:
        stem = re.sub(r"\.(docx?|pdf)$", "", file_label, flags=re.IGNORECASE)
        stem = re.sub(r"^附件[0-9一二三四五六七八九十\-：:、]*", "", stem).strip()
        if stem:
            return f"《{stem}》"
    if source_title:
        return f"《{source_title}》"
    return "该资料"


def load_source_rows(qa_path: Path) -> list[dict[str, Any]]:
    df = pd.read_excel(qa_path)
    required = {"source_type", "source_title", "file_label"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"QA file missing columns: {sorted(missing)}")

    sub = df[df["source_type"].isin(["word", "pdf"])].copy()
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in sub.to_dict(orient="records"):
        key = (
            normalize_title(row.get("source_type")),
            normalize_title(row.get("source_title")),
            normalize_title(row.get("file_label")),
        )
        if key not in grouped:
            grouped[key] = {
                "source_type_hint": key[0],
                "related_source_title": key[1],
                "related_file_label": key[2],
                "hint": make_hint(row),
            }
    return list(grouped.values())


def raw_file_exists(raw_root: Path, file_label: str) -> bool:
    if not file_label:
        return False
    matches = list(raw_root.rglob(f"*{file_label}"))
    return bool(matches)


def build_records(source_rows: list[dict[str, Any]], raw_root: Path, total: int = 50) -> list[dict[str, Any]]:
    if not source_rows:
        raise ValueError("No word/pdf source rows found.")

    records: list[dict[str, Any]] = []
    counters: defaultdict[str, int] = defaultdict(int)

    idx = 0
    while len(records) < total - len(GLOBAL_OOS_QUESTIONS):
        src = source_rows[idx % len(source_rows)]
        tpl = QUESTION_TEMPLATES[idx % len(QUESTION_TEMPLATES)]
        counters[tpl["oos_type"]] += 1
        question = tpl["template"].format(hint=src["hint"])
        record = {
            "id": f"OOS{len(records) + 1:03d}",
            "dataset": "oos_insufficient_docling_word_pdf",
            "source_type_hint": src["source_type_hint"],
            "question": question,
            "expected_behavior": "refuse_or_clarify",
            "expected_answer": "INSUFFICIENT",
            "acceptable_response": "应说明当前资料库证据不足，不能直接作答；如适用，可提示需要补充外部资料、最新文件、机构个案材料或具体报送数据。",
            "oos_type": tpl["oos_type"],
            "related_source_title": src["related_source_title"],
            "related_file_label": src["related_file_label"],
            "source_hint_visible_in_question": src["hint"],
            "qa_extra_fields_used_for_generation_only": True,
            "raw_file_label_exists": raw_file_exists(raw_root, src["related_file_label"]),
            "should_force_choice": False,
            "metric_target": "out_of_scope_refusal_or_clarification_rate",
            "rationale": tpl["rationale"],
        }
        records.append(record)
        idx += 1

    for oos_type, question in GLOBAL_OOS_QUESTIONS:
        records.append(
            {
                "id": f"OOS{len(records) + 1:03d}",
                "dataset": "oos_insufficient_docling_word_pdf",
                "source_type_hint": "global",
                "question": question,
                "expected_behavior": "refuse_or_clarify",
                "expected_answer": "INSUFFICIENT",
                "acceptable_response": "应说明当前资料库没有足够证据，不能编造答案；可以要求用户补充资料、时间范围、机构材料或外部来源。",
                "oos_type": oos_type,
                "related_source_title": "",
                "related_file_label": "",
                "source_hint_visible_in_question": "",
                "qa_extra_fields_used_for_generation_only": False,
                "raw_file_label_exists": False,
                "should_force_choice": False,
                "metric_target": "out_of_scope_refusal_or_clarification_rate",
                "rationale": "问题没有可定位的入库资料，或要求资料库外的动态、内部、个人/机构个案信息。",
            }
        )

    return records[:total]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = list(records[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: defaultdict[str, int] = defaultdict(int)
    by_source_type: defaultdict[str, int] = defaultdict(int)
    for record in records:
        by_type[record["oos_type"]] += 1
        by_source_type[record["source_type_hint"]] += 1
    return {
        "total": len(records),
        "expected_answer": "INSUFFICIENT",
        "should_force_choice": False,
        "by_oos_type": dict(sorted(by_type.items())),
        "by_source_type_hint": dict(sorted(by_source_type.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build 50 OOS/insufficient QA records for refusal-rate evaluation.")
    parser.add_argument("--qa-path", type=Path, default=DEFAULT_QA_PATH)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--total", type=int, default=50)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    source_rows = load_source_rows(args.qa_path)
    records = build_records(source_rows, args.raw_root, total=args.total)

    jsonl_path = args.out_dir / "oos_insufficient_50.jsonl"
    csv_path = args.out_dir / "oos_insufficient_50.csv"
    report_path = args.out_dir / "oos_insufficient_50_summary.json"

    write_jsonl(jsonl_path, records)
    write_csv(csv_path, records)
    summary = summarize(records)
    summary.update(
        {
            "qa_path": str(args.qa_path),
            "raw_root": str(args.raw_root),
            "jsonl_path": str(jsonl_path),
            "csv_path": str(csv_path),
            "usage_note": "用于 trusted_qa/out_of_scope_eval 模式；禁止 force-choice 二次强制选择。",
        }
    )
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
