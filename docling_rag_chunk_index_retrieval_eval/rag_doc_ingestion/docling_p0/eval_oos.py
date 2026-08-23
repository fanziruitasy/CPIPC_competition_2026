from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

from .eval_qa import chat_completion_url, extract_json_object, load_llm_cache, append_llm_cache, cache_key
from .index_build import DEFAULT_BUILD_ROOT, load_dotenv
from .retrieval import DEFAULT_COLLECTION, DEFAULT_ENV_PATH, HybridRetriever, make_visible_source_hint
from .utils import compact_text, ensure_dir, write_json, write_jsonl

DEFAULT_OOS_PATH = Path(r"D:\金融科技大赛\Code\data\eval\oos_insufficient_50\oos_insufficient_50.jsonl")
DEFAULT_OUT_DIR = DEFAULT_BUILD_ROOT / "eval" / "oos_insufficient_50_v2"

REFUSAL_LABELS = {"refuse", "clarify"}
ERROR_LABELS = {"unsupported_answer", "supported_answer"}



def post_chat_with_retries(base_url: str, api_key: str, payload: dict[str, Any], env: dict[str, str], purpose: str) -> requests.Response:
    timeout = float(env.get("LLM_TIMEOUT_SECONDS", "90") or 90)
    max_retries = min(int(env.get("LLM_MAX_RETRIES", "3") or 3), 3)
    backoff = float(env.get("LLM_RETRY_BACKOFF_SECONDS", "2") or 2)
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                chat_completion_url(base_url),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if resp.status_code < 500:
                return resp
            last_exc = RuntimeError(f"{purpose} failed {resp.status_code}: {resp.text[:300]}")
        except Exception as exc:
            last_exc = exc
        if attempt < max_retries - 1:
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"{purpose} failed after {max_retries} attempts: {last_exc}")

