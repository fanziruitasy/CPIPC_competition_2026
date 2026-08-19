from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .index_build import DEFAULT_BUILD_ROOT, load_dotenv
from .retrieval import DEFAULT_COLLECTION, DEFAULT_ENV_PATH, HybridRetriever
from .utils import compact_text, ensure_dir, write_json, write_jsonl

DEFAULT_QA_PATH = Path(r"D:\金融科技大赛\Code\data\QA数据.xlsx")
DEFAULT_OUT_DIR = DEFAULT_BUILD_ROOT / "eval" / "qa_hybrid_smoke20"
OPTION_KEYS = ["A", "B", "C", "D"]


def value_to_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def cache_key(obj: Any) -> str:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def load_llm_cache(path: Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return cache
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = row.get("key")
            if key:
                cache[key] = row
    return cache


def append_llm_cache(path: Path, key: str, prediction: dict[str, Any], llm_info: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "prediction": prediction, "llm": llm_info}, ensure_ascii=False) + "\n")


def load_wrong_ids(path: Path | None) -> set[str] | None:
    if not path:
        return None
    ids: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("is_correct") is False and row.get("id"):
                ids.add(str(row["id"]))
    return ids


def api_failed_prediction(error: str | None, force_choice: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    return {
        "answer": "API_FAILED",
        "confidence": 0.0,
        "evidence_chunk_ids": [],
        "reason": "LLM 多次调用失败，未使用 lexical fallback 作为最终答案。",
    }, {"backend": "api_failed_no_final", "error": error, "force_choice": force_choice}


def load_qa_rows(
    path: Path,
    sheet_name: str | None = None,
    source_types: list[str] | None = None,
    limit: int | None = None,
    include_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    df = pd.read_excel(path, sheet_name=sheet_name or 0)
    rows: list[dict[str, Any]] = []
    allowed = {s.lower() for s in source_types or []}
    for _, rec in df.iterrows():
        source_type = value_to_text(rec.get("source_type")).lower()
        row_id = value_to_text(rec.get("id")) or f"row_{len(rows) + 1:04d}"
        if include_ids is not None and row_id not in include_ids:
            continue
        if allowed and source_type not in allowed:
            continue
        row = {
            "id": row_id,
            "source_type": source_type,
            "difficulty": value_to_text(rec.get("difficulty")),
            "difficulty_cn": value_to_text(rec.get("difficulty_cn")),
            "qa_type": value_to_text(rec.get("qa_type")),
            "question": value_to_text(rec.get("question")),
            "options": {k: value_to_text(rec.get(f"option_{k.lower()}")) for k in OPTION_KEYS},
            "answer": value_to_text(rec.get("answer")).upper()[:1],
            "answer_text": value_to_text(rec.get("answer_text")),
            "evidence": value_to_text(rec.get("evidence")),
            "source_title": value_to_text(rec.get("source_title")),
            "file_label": value_to_text(rec.get("file_label")),
        }
        if row["question"] and row["answer"] in OPTION_KEYS:
            rows.append(row)
        if limit and len(rows) >= limit:
            break
    return rows


def make_retrieval_query(row: dict[str, Any], include_options: bool = True) -> str:
    source_hint = make_source_hint(row)
    parts = [row["question"]]
    if source_hint:
        parts.append(f"目标资料：{source_hint}")
    if include_options:
        parts.extend(f"{key}. {row['options'].get(key, '')}" for key in OPTION_KEYS)
    return "\n".join(p for p in parts if p)


def make_source_hint(row: dict[str, Any]) -> str:
    parts = [row.get("source_title"), row.get("file_label")]
    seen = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return " | ".join(seen)


def chat_completion_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def call_llm_choice(row: dict[str, Any], hits: list[dict[str, Any]], env: dict[str, str], force_choice: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = env.get("DASHSCOPE_API_KEY", "")
    base_url = env.get("DASHSCOPE_BASE_URL", "")
    model = env.get("LLM_MODEL", "")
    if not api_key or not base_url or not model:
        raise RuntimeError("Missing LLM config")
    evidence_blocks = []
    for idx, hit in enumerate(hits, start=1):
        payload = hit.get("payload") or {}
        evidence_blocks.append(
            f"[{idx}] chunk_id={hit.get('chunk_id')}; doc_id={hit.get('doc_id')}; source={hit.get('source')}; "
            f"title={payload.get('doc_title')}; page={payload.get('page_start')}-{payload.get('page_end')}\n"
            f"{compact_text(hit.get('content') or '')[:1600]}"
        )
    options_text = "\n".join(f"{k}. {row['options'].get(k, '')}" for k in OPTION_KEYS)
    source_hint = make_source_hint(row)
    answer_schema = "A/B/C/D" if force_choice else "A/B/C/D/INSUFFICIENT"
    force_rules = ""
    if force_choice:
        force_rules = """
强制择优规则：
1. 这是资料库内单选题，且已经检索到相关证据。
2. 本轮不允许输出 INSUFFICIENT，必须在 A/B/C/D 中选择最可能的一项。
3. 仍然必须以目标资料证据为准，不能用其他资料中的定义、常识或模型记忆补答案。
4. 如果某个选项没有直接证据 chunk_id，不得把它标记为 support。
""".strip()
    system = "你是银行业监管制度与统计报表 RAG 选择题评测器。只能基于给定证据判断，不要使用资料库外知识。"
    user = f"""
请回答单选题。你必须只输出 JSON，不要输出额外文字。

目标资料：
{source_hint or "未提供"}

题目：
{row['question']}

选项：
{options_text}

检索证据：
{chr(10).join(evidence_blocks)}

输出格式：
{{"option_checks":{{"A":{{"status":"support/refute/unknown","evidence_chunk_ids":["..."],"reason":"..."}},"B":{{"status":"support/refute/unknown","evidence_chunk_ids":["..."],"reason":"..."}},"C":{{"status":"support/refute/unknown","evidence_chunk_ids":["..."],"reason":"..."}},"D":{{"status":"support/refute/unknown","evidence_chunk_ids":["..."],"reason":"..."}}}},"answer":"{answer_schema}","confidence":0.0,"evidence_chunk_ids":["..."],"reason":"一句话说明"}}

规则：
1. 先分别检查 A/B/C/D：每个选项必须标记 support、refute 或 unknown。
2. support 必须满足两个条件：证据来自“目标资料”或同名同源资料；并且能直接支持该选项的核心事实。
3. 如果一个选项只能被非目标资料、常识、模型记忆支持，则必须标记为 unknown 或 refute，不能标记为 support。
4. 每个 support 选项必须列出至少一个直接证据 chunk_id；没有直接 chunk_id 的选项不得 support。
5. 本批评测题来自资料库内，通常存在唯一正确选项；只要目标资料证据能比较选项，就在 A/B/C/D 中择优选择。
6. 如果多个选项都像是 support，优先选择目标资料证据最直接、覆盖题干条件最多的选项。
7. 只有当检索证据整体与目标资料完全无关，且无法比较任何选项时，才输出 INSUFFICIENT。
8. evidence_chunk_ids 只能填写上方出现的 chunk_id。
{force_rules}
""".strip()
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": float(env.get("LLM_TEMPERATURE", "0") or 0),
        "top_p": float(env.get("LLM_TOP_P", "0.8") or 0.8),
        "max_tokens": int(env.get("LLM_MAX_TOKENS", "512") or 512),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = float(env.get("LLM_TIMEOUT_SECONDS", "90") or 90)
    resp = requests.post(chat_completion_url(base_url), headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM API failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    parsed = extract_json_object(content) or {}
    answer = str(parsed.get("answer", "")).upper().strip()
    if answer not in OPTION_KEYS and answer != "INSUFFICIENT":
        match = re.search(r"\b([ABCD])\b", content.upper())
        answer = match.group(1) if match else "INSUFFICIENT"
    parsed["answer"] = answer
    return parsed, {"backend": "dashscope_force_choice" if force_choice else "dashscope", "model": model, "raw": content[:1000]}


def fallback_choice(row: dict[str, Any], hits: list[dict[str, Any]], error: str | None = None, force_choice: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = "\n".join(h.get("content") or "" for h in hits).lower()
    scores = {}
    for key, option in row["options"].items():
        terms = set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", option.lower()))
        scores[key] = sum(1 for term in terms if term and term in evidence)
    best, score = max(scores.items(), key=lambda x: x[1]) if scores else ("INSUFFICIENT", 0)
    answer = best if (score > 0 or force_choice) else "INSUFFICIENT"
    return {
        "answer": answer,
        "confidence": 0.2 if answer != "INSUFFICIENT" else 0.0,
        "evidence_chunk_ids": [h.get("chunk_id") for h in hits[:2] if h.get("chunk_id")],
        "reason": "LLM 调用失败后使用选项-证据词面重合降级判断。",
    }, {"backend": "lexical_force_choice_fallback" if force_choice else "lexical_fallback", "error": error}


def evidence_hit(row: dict[str, Any], hits: list[dict[str, Any]]) -> bool:
    gold_parts = [row.get("source_title"), row.get("file_label")]
    gold_text = " ".join(p for p in gold_parts if p).lower()
    if not gold_text.strip():
        return False
    gold_tokens = re.findall(r"[\u4e00-\u9fff]{3,}|[A-Za-z0-9_]{3,}", gold_text)
    for hit in hits:
        payload = hit.get("payload") or {}
        hay = " ".join(str(payload.get(k) or "") for k in ["doc_title", "source_path", "doc_id"]).lower()
        if any(part and part.lower() in hay for part in gold_parts):
            return True
        if any(tok in hay for tok in gold_tokens):
            return True
    return False


def classify_failure(row: dict[str, Any], pred: str, hits: list[dict[str, Any]], hit_ok: bool) -> str:
    if pred == row["answer"]:
        return "correct"
    if pred == "API_FAILED":
        return "api_failed"
    if not hits:
        return "no_retrieval_hits"
    if not hit_ok:
        return "evidence_not_recalled"
    if pred == "INSUFFICIENT":
        return "llm_over_refusal_or_low_confidence"
    return "llm_choice_error_after_recall"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    correct = sum(1 for r in rows if r["is_correct"])
    evidence_hits = sum(1 for r in rows if r["evidence_hit"])
    report = {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0,
        "evidence_hits": evidence_hits,
        "evidence_hit_rate": evidence_hits / total if total else 0,
        "failure_counts": dict(Counter(r["failure_type"] for r in rows)),
        "by_source_type": {},
        "by_qa_type": {},
        "target_metrics_reference": {
            "policy_fact_accuracy_min": 0.85,
            "table_value_accuracy_min": 0.80,
            "evidence_hit_rate_min": 0.90,
            "critical_number_date_org_docno_error_rate_max": 0.05,
            "out_of_scope_refusal_rate_min": 0.80,
        },
    }
    for field, bucket_name in [("source_type", "by_source_type"), ("qa_type", "by_qa_type")]:
        groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get(field) or "")].append(row)
        for key, vals in groups.items():
            report[bucket_name][key] = {
                "count": len(vals),
                "accuracy": sum(1 for v in vals if v["is_correct"]) / len(vals) if vals else 0,
                "evidence_hit_rate": sum(1 for v in vals if v["evidence_hit"]) / len(vals) if vals else 0,
            }
    return report


def run_eval(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dir(args.out_dir)
    env = load_dotenv(args.env_path)
    only_ids = load_wrong_ids(args.only_wrong_from)
    rows = load_qa_rows(args.qa_path, args.sheet_name, args.source_types, args.limit, include_ids=only_ids)
    llm_cache_path = args.llm_cache_path or (args.build_root / "eval" / "qa_cache" / "llm_choice_cache.jsonl")
    llm_cache = load_llm_cache(llm_cache_path)
    llm_cache_hits = 0
    llm_cache_writes = 0
    second_pass_count = 0
    retriever = HybridRetriever(
        build_root=args.build_root,
        env_path=args.env_path,
        collection=args.collection,
        embedding_backend=args.embedding_backend,
    )
    details: list[dict[str, Any]] = []
    detail_path = args.out_dir / "qa_eval_details.jsonl"
    report_path = args.out_dir / "qa_eval_report.json"
    try:
        for row_idx, row in enumerate(rows, start=1):
            query = make_retrieval_query(row, include_options=args.options_in_retrieval_query)
            source_hint = make_source_hint(row)
            source_hint_query = make_retrieval_query(row, include_options=True)
            retrieval = retriever.retrieve(
                query,
                source_hint=source_hint,
                source_hint_query=source_hint_query,
                source_type_hint=row["source_type"],
                dense_top_k=args.dense_top_k,
                sparse_top_k=args.sparse_top_k,
                table_top_k=args.table_top_k,
                rerank_top_k=args.evidence_top_k,
            )
            hits = retrieval["reranked_hits"]
            last_err = None
            llm_key = cache_key(
                {
                    "schema": "qa_choice_v7_target_evidence_required_profile_boost",
                    "model": env.get("LLM_MODEL"),
                    "row_id": row["id"],
                    "question": row["question"],
                    "options": row["options"],
                    "source_hint": source_hint,
                    "source_type": row["source_type"],
                    "evidence": [
                        {
                            "chunk_id": h.get("chunk_id"),
                            "doc_id": h.get("doc_id"),
                            "content": compact_text(h.get("content") or "")[:1600],
                        }
                        for h in hits
                    ],
                }
            )
            cached = llm_cache.get(llm_key)
            if cached:
                prediction = cached["prediction"]
                llm_info = dict(cached.get("llm") or {})
                llm_info["cache_hit"] = True
                llm_cache_hits += 1
            else:
                for attempt in range(int(env.get("LLM_MAX_RETRIES", "2") or 2)):
                    try:
                        prediction, llm_info = call_llm_choice(row, hits, env)
                        append_llm_cache(llm_cache_path, llm_key, prediction, llm_info)
                        llm_cache[llm_key] = {"prediction": prediction, "llm": llm_info}
                        llm_cache_writes += 1
                        break
                    except Exception as exc:
                        last_err = str(exc)
                        time.sleep(float(env.get("LLM_RETRY_BACKOFF_SECONDS", "2") or 2) * (attempt + 1))
                else:
                    if args.no_lexical_final:
                        prediction, llm_info = api_failed_prediction(last_err)
                    else:
                        prediction, llm_info = fallback_choice(row, hits, last_err)
            pred_answer = str(prediction.get("answer") or "").upper()
            hit_ok = evidence_hit(row, hits)
            second_pass_info: dict[str, Any] | None = None
            if args.force_choice_on_insufficient and pred_answer == "INSUFFICIENT" and hit_ok and hits:
                second_pass_count += 1
                force_key = cache_key(
                    {
                        "schema": "qa_choice_v7_force_choice_target_evidence_required",
                        "model": env.get("LLM_MODEL"),
                        "row_id": row["id"],
                        "question": row["question"],
                        "options": row["options"],
                        "source_hint": source_hint,
                        "source_type": row["source_type"],
                        "evidence": [
                            {
                                "chunk_id": h.get("chunk_id"),
                                "doc_id": h.get("doc_id"),
                                "content": compact_text(h.get("content") or "")[:1600],
                            }
                            for h in hits
                        ],
                    }
                )
                cached_force = llm_cache.get(force_key)
                if cached_force:
                    prediction = cached_force["prediction"]
                    llm_info = dict(cached_force.get("llm") or {})
                    llm_info["cache_hit"] = True
                    llm_cache_hits += 1
                else:
                    force_err = None
                    for attempt in range(int(env.get("LLM_MAX_RETRIES", "2") or 2)):
                        try:
                            prediction, llm_info = call_llm_choice(row, hits, env, force_choice=True)
                            append_llm_cache(llm_cache_path, force_key, prediction, llm_info)
                            llm_cache[force_key] = {"prediction": prediction, "llm": llm_info}
                            llm_cache_writes += 1
                            break
                        except Exception as exc:
                            force_err = str(exc)
                            time.sleep(float(env.get("LLM_RETRY_BACKOFF_SECONDS", "2") or 2) * (attempt + 1))
                    else:
                        if args.no_lexical_final:
                            prediction, llm_info = api_failed_prediction(force_err, force_choice=True)
                        else:
                            prediction, llm_info = fallback_choice(row, hits, force_err, force_choice=True)
                pred_answer = str(prediction.get("answer") or "").upper()
                second_pass_info = {"triggered": True, "backend": llm_info.get("backend"), "answer": pred_answer}
            detail = {
                "id": row["id"],
                "source_type": row["source_type"],
                "qa_type": row["qa_type"],
                "difficulty": row["difficulty"],
                "question": row["question"],
                "options": row["options"],
                "gold_answer": row["answer"],
                "pred_answer": pred_answer,
                "is_correct": pred_answer == row["answer"],
                "answer_text": row.get("answer_text"),
                "source_title": row.get("source_title"),
                "file_label": row.get("file_label"),
                "evidence_hit": hit_ok,
                "failure_type": classify_failure(row, pred_answer, hits, hit_ok),
                "prediction": prediction,
                "llm": llm_info,
                "second_pass": second_pass_info,
                "retrieval_route": retrieval["route"],
                "retrieval_errors": retrieval.get("errors", {}),
                "rerank": retrieval["rerank"],
                "retrieval_sources": sorted({src for h in hits for src in ((h.get("payload") or {}).get("retrieval_sources") or [h.get("source")]) if src}),
                "top_evidence": [
                    {
                        "rank": idx,
                        "chunk_id": h.get("chunk_id"),
                        "doc_id": h.get("doc_id"),
                        "source": h.get("source"),
                        "score": h.get("score"),
                        "payload": h.get("payload"),
                        "content_preview": compact_text(h.get("content") or "")[:500],
                    }
                    for idx, h in enumerate(hits, start=1)
                ],
            }
            details.append(detail)
            write_jsonl(detail_path, details)
            partial_report = summarize(details)
            partial_report.update({"completed": row_idx, "planned_total": len(rows), "status": "running", "llm_cache_hits": llm_cache_hits, "llm_cache_writes": llm_cache_writes, "second_pass_count": second_pass_count})
            write_json(report_path, partial_report)
            print(f"[{row_idx}/{len(rows)}] {row['id']} pred={pred_answer} gold={row['answer']} correct={detail['is_correct']} failure={detail['failure_type']}", flush=True)
    finally:
        retriever.close()
    report = summarize(details)
    report.update(
        {
            "qa_path": str(args.qa_path),
            "out_dir": str(args.out_dir),
            "limit": args.limit,
            "source_types": args.source_types,
            "only_wrong_from": str(args.only_wrong_from) if args.only_wrong_from else None,
            "only_wrong_count": len(only_ids) if only_ids is not None else None,
            "no_lexical_final": args.no_lexical_final,
            "collection": args.collection,
            "embedding_backend": args.embedding_backend,
            "llm_cache_path": str(llm_cache_path),
            "llm_cache_hits": llm_cache_hits,
            "llm_cache_writes": llm_cache_writes,
            "second_pass_count": second_pass_count,
        }
    )
    write_jsonl(detail_path, details)
    write_json(report_path, report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run MCQ QA evaluation with hybrid retrieval + rerank + LLM choice.")
    p.add_argument("--qa-path", type=Path, default=DEFAULT_QA_PATH)
    p.add_argument("--sheet-name", default=None)
    p.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    p.add_argument("--env-path", type=Path, default=DEFAULT_ENV_PATH)
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--embedding-backend", choices=["api", "hash"], default="api")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--llm-cache-path", type=Path, default=None)
    p.add_argument("--only-wrong-from", type=Path, default=None, help="Only rerun QA rows that were incorrect in a previous qa_eval_details.jsonl.")
    p.add_argument("--no-lexical-final", action="store_true", help="Mark API_FAILED instead of using lexical fallback as final answer when LLM calls fail.")
    p.add_argument("--source-types", nargs="*", default=["word", "pdf"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--dense-top-k", type=int, default=10)
    p.add_argument("--sparse-top-k", type=int, default=10)
    p.add_argument("--table-top-k", type=int, default=10)
    p.add_argument("--evidence-top-k", type=int, default=5)
    p.add_argument("--force-choice-on-insufficient", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--options-in-retrieval-query", action="store_true", help="Include A/B/C/D options in the retrieval query. Disabled by default to avoid distractor contamination.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_eval(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
