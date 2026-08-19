from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

import requests

from .eval_qa import (
    DEFAULT_QA_PATH,
    OPTION_KEYS,
    call_llm_choice,
    chat_completion_url,
    classify_failure,
    evidence_hit,
    extract_json_object,
    fallback_choice,
    load_qa_rows,
    make_retrieval_query,
    make_source_hint,
)
from .index_build import DEFAULT_BUILD_ROOT, load_dotenv
from .retrieval import DEFAULT_COLLECTION, DEFAULT_ENV_PATH, HybridRetriever
from .utils import compact_text, ensure_dir, write_json, write_jsonl

DEFAULT_OUT_DIR = DEFAULT_BUILD_ROOT / "eval" / "latency_sample10"


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    idx = (len(ordered) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarize_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def stats(field: str) -> dict[str, Any]:
        values = [float(row[field]) for row in rows if row.get(field) is not None]
        if not values:
            return {"count": 0}
        return {
            "count": len(values),
            "avg_sec": sum(values) / len(values),
            "p50_sec": statistics.median(values),
            "p90_sec": percentile(values, 0.9),
            "max_sec": max(values),
            "min_sec": min(values),
        }

    return {
        "total": len(rows),
        "correct": sum(1 for row in rows if row.get("is_correct")),
        "accuracy": sum(1 for row in rows if row.get("is_correct")) / len(rows) if rows else 0,
        "api_failed": sum(1 for row in rows if row.get("pred_answer") == "API_FAILED"),
        "second_pass_count": sum(1 for row in rows if row.get("second_pass")),
        "latency": {
            "end_to_end": stats("latency_total_sec"),
            "retrieval": stats("latency_retrieval_sec"),
            "llm_first": stats("latency_llm_first_sec"),
            "llm_second": stats("latency_llm_second_sec"),
        },
        "slowest": sorted(
            [
                {
                    "id": row.get("id"),
                    "source_type": row.get("source_type"),
                    "qa_type": row.get("qa_type"),
                    "latency_total_sec": row.get("latency_total_sec"),
                    "latency_retrieval_sec": row.get("latency_retrieval_sec"),
                    "latency_llm_first_sec": row.get("latency_llm_first_sec"),
                    "latency_llm_second_sec": row.get("latency_llm_second_sec"),
                    "pred_answer": row.get("pred_answer"),
                    "gold_answer": row.get("gold_answer"),
                    "is_correct": row.get("is_correct"),
                }
                for row in rows
            ],
            key=lambda x: float(x.get("latency_total_sec") or 0),
            reverse=True,
        )[:5],
    }


def call_fast_demo_choice(row: dict[str, Any], hits: list[dict[str, Any]], env: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = env.get("DASHSCOPE_API_KEY", "")
    base_url = env.get("DASHSCOPE_BASE_URL", "")
    model = env.get("LLM_MODEL", "")
    if not api_key or not base_url or not model:
        raise RuntimeError("Missing LLM config")
    evidence_blocks = []
    for idx, hit in enumerate(hits, start=1):
        payload = hit.get("payload") or {}
        evidence_blocks.append(
            f"[{idx}] chunk_id={hit.get('chunk_id')}; title={payload.get('doc_title')}\n"
            f"{compact_text(hit.get('content') or '')[:800]}"
        )
    options_text = "\n".join(f"{key}. {row['options'].get(key, '')}" for key in OPTION_KEYS)
    prompt = f"""
你是现场演示用 RAG 选择题助手。只基于证据回答，直接选择最可能的 A/B/C/D。
必须只输出 JSON：{{"answer":"A/B/C/D","confidence":0.0,"evidence_chunk_ids":["..."],"reason":"不超过30字"}}

题目：{row['question']}
选项：
{options_text}
证据：
{chr(10).join(evidence_blocks)}
""".strip()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "top_p": float(env.get("LLM_TOP_P", "0.8") or 0.8),
        "max_tokens": min(256, int(env.get("LLM_MAX_TOKENS", "512") or 512)),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = float(env.get("LLM_TIMEOUT_SECONDS", "45") or 45)
    resp = requests.post(chat_completion_url(base_url), headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM API failed {resp.status_code}: {resp.text[:300]}")
    content = ((((resp.json().get("choices") or [{}])[0]).get("message") or {}).get("content") or "").strip()
    parsed = extract_json_object(content) or {}
    answer = str(parsed.get("answer", "")).upper().strip()
    if answer not in OPTION_KEYS:
        parsed["answer"] = "API_FAILED"
    else:
        parsed["answer"] = answer
    return parsed, {"backend": "dashscope_fast_demo", "model": model, "raw": content[:600]}


def run_latency_sample(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dir(args.out_dir)
    if args.fast_demo_mode:
        args.evidence_top_k = min(args.evidence_top_k, 3)
        args.force_choice_on_insufficient = False
    env = load_dotenv(args.env_path)
    all_rows = load_qa_rows(args.qa_path, args.sheet_name, args.source_types, args.pool_limit)
    if args.sample_size > len(all_rows):
        raise ValueError(f"sample_size={args.sample_size} is larger than QA pool size={len(all_rows)}")
    rng = random.Random(args.seed)
    rows = rng.sample(all_rows, args.sample_size)
    if args.sort_by_id:
        rows.sort(key=lambda row: row["id"])

    retriever = HybridRetriever(
        build_root=args.build_root,
        env_path=args.env_path,
        collection=args.collection,
        embedding_backend=args.embedding_backend,
    )
    details: list[dict[str, Any]] = []
    try:
        for idx, row in enumerate(rows, start=1):
            start_total = time.perf_counter()
            query = make_retrieval_query(row, include_options=args.options_in_retrieval_query)
            source_hint = make_source_hint(row)
            source_hint_query = make_retrieval_query(row, include_options=True)

            start_retrieval = time.perf_counter()
            retrieval = retriever.retrieve(
                query,
                source_hint=source_hint,
                source_hint_query=source_hint_query,
                dense_top_k=args.dense_top_k,
                sparse_top_k=args.sparse_top_k,
                table_top_k=args.table_top_k,
                rerank_top_k=args.evidence_top_k,
            )
            latency_retrieval = time.perf_counter() - start_retrieval
            hits = retrieval["reranked_hits"]

            start_llm = time.perf_counter()
            llm_error = None
            try:
                if args.fast_demo_mode:
                    prediction, llm_info = call_fast_demo_choice(row, hits, env)
                else:
                    prediction, llm_info = call_llm_choice(row, hits, env)
            except Exception as exc:
                llm_error = str(exc)
                if args.no_lexical_final:
                    prediction = {
                        "answer": "API_FAILED",
                        "confidence": 0.0,
                        "evidence_chunk_ids": [],
                        "reason": "LLM 调用失败，未使用 lexical fallback。",
                    }
                    llm_info = {"backend": "api_failed_no_final", "error": llm_error}
                else:
                    prediction, llm_info = fallback_choice(row, hits, llm_error)
            latency_llm_first = time.perf_counter() - start_llm

            pred_answer = str(prediction.get("answer") or "").upper()
            hit_ok = evidence_hit(row, hits)
            second_pass_info = None
            latency_llm_second = None
            if args.force_choice_on_insufficient and pred_answer == "INSUFFICIENT" and hit_ok and hits:
                start_second = time.perf_counter()
                second_error = None
                try:
                    prediction, llm_info = call_llm_choice(row, hits, env, force_choice=True)
                except Exception as exc:
                    second_error = str(exc)
                    if args.no_lexical_final:
                        prediction = {
                            "answer": "API_FAILED",
                            "confidence": 0.0,
                            "evidence_chunk_ids": [],
                            "reason": "二次 LLM 调用失败，未使用 lexical fallback。",
                        }
                        llm_info = {"backend": "api_failed_no_final", "error": second_error, "force_choice": True}
                    else:
                        prediction, llm_info = fallback_choice(row, hits, second_error, force_choice=True)
                latency_llm_second = time.perf_counter() - start_second
                pred_answer = str(prediction.get("answer") or "").upper()
                second_pass_info = {"triggered": True, "backend": llm_info.get("backend"), "answer": pred_answer}

            total_latency = time.perf_counter() - start_total
            detail = {
                "sample_rank": idx,
                "id": row["id"],
                "source_type": row["source_type"],
                "qa_type": row["qa_type"],
                "difficulty": row["difficulty"],
                "question": row["question"],
                "options": row["options"],
                "gold_answer": row["answer"],
                "pred_answer": pred_answer,
                "is_correct": pred_answer == row["answer"],
                "evidence_hit": hit_ok,
                "failure_type": classify_failure(row, pred_answer, hits, hit_ok),
                "latency_total_sec": total_latency,
                "latency_retrieval_sec": latency_retrieval,
                "latency_llm_first_sec": latency_llm_first,
                "latency_llm_second_sec": latency_llm_second,
                "retrieval_route": retrieval["route"],
                "retrieval_errors": retrieval.get("errors", {}),
                "rerank": retrieval["rerank"],
                "llm": llm_info,
                "second_pass": second_pass_info,
                "top_evidence": [
                    {
                        "rank": evidence_idx,
                        "chunk_id": hit.get("chunk_id"),
                        "doc_id": hit.get("doc_id"),
                        "source": hit.get("source"),
                        "score": hit.get("score"),
                        "doc_title": (hit.get("payload") or {}).get("doc_title"),
                        "content_preview": compact_text(hit.get("content") or "")[:300],
                    }
                    for evidence_idx, hit in enumerate(hits, start=1)
                ],
            }
            details.append(detail)
            write_jsonl(args.out_dir / "latency_sample_details.jsonl", details)
            report = summarize_latency(details)
            report.update(
                {
                    "status": "running",
                    "completed": idx,
                    "planned_total": len(rows),
                    "seed": args.seed,
                    "sample_size": args.sample_size,
                    "pool_size": len(all_rows),
                }
            )
            write_json(args.out_dir / "latency_sample_report.json", report)
            print(
                f"[{idx}/{len(rows)}] {row['id']} total={total_latency:.2f}s retrieval={latency_retrieval:.2f}s "
                f"llm1={latency_llm_first:.2f}s llm2={latency_llm_second if latency_llm_second is not None else 0:.2f}s "
                f"pred={pred_answer} gold={row['answer']} correct={detail['is_correct']}",
                flush=True,
            )
    finally:
        retriever.close()

    report = summarize_latency(details)
    report.update(
        {
            "status": "finished",
            "seed": args.seed,
            "sample_size": args.sample_size,
            "pool_size": len(all_rows),
            "qa_path": str(args.qa_path),
            "build_root": str(args.build_root),
            "out_dir": str(args.out_dir),
            "source_types": args.source_types,
            "collection": args.collection,
            "embedding_backend": args.embedding_backend,
            "fast_demo_mode": args.fast_demo_mode,
            "note": "LLM latency is measured with live calls in this script; query embedding cache may still be used by retrieval.",
        }
    )
    write_jsonl(args.out_dir / "latency_sample_details.jsonl", details)
    write_json(args.out_dir / "latency_sample_report.json", report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Randomly sample QA rows and measure live retrieval + LLM answer latency.")
    p.add_argument("--qa-path", type=Path, default=DEFAULT_QA_PATH)
    p.add_argument("--sheet-name", default=None)
    p.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    p.add_argument("--env-path", type=Path, default=DEFAULT_ENV_PATH)
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--embedding-backend", choices=["api", "hash"], default="api")
    p.add_argument("--source-types", nargs="*", default=["word", "pdf"])
    p.add_argument("--pool-limit", type=int, default=200)
    p.add_argument("--sample-size", type=int, default=10)
    p.add_argument("--seed", type=int, default=20260818)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--dense-top-k", type=int, default=10)
    p.add_argument("--sparse-top-k", type=int, default=10)
    p.add_argument("--table-top-k", type=int, default=10)
    p.add_argument("--evidence-top-k", type=int, default=5)
    p.add_argument("--force-choice-on-insufficient", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--options-in-retrieval-query", action="store_true")
    p.add_argument("--no-lexical-final", action="store_true")
    p.add_argument("--sort-by-id", action="store_true")
    p.add_argument("--fast-demo-mode", action="store_true", help="Use a shorter prompt, top3 evidence, and no second pass to approximate live demo latency.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_latency_sample(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