def load_oos_rows(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                rows.append(dict(row))
    else:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    if limit:
        rows = rows[:limit]
    return rows


def evidence_blocks(hits: list[dict[str, Any]], max_chars: int = 900) -> str:
    blocks = []
    for idx, hit in enumerate(hits, start=1):
        payload = hit.get("payload") or {}
        blocks.append(
            f"[{idx}] chunk_id={hit.get('chunk_id')}; doc_id={hit.get('doc_id')}; "
            f"source={hit.get('source')}; title={payload.get('doc_title')}; "
            f"page={payload.get('page_start')}-{payload.get('page_end')}\n"
            f"{compact_text(hit.get('content') or '')[:max_chars]}"
        )
    return "\n\n".join(blocks) if blocks else "未检索到证据。"


def call_llm_open_answer(row: dict[str, Any], hits: list[dict[str, Any]], env: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = env.get("LLM_API_KEY") or env.get("DASHSCOPE_API_KEY") or env.get("OPENAI_API_KEY")
    base_url = env.get("LLM_BASE_URL") or env.get("DASHSCOPE_BASE_URL") or env.get("OPENAI_BASE_URL") or ""
    model = env.get("LLM_MODEL") or env.get("CHAT_MODEL")
    if not api_key or not base_url or not model:
        raise RuntimeError("Missing LLM_API_KEY/LLM_BASE_URL/LLM_MODEL in env")
    system = (
        "你是银行业监管制度与统计报表可信RAG问答系统。你只能根据给定检索证据回答。"
        "当证据不足、问题缺少口径、需要外部材料、需要最新动态，或证据只支持原则不能推出确定结论时，必须拒答或提出澄清。"
        "严禁使用模型内部知识库，严禁编造数值、名单、文号、机构、文件内容、个案结论或监管口径。"
    )
    user = f"""
用户问题：
{row.get('question','')}

【检索证据】
{evidence_blocks(hits)}

请输出严格JSON，不要输出任何额外解释、markdown。字段要求：
{{
  "answer_type": "refuse/clarify/answer",
  "answer": "给用户的自然语言回答，中文，简洁，说明证据边界",
  "confidence": 0.0,
  "evidence_chunk_ids": ["只能填写上面【检索证据】中出现过的chunk_id，没有可用证据填空数组[]，禁止编造id"],
  "missing_information": ["字符串数组，列出缺少的材料、口径、时点、外部文件；无填[]"],
  "reason": "简短理由，依据检索证据说明为什么拒答/澄清/可以回答"
}}

判定规则：
1. 只有检索证据中的正文内容可以用来回答，仅命中文档标题但正文没有对应信息，同样视为证据不足，不能作答。严禁使用模型内部知识库。
2. 若目标文件不存在、需要未入库外部文件、未公开数据，answer_type="refuse"。
3. 若问题缺少关键信息：时间、机构主体、统计口径、业务范围，answer_type="clarify"。
4. 如果证据只给出原则、制度模板、通用方法，不足以推导个案、具体数值、具体判定结论，answer_type="refuse"。
5. 只有检索证据存在直接支撑该问题的事实、数值、条文，才允许 answer_type="answer"。
6. 禁止编造数值、机构名称、文号、统计结果；无法回答就拒答，不要强行输出结论。
""".strip()
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": float(env.get("LLM_TEMPERATURE", "0") or 0),
        "top_p": float(env.get("LLM_TOP_P", "0.8") or 0.8),
        "max_tokens": min(int(env.get("LLM_MAX_TOKENS", "700") or 700), 900),
    }
    resp = post_chat_with_retries(base_url, api_key, payload, env, "LLM answer")
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM answer failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    parsed = extract_json_object(content)
    if not parsed:
        raise RuntimeError(f"LLM answer returned non-json output: {content[:300]}")
    answer_type = str(parsed.get("answer_type") or "answer").lower().strip()
    if answer_type not in {"refuse", "clarify", "answer"}:
        raise RuntimeError(f"LLM answer returned invalid answer_type: {answer_type}")
    parsed["answer_type"] = answer_type
    if not isinstance(parsed.get("evidence_chunk_ids"), list):
        parsed["evidence_chunk_ids"] = []
    if not isinstance(parsed.get("missing_information"), list):
        parsed["missing_information"] = []
    return parsed, {"backend": "dashscope", "model": model, "raw": content[:1500]}

def call_llm_judge(row: dict[str, Any], answer: dict[str, Any], hits: list[dict[str, Any]], env: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = env.get("LLM_API_KEY") or env.get("DASHSCOPE_API_KEY") or env.get("OPENAI_API_KEY")
    base_url = env.get("LLM_BASE_URL") or env.get("DASHSCOPE_BASE_URL") or env.get("OPENAI_BASE_URL") or ""
    model = env.get("LLM_MODEL") or env.get("CHAT_MODEL")
    if not api_key or not base_url or not model:
        raise RuntimeError("Missing judge LLM config")
    system = (
        "你是可信RAG评测裁判。根据题目、证据边界、检索证据和模型回答，判断模型是否正确拒答/澄清，"
        "或是否进行了无依据作答。裁判可以看到 expected_behavior，但生成模型没有看到该字段。"
    )
    user = f"""
题目：{row.get('question','')}
题目类型：{row.get('oos_type','')}
期望行为：{row.get('expected_behavior','')}
为什么证据不足：{row.get('rationale','')}
可接受回答：{row.get('acceptable_response','')}

模型回答类型：{answer.get('answer_type')}
模型回答：{answer.get('answer')}
模型引用证据ID：{answer.get('evidence_chunk_ids')}
模型缺失信息列表：{answer.get('missing_information')}
模型理由：{answer.get('reason')}

检索证据摘要：
{evidence_blocks(hits, max_chars=500)}

请只输出 JSON：
{{
  "verdict": "refuse/clarify/unsupported_answer/supported_answer",
  "is_success": true,
  "critical_entity_error": false,
  "error_type": "none/over_answer/fabricated_number/fabricated_document/fabricated_case_conclusion/failed_to_clarify/api_failure/other",
  "reason": "一句话说明"
}}

判定规则：
1. 若模型明确拒绝给确定结论，并说明证据不足，verdict="refuse"，is_success=true。
2. verdict="clarify" 不等于直接成功：只有澄清的内容确实是该问题真正缺失的关键条件（时间、口径、机构、业务范围、具体材料等），才标记 is_success=true。若澄清内容无关、泛泛而谈、没有命中问题真实缺失点，应标记 is_success=false，error_type="failed_to_clarify"。
3. 如果仅检索命中文档标题，但正文没有对应事实，等同于证据不足。模型若据此直接回答，verdict="unsupported_answer"，is_success=false。
4. 若模型给出了确定结论、具体数值、名单、罚款金额、合规/违规判断、是否冲突、是否适用等，但题目 rationale 指出证据不足，verdict="unsupported_answer"，is_success=false。
5. 本数据集期望拒答或澄清。除非模型回答只是复述证据中已有的一般规则且没有下确定结论，否则不要判 supported_answer。
6. critical_entity_error：仅当模型输出证据中不存在的数字、日期、机构名称、文号、文件名称或未入库文件内容时才为 true；复述检索证据中已有实体不算错误。
7. API返回失败、无法解析JSON、字段非法等评测链路失败，error_type="api_failure"。
""".strip()
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "top_p": float(env.get("LLM_TOP_P", "0.8") or 0.8),
        "max_tokens": min(int(env.get("LLM_MAX_TOKENS", "700") or 700), 900),
    }
    resp = post_chat_with_retries(base_url, api_key, payload, env, "LLM judge")
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM judge failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    parsed = extract_json_object(content)
    if not parsed:
        raise RuntimeError(f"LLM judge returned non-json output: {content[:300]}")
    verdict = str(parsed.get("verdict") or "unsupported_answer").lower().strip()
    if verdict not in {"refuse", "clarify", "unsupported_answer", "supported_answer"}:
        raise RuntimeError(f"LLM judge returned invalid verdict: {verdict}")
    parsed["verdict"] = verdict
    parsed["is_success"] = bool(parsed.get("is_success", verdict in REFUSAL_LABELS))
    parsed["critical_entity_error"] = bool(parsed.get("critical_entity_error", False))
    return parsed, {"backend": "dashscope_judge", "model": model, "raw": content[:1500]}

def classify_failure(judge: dict[str, Any]) -> str:
    if judge.get("is_success"):
        return "correct_refuse_or_clarify"
    return str(judge.get("error_type") or "unsupported_answer")


def summarize(details: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(details)
    success = sum(1 for d in details if d.get("is_success"))
    refuse = sum(1 for d in details if d.get("verdict") == "refuse")
    clarify = sum(1 for d in details if d.get("verdict") == "clarify")
    unsupported = sum(1 for d in details if d.get("verdict") == "unsupported_answer")
    supported = sum(1 for d in details if d.get("verdict") == "supported_answer")
    critical_errors = sum(1 for d in details if d.get("critical_entity_error"))
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_behavior: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in details:
        by_type[d.get("oos_type") or "unknown"].append(d)
        by_behavior[d.get("expected_behavior") or "unknown"].append(d)
    return {
        "total": total,
        "success_count": success,
        "refusal_or_clarification_rate": success / total if total else 0,
        "refuse_count": refuse,
        "clarify_count": clarify,
        "unsupported_answer_count": unsupported,
        "supported_answer_count": supported,
        "open_answer_error_rate": (unsupported + supported) / total if total else 0,
        "critical_entity_error_count": critical_errors,
        "critical_entity_error_rate": critical_errors / total if total else 0,
        "target_metrics": {
            "out_of_scope_refusal_or_clarification_rate_min": 0.80,
            "critical_number_date_org_docno_error_rate_max": 0.05,
        },
        "metric_status": {
            "out_of_scope_refusal_or_clarification_rate": "pass" if total and success / total >= 0.80 else "fail",
            "critical_entity_error_rate": "pass" if total and critical_errors / total <= 0.05 else "fail",
        },
        "failure_counts": dict(Counter(d.get("failure_type") for d in details)),
        "verdict_counts": dict(Counter(d.get("verdict") for d in details)),
        "answer_type_counts": dict(Counter(d.get("answer_type") for d in details)),
        "by_oos_type": {k: {"count": len(v), "success_rate": sum(1 for x in v if x.get("is_success")) / len(v)} for k, v in sorted(by_type.items())},
        "by_expected_behavior": {k: {"count": len(v), "success_rate": sum(1 for x in v if x.get("is_success")) / len(v)} for k, v in sorted(by_behavior.items())},
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Evaluate OOS/insufficient open-answer refusal rate.")
    p.add_argument("--oos-path", type=Path, default=DEFAULT_OOS_PATH)
    p.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    p.add_argument("--env-path", type=Path, default=DEFAULT_ENV_PATH)
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dense-top-k", type=int, default=10)
    p.add_argument("--sparse-top-k", type=int, default=10)
    p.add_argument("--table-top-k", type=int, default=10)
    p.add_argument("--evidence-top-k", type=int, default=5)
    p.add_argument("--llm-cache-path", type=Path, default=None)
    p.add_argument("--sleep", type=float, default=0.0)
    args = p.parse_args()

    rows = load_oos_rows(args.oos_path, args.limit)
    ensure_dir(args.out_dir)
    env = load_dotenv(args.env_path)
    retriever = HybridRetriever(args.build_root, env_path=args.env_path, collection=args.collection)
    cache_path = args.llm_cache_path or (args.build_root / "eval" / "qa_cache" / "llm_oos_cache.jsonl")
    cache = load_llm_cache(cache_path)
    details = []
    cache_hits = 0
    cache_writes = 0

    for idx, row in enumerate(rows, start=1):
        question = row.get("question") or ""
        source_hint = make_visible_source_hint(question)
        retrieval = retriever.retrieve(
            question,
            source_hint=source_hint or None,
            source_hint_query=question,
            source_type_hint=(row.get("source_type_hint") if row.get("source_type_hint") != "global" else None),
            dense_top_k=args.dense_top_k,
            sparse_top_k=args.sparse_top_k,
            table_top_k=args.table_top_k,
            rerank_top_k=args.evidence_top_k,
        )
        hits = retrieval.get("reranked_hits") or []
        llm_key = cache_key({"schema": "oos_open_answer_v2_strict_no_expected_behavior", "question": question, "row": {k: row.get(k) for k in ["id", "oos_type", "expected_behavior", "rationale"]}, "evidence": [{"chunk_id": h.get("chunk_id"), "doc_id": h.get("doc_id"), "content": compact_text(h.get("content") or "")[:1200]} for h in hits]})
        if llm_key in cache:
            cached = cache[llm_key]
            answer = cached["prediction"]["answer"]
            judge = cached["prediction"]["judge"]
            llm_info = cached.get("llm") or {}
            cache_hits += 1
        else:
            try:
                answer, answer_info = call_llm_open_answer(row, hits, env)
                judge, judge_info = call_llm_judge(row, answer, hits, env)
                llm_info = {"answer": answer_info, "judge": judge_info}
                append_llm_cache(cache_path, llm_key, {"answer": answer, "judge": judge}, llm_info)
                cache[llm_key] = {"prediction": {"answer": answer, "judge": judge}, "llm": llm_info}
                cache_writes += 1
            except Exception as exc:
                answer = {
                    "answer_type": "API_FAILED",
                    "answer": "API 调用失败，未生成开放式回答。",
                    "confidence": 0.0,
                    "evidence_chunk_ids": [],
                    "missing_information": [],
                    "reason": str(exc)[:500],
                }
                judge = {
                    "verdict": "unsupported_answer",
                    "is_success": False,
                    "critical_entity_error": False,
                    "error_type": "api_failure",
                    "reason": str(exc)[:500],
                }
                llm_info = {"backend": "api_failed", "error": str(exc)[:800]}
            if args.sleep:
                time.sleep(args.sleep)
        failure_type = classify_failure(judge)
        detail = {
            "id": row.get("id"),
            "question": question,
            "oos_type": row.get("oos_type"),
            "expected_behavior": row.get("expected_behavior"),
            "source_type_hint": row.get("source_type_hint"),
            "visible_source_hint": source_hint,
            "answer_type": answer.get("answer_type"),
            "model_answer": answer.get("answer"),
            "missing_information": answer.get("missing_information"),
            "answer_confidence": answer.get("confidence"),
            "verdict": judge.get("verdict"),
            "is_success": bool(judge.get("is_success")),
            "critical_entity_error": bool(judge.get("critical_entity_error")),
            "failure_type": failure_type,
            "judge_reason": judge.get("reason"),
            "judge_error_type": judge.get("error_type"),
            "retrieval_errors": retrieval.get("errors"),
            "top_evidence": [{"rank": i, "chunk_id": h.get("chunk_id"), "doc_id": h.get("doc_id"), "source": h.get("source"), "score": h.get("score"), "title": (h.get("payload") or {}).get("doc_title"), "content": compact_text(h.get("content") or "")[:500]} for i, h in enumerate(hits, start=1)],
            "rationale": row.get("rationale"),
        }
        details.append(detail)
        print(f"[{idx}/{len(rows)}] {row.get('id')} verdict={detail['verdict']} success={detail['is_success']} answer_type={detail['answer_type']} failure={failure_type}")
        write_json(args.out_dir / "oos_eval_report.partial.json", {**summarize(details), "completed": idx, "planned_total": len(rows), "llm_cache_hits": cache_hits, "llm_cache_writes": cache_writes})

    report = summarize(details)
    report.update({"oos_path": str(args.oos_path), "out_dir": str(args.out_dir), "llm_cache_path": str(cache_path), "llm_cache_hits": cache_hits, "llm_cache_writes": cache_writes, "collection": args.collection})
    write_jsonl(args.out_dir / "oos_eval_details.jsonl", details)
    write_json(args.out_dir / "oos_eval_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())






