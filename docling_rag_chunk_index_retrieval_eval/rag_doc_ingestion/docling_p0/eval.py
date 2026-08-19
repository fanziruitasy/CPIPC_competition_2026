from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .indexes import hash_embedding
from .utils import ensure_dir, read_json, read_jsonl, tokenize_for_index, write_json, write_jsonl

DEFAULT_BUILD_ROOT = Path(r"D:\金融科技大赛\Code\data\rag_build\docling_p0_smoke20")


def chunk_title(chunk: dict[str, Any], doc_map: dict[str, dict[str, Any]]) -> str:
    doc = doc_map.get(chunk.get("doc_id"), {})
    return (doc.get("origin") or {}).get("filename") or doc.get("name") or chunk.get("doc_id") or "该文档"


def short_section(section_path: list[str] | None) -> str:
    if not section_path:
        return "相关章节"
    return " > ".join(section_path[-2:])


def load_build(build_root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    chunks = read_jsonl(build_root / "chunks.jsonl")
    docs = read_jsonl(build_root / "normalized_documents.jsonl")
    tables = read_jsonl(build_root / "normalized_tables.jsonl")
    doc_map = {d["doc_id"]: d for d in docs}
    return chunks, doc_map, tables


def generate_text_questions(chunks: list[dict[str, Any]], doc_map: dict[str, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows = []
    seen_doc_clause = set()
    for chunk in chunks:
        if chunk.get("chunk_type") != "text" or chunk.get("quality_status") != "ready":
            continue
        content = (chunk.get("content_text") or "").strip()
        if len(content) < 30:
            continue
        doc_id = chunk["doc_id"]
        title = chunk_title(chunk, doc_map)
        section = short_section(chunk.get("section_path"))
        clause = chunk.get("clause_no")
        key = (doc_id, clause or section, chunk["chunk_id"])
        if key in seen_doc_clause:
            continue
        seen_doc_clause.add(key)
        if clause:
            question = f"《{title}》中{clause}的主要规定是什么？"
            qtype = "clause_text"
        else:
            question = f"《{title}》中“{section}”部分主要说明了什么？"
            qtype = "section_text"
        rows.append({
            "qa_id": f"silver_text_{len(rows):04d}",
            "question": question,
            "question_type": qtype,
            "gold_doc_id": doc_id,
            "gold_chunk_ids": [chunk["chunk_id"]],
            "gold_table_id": None,
            "gold_row_idx": None,
            "gold_answer_hint": content[:240],
            "gold_source": "auto_from_chunk",
            "review_status": "silver_auto_needs_human_review",
        })
        if len(rows) >= limit:
            break
    return rows


def table_chunk_for_fact(chunks: list[dict[str, Any]], table_id: str, row_idx: int | None) -> list[str]:
    out = []
    for chunk in chunks:
        ref = chunk.get("modality_ref") or {}
        if ref.get("table_id") != table_id:
            continue
        if row_idx is None:
            out.append(chunk["chunk_id"])
            continue
        start = ref.get("row_start")
        end = ref.get("row_end")
        if isinstance(start, int) and isinstance(end, int) and start <= row_idx <= end:
            out.append(chunk["chunk_id"])
    return out


def generate_table_fact_questions(build_root: Path, chunks: list[dict[str, Any]], doc_map: dict[str, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    db_path = build_root / "indexes" / "sqlite" / "rag_tables.sqlite"
    if not db_path.exists():
        return []
    rows = []
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        facts = cur.execute(
            "SELECT fact_id, doc_id, table_id, row_idx, col_idx, row_headers, col_headers, value_text FROM table_facts WHERE length(value_text) > 0 LIMIT ?",
            (limit * 5,),
        ).fetchall()
    finally:
        con.close()
    for fact_id, doc_id, table_id, row_idx, col_idx, row_headers_json, col_headers_json, value_text in facts:
        if len(rows) >= limit:
            break
        if not value_text or len(str(value_text)) > 80:
            continue
        row_headers = json.loads(row_headers_json or "[]")
        col_headers = json.loads(col_headers_json or "[]")
        row_label = " / ".join([x for x in row_headers if x]) or f"第{row_idx}行"
        col_label = " / ".join([x for x in col_headers if x]) or f"第{col_idx}列"
        doc_title = (doc_map.get(doc_id, {}).get("origin") or {}).get("filename") or doc_id
        gold_chunks = table_chunk_for_fact(chunks, table_id, row_idx)
        if not gold_chunks:
            gold_chunks = table_chunk_for_fact(chunks, table_id, None)
        rows.append({
            "qa_id": f"silver_table_fact_{len(rows):04d}",
            "question": f"《{doc_title}》中表格 {table_id} 里，{row_label} 对应 {col_label} 的值是什么？",
            "question_type": "table_fact",
            "gold_doc_id": doc_id,
            "gold_chunk_ids": gold_chunks,
            "gold_table_id": table_id,
            "gold_row_idx": row_idx,
            "gold_answer_hint": str(value_text),
            "gold_source": fact_id,
            "review_status": "silver_auto_needs_human_review",
        })
    return rows


def generate_table_summary_questions(chunks: list[dict[str, Any]], doc_map: dict[str, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows = []
    for chunk in chunks:
        if chunk.get("chunk_type") != "table_summary":
            continue
        table_id = (chunk.get("modality_ref") or {}).get("table_id")
        if not table_id:
            continue
        title = chunk_title(chunk, doc_map)
        rows.append({
            "qa_id": f"silver_table_summary_{len(rows):04d}",
            "question": f"《{title}》中的表格 {table_id} 主要包含哪些字段或表头？",
            "question_type": "table_summary",
            "gold_doc_id": chunk.get("doc_id"),
            "gold_chunk_ids": [chunk["chunk_id"]],
            "gold_table_id": table_id,
            "gold_row_idx": None,
            "gold_answer_hint": (chunk.get("content_text") or "")[:240],
            "gold_source": "auto_from_table_summary_chunk",
            "review_status": "silver_auto_needs_human_review",
        })
        if len(rows) >= limit:
            break
    return rows


def generate_silver_qa(build_root: Path, output_dir: Path, text_limit: int = 50, table_fact_limit: int = 40, table_summary_limit: int = 20) -> list[dict[str, Any]]:
    chunks, doc_map, _tables = load_build(build_root)
    rows = []
    rows.extend(generate_text_questions(chunks, doc_map, text_limit))
    rows.extend(generate_table_fact_questions(build_root, chunks, doc_map, table_fact_limit))
    rows.extend(generate_table_summary_questions(chunks, doc_map, table_summary_limit))
    for idx, row in enumerate(rows):
        row["qa_id"] = f"qa_silver_{idx:04d}"
    ensure_dir(output_dir)
    write_jsonl(output_dir / "qa_silver_candidates.jsonl", rows)
    gold_template = []
    for row in rows:
        item = dict(row)
        item["review_status"] = "needs_human_review"
        item["human_gold_answer"] = ""
        item["human_notes"] = ""
        gold_template.append(item)
    write_jsonl(output_dir / "qa_gold_template.jsonl", gold_template)
    return rows


def bm25_search(query: str, bm25: dict[str, Any], top_k: int = 50) -> list[tuple[str, float]]:
    terms = tokenize_for_index(query)
    if not terms:
        return []
    postings = bm25.get("postings") or {}
    doc_lengths = bm25.get("doc_lengths") or {}
    avgdl = bm25.get("avg_doc_length") or 1.0
    n_docs = max(1, bm25.get("chunk_count") or len(doc_lengths))
    k1 = 1.5
    b = 0.75
    scores: dict[str, float] = defaultdict(float)
    for term in terms:
        term_postings = postings.get(term)
        if not term_postings:
            continue
        df = len(term_postings)
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for cid, tf in term_postings.items():
            dl = doc_lengths.get(cid, avgdl) or avgdl
            denom = tf + k1 * (1 - b + b * dl / avgdl)
            scores[cid] += idf * (tf * (k1 + 1) / denom)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]


def vector_search(query: str, vector_rows: list[dict[str, Any]], top_k: int = 50) -> list[tuple[str, float]]:
    if not vector_rows:
        return []
    dim = vector_rows[0].get("dim") or 256
    qv = hash_embedding(query, dim)
    scored = []
    for row in vector_rows:
        vec = row.get("vector") or []
        score = sum(a * b for a, b in zip(qv, vec))
        scored.append((row["chunk_id"], score))
    return sorted(scored, key=lambda x: x[1], reverse=True)[:top_k]


def rrf(bm25_hits: list[tuple[str, float]], vector_hits: list[tuple[str, float]], top_k: int = 20, k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for rank, (cid, _score) in enumerate(bm25_hits, start=1):
        scores[cid] += 1.0 / (k + rank)
    for rank, (cid, _score) in enumerate(vector_hits, start=1):
        scores[cid] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]


def evaluate_retrieval(build_root: Path, qa_path: Path, output_dir: Path, top_ks: tuple[int, ...] = (1, 3, 5, 10)) -> dict[str, Any]:
    chunks = read_jsonl(build_root / "chunks.jsonl")
    chunk_map = {c["chunk_id"]: c for c in chunks}
    bm25 = read_json(build_root / "indexes" / "bm25" / "bm25_index.json")
    vector_rows = read_jsonl(build_root / "indexes" / "vector" / "hash_vectors.jsonl")
    qa_rows = read_jsonl(qa_path)
    details = []
    aggregate = {f"hit@{k}": 0 for k in top_ks}
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, **{f"hit@{k}": 0 for k in top_ks}})
    for qa in qa_rows:
        question = qa["question"]
        bm25_hits = bm25_search(question, bm25, top_k=50)
        vec_hits = vector_search(question, vector_rows, top_k=50)
        fused = rrf(bm25_hits, vec_hits, top_k=max(top_ks))
        ranked_ids = [cid for cid, _ in fused]
        gold_chunk_ids = set(qa.get("gold_chunk_ids") or [])
        gold_table_id = qa.get("gold_table_id")
        hits: dict[str, bool] = {}
        for k in top_ks:
            top_ids = ranked_ids[:k]
            chunk_hit = bool(gold_chunk_ids.intersection(top_ids))
            table_hit = False
            if gold_table_id:
                for cid in top_ids:
                    ref = (chunk_map.get(cid, {}).get("modality_ref") or {})
                    if ref.get("table_id") == gold_table_id:
                        table_hit = True
                        break
            hit = chunk_hit or table_hit
            hits[f"hit@{k}"] = hit
            if hit:
                aggregate[f"hit@{k}"] += 1
        qtype = qa.get("question_type") or "unknown"
        by_type[qtype]["count"] += 1
        for k in top_ks:
            if hits[f"hit@{k}"]:
                by_type[qtype][f"hit@{k}"] += 1
        details.append({
            "qa_id": qa.get("qa_id"),
            "question_type": qtype,
            "question": question,
            "gold_chunk_ids": list(gold_chunk_ids),
            "gold_table_id": gold_table_id,
            "hits": hits,
            "retrieved": [
                {
                    "rank": idx + 1,
                    "chunk_id": cid,
                    "score": score,
                    "chunk_type": chunk_map.get(cid, {}).get("chunk_type"),
                    "doc_id": chunk_map.get(cid, {}).get("doc_id"),
                    "table_id": (chunk_map.get(cid, {}).get("modality_ref") or {}).get("table_id"),
                    "preview": (chunk_map.get(cid, {}).get("content_text") or "")[:160],
                }
                for idx, (cid, score) in enumerate(fused[:10])
            ],
        })
    total = len(qa_rows) or 1
    report = {
        "schema_version": "retrieval_eval_report.v1",
        "build_root": str(build_root),
        "qa_path": str(qa_path),
        "qa_count": len(qa_rows),
        "retrieval": "bm25 + hash_vector + rrf",
        "metrics": {key: value / total for key, value in aggregate.items()},
        "counts": aggregate,
        "by_type": {
            key: {
                metric: (value / max(1, stats["count"]) if metric.startswith("hit@") else value)
                for metric, value in stats.items()
            }
            for key, stats in by_type.items()
        },
        "note": "Silver QA uses auto-derived gold and is for smoke/regression only. Human-reviewed qa_gold.jsonl is required for official accuracy.",
    }
    ensure_dir(output_dir)
    write_json(output_dir / "retrieval_eval_report.json", report)
    write_jsonl(output_dir / "retrieval_eval_details.jsonl", details)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate silver QA and evaluate local retrieval for Docling P0 build.")
    parser.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--qa-path", type=Path, default=None, help="Existing QA JSONL. If omitted, silver QA is generated first.")
    parser.add_argument("--text-limit", type=int, default=50)
    parser.add_argument("--table-fact-limit", type=int, default=40)
    parser.add_argument("--table-summary-limit", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir or (args.build_root / "eval")
    if args.qa_path:
        qa_path = args.qa_path
    else:
        rows = generate_silver_qa(args.build_root, output_dir, args.text_limit, args.table_fact_limit, args.table_summary_limit)
        qa_path = output_dir / "qa_silver_candidates.jsonl"
        print(json.dumps({"generated_qa_count": len(rows), "qa_path": str(qa_path)}, ensure_ascii=False, indent=2))
    report = evaluate_retrieval(args.build_root, qa_path, output_dir)
    print(json.dumps({"qa_count": report["qa_count"], "metrics": report["metrics"], "output_dir": str(output_dir)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
