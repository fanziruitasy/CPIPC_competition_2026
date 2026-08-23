from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import requests
from qdrant_client import QdrantClient, models

from .index_build import DEFAULT_BUILD_ROOT, DEFAULT_COLLECTION, embed_api_batch, hash_embedding, load_dotenv, sparse_tokens
from .utils import compact_text, ensure_dir, read_json, read_jsonl

DEFAULT_ENV_PATH = Path(r"D:\金融科技大赛\Code\.env")
DEFAULT_DUCKDB_PATH = DEFAULT_BUILD_ROOT / "indexes" / "duckdb" / "rag_tables.duckdb"
DEFAULT_QDRANT_PATH = DEFAULT_BUILD_ROOT / "indexes" / "qdrant_local"

TABLE_HINT_RE = re.compile(r"(表|报表|单元格|数值|金额|余额|比例|比率|增长|下降|同比|环比|合计|小计|平均|最大|最小|多少|第.+行|第.+列|取值|填报)")
HINT_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}")
TITLE_HINT_RE = re.compile(r"《([^》]{2,120})》")
DOCNO_HINT_RE = re.compile(r"[\u4e00-\u9fa5]{0,12}[〔\[]\d{4}[〕\]][^\s，。；;、]{0,30}?号")
ATTACHMENT_HINT_RE = re.compile(r"(?:附件|附录)\s*[0-9一二三四五六七八九十]+[：:、]?\s*[\u4e00-\u9fa5A-Za-z0-9（）()《》\-—_]{0,80}")
ORG_HINT_RE = re.compile(
    r"(国家金融监督管理总局|中国银保监会|银保监会|中国人民银行|财政部|国务院|"
    r"国家知识产权局|国家版权局|金融监管总局)"
)


@dataclass
class RetrievalHit:
    hit_id: str
    chunk_id: str | None
    doc_id: str | None
    source: str
    score: float
    rank: int
    content: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "hit_id": self.hit_id,
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "source": self.source,
            "score": self.score,
            "rank": self.rank,
            "content": self.content,
            "payload": self.payload,
        }


def _dense_query_text(query: str) -> str:
    return compact_text(query)[:12000]


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _load_embedding_cache(path: Path) -> dict[str, list[float]]:
    cache: dict[str, list[float]] = {}
    if not path.exists():
        return cache
    for row in read_jsonl(path):
        key = row.get("key")
        vector = row.get("vector")
        if key and isinstance(vector, list):
            cache[key] = [float(x) for x in vector]
    return cache


def _append_embedding_cache(path: Path, key: str, text: str, vector: list[float]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "text": text, "vector": vector}, ensure_ascii=False) + "\n")


def _hint_terms(source_hint: str | None) -> list[str]:
    if not source_hint:
        return []
    text = compact_text(source_hint).lower()
    text = re.sub(r"\.(docx?|pdf|xlsx?)\b", "", text)
    terms = HINT_TOKEN_RE.findall(text)
    long_terms = [t for t in terms if len(t) >= 3]
    return list(dict.fromkeys(long_terms or terms))


def _hint_phrases(source_hint: str | None) -> list[str]:
    if not source_hint:
        return []
    phrases: list[str] = []
    for part in re.split(r"\s*\|\s*|\n", source_hint):
        text = compact_text(part).lower()
        text = re.sub(r"\.(docx?|pdf|xlsx?)\b", "", text)
        text = text.strip("《》 ")
        if len(text) >= 4 and text not in phrases:
            phrases.append(text)
    return phrases


def extract_source_hints_from_question(question: str) -> list[str]:
    """Extract only user-visible source hints from the question text.

    This is deliberately limited to text that appears in the query itself:
    document titles in 《...》, regulatory document numbers, attachment labels,
    and agency names. QA-only fields such as source_title/file_label/evidence
    must not be passed here.
    """
    text = compact_text(question or "")
    hints: list[str] = []
    for pattern in [TITLE_HINT_RE, DOCNO_HINT_RE, ATTACHMENT_HINT_RE, ORG_HINT_RE]:
        for match in pattern.finditer(text):
            value = match.group(1) if pattern is TITLE_HINT_RE or pattern is ORG_HINT_RE else match.group(0)
            value = compact_text(value)
            if value and value not in hints:
                hints.append(value)
    return hints


def make_visible_source_hint(question: str) -> str:
    return " | ".join(extract_source_hints_from_question(question))


def _preferred_profile(source_type_hint: str | None) -> str | None:
    value = (source_type_hint or "").strip().lower()
    if value == "pdf":
        return "pdf"
    if value == "word":
        return "docx"
    return None


def _is_preferred_profile(payload: dict[str, Any], source_type_hint: str | None) -> bool:
    preferred = _preferred_profile(source_type_hint)
    if preferred is None:
        return False
    profile = str(payload.get("source_profile") or "").lower()
    path = str(payload.get("source_path") or payload.get("doc_title") or "").lower()
    if preferred == "pdf":
        return profile == "pdf" or path.endswith(".pdf")
    return profile in {"native-docx", "converted-docx"} or path.endswith(".docx") or path.endswith(".doc")


def _target_match_score(payload: dict[str, Any], source_hint: str | None, source_type_hint: str | None = None) -> float:
    terms = _hint_terms(source_hint)
    hay = " ".join(
        str(payload.get(key) or "")
        for key in ["doc_title", "source_path", "section_path", "doc_id", "source_profile"]
    ).lower()
    score = 0.0
    if terms:
        score += min(1.0, sum(1 for term in terms if term in hay) / max(1, len(terms)))
    if _is_preferred_profile(payload, source_type_hint):
        score += 0.25
    return score


def _load_chunks_by_id(build_root: Path) -> dict[str, dict[str, Any]]:
    return {row["chunk_id"]: row for row in read_jsonl(build_root / "chunks.jsonl") if row.get("chunk_id")}


def _load_docs_by_id(build_root: Path) -> dict[str, dict[str, Any]]:
    return {row["doc_id"]: row for row in read_jsonl(build_root / "normalized_documents.jsonl") if row.get("doc_id")}


def build_sparse_query(query: str, vocab: dict[str, int], chunks: list[dict[str, Any]]) -> models.SparseVector:
    # The current index artifact stores vocab but not idf; recompute idf from the same ready chunk corpus.
    df: Counter[str] = Counter()
    for chunk in chunks:
        text = chunk.get("bm25_text") or chunk.get("embedding_text") or chunk.get("content_text") or ""
        df.update(set(sparse_tokens(text)))
    n = max(1, len(chunks))
    pairs: list[tuple[int, float]] = []
    for term, tf in Counter(sparse_tokens(query)).items():
        idx = vocab.get(term)
        if idx is None:
            continue
        term_df = df.get(term, 0)
        idf = math.log(1 + (n - term_df + 0.5) / (term_df + 0.5)) if term_df else math.log(1 + n)
        value = (1 + math.log(tf)) * idf
        if value > 0:
            pairs.append((idx, float(value)))
    pairs.sort(key=lambda x: x[0])
    return models.SparseVector(indices=[idx for idx, _ in pairs], values=[value for _, value in pairs])


def build_sparse_df(chunks: list[dict[str, Any]]) -> Counter[str]:
    df: Counter[str] = Counter()
    for chunk in chunks:
        text = chunk.get("bm25_text") or chunk.get("embedding_text") or chunk.get("content_text") or ""
        df.update(set(sparse_tokens(text)))
    return df


def build_sparse_query_from_df(query: str, vocab: dict[str, int], df: Counter[str], n: int) -> models.SparseVector:
    pairs: list[tuple[int, float]] = []
    for term, tf in Counter(sparse_tokens(query)).items():
        idx = vocab.get(term)
        if idx is None:
            continue
        term_df = df.get(term, 0)
        idf = math.log(1 + (n - term_df + 0.5) / (term_df + 0.5)) if term_df else math.log(1 + n)
        value = (1 + math.log(tf)) * idf
        if value > 0:
            pairs.append((idx, float(value)))
    pairs.sort(key=lambda x: x[0])
    return models.SparseVector(indices=[idx for idx, _ in pairs], values=[value for _, value in pairs])


def _dashscope_rerank_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if "compatible-mode" in base:
        base = "https://dashscope.aliyuncs.com"
    return base + "/api/v1/services/rerank/text-rerank/text-rerank"


def call_dashscope_rerank(query: str, documents: list[str], env: dict[str, str], top_n: int) -> list[tuple[int, float]]:
    api_key = env.get("DASHSCOPE_API_KEY", "")
    model = env.get("RERANK_MODEL", "")
    base_url = env.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com")
    if not api_key or not model or not documents:
        raise RuntimeError("Missing rerank config")
    payload = {
        "model": model,
        "input": {"query": query, "documents": documents},
        "parameters": {"return_documents": False, "top_n": min(top_n, len(documents))},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = float(env.get("RERANK_TIMEOUT_SECONDS", "60") or 60)
    resp = requests.post(_dashscope_rerank_url(base_url), headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"Rerank API failed {resp.status_code}: {resp.text[:300]}")
    rows = ((resp.json().get("output") or {}).get("results") or resp.json().get("results") or [])
    pairs: list[tuple[int, float]] = []
    for row in rows:
        idx = row.get("index")
        score = row.get("relevance_score", row.get("score", 0.0))
        if isinstance(idx, int):
            pairs.append((idx, float(score)))
    if not pairs:
        raise RuntimeError("Rerank API returned no ranked rows")
    return pairs


def lexical_rerank(query: str, hits: list[RetrievalHit]) -> list[RetrievalHit]:
    q_terms = set(sparse_tokens(query))
    for hit in hits:
        hit.score += len(q_terms & set(sparse_tokens(hit.content))) * 0.02
    return sorted(hits, key=lambda x: x.score, reverse=True)


class HybridRetriever:
    def __init__(
        self,
        build_root: Path = DEFAULT_BUILD_ROOT,
        env_path: Path = DEFAULT_ENV_PATH,
        collection: str = DEFAULT_COLLECTION,
        qdrant_path: Path = DEFAULT_QDRANT_PATH,
        duckdb_path: Path = DEFAULT_DUCKDB_PATH,
        embedding_backend: str = "api",
    ) -> None:
        self.build_root = build_root
        self.env = load_dotenv(env_path)
        self.collection = collection
        self.qdrant_path = qdrant_path
        self.duckdb_path = duckdb_path
        self.embedding_backend = embedding_backend
        self.chunks_by_id = _load_chunks_by_id(build_root)
        self.ready_chunks = [c for c in self.chunks_by_id.values() if c.get("quality_status") == "ready"]
        self.docs_by_id = _load_docs_by_id(build_root)
        vocab_path = build_root / "indexes" / "bm25s" / "jieba_sparse_vocab.json"
        vocab_obj = read_json(vocab_path)
        self.vocab = vocab_obj.get("vocab", vocab_obj)
        self.sparse_df = build_sparse_df(self.ready_chunks)
        self.sparse_n = max(1, len(self.ready_chunks))
        self._sparse_query_cache: dict[str, models.SparseVector] = {}
        self.query_embedding_cache_path = build_root / "indexes" / "embedding_cache" / f"query_{embedding_backend}_embeddings.jsonl"
        self.query_embedding_cache = _load_embedding_cache(self.query_embedding_cache_path)
        self.client = QdrantClient(path=str(qdrant_path))

    def close(self) -> None:
        self.client.close()

    def route_query(self, query: str) -> dict[str, Any]:
        use_duckdb = bool(TABLE_HINT_RE.search(query))
        return {
            "use_dense": True,
            "use_sparse": True,
            "use_duckdb": use_duckdb,
            "query_type": "table" if use_duckdb else "policy_fact",
        }

    def embed_query(self, query: str) -> list[float]:
        text = _dense_query_text(query)
        if self.embedding_backend == "hash":
            dim = int(self.env.get("EMBEDDING_DIMENSIONS", "1024") or 1024)
            return hash_embedding(text, dim)
        key = _cache_key(text)
        cached = self.query_embedding_cache.get(key)
        if cached is not None:
            return cached
        max_retries = int(self.env.get("EMBEDDING_MAX_RETRIES", "3") or 3)
        backoff = float(self.env.get("EMBEDDING_RETRY_BACKOFF_SECONDS", "2") or 2)
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                vector = [float(x) for x in embed_api_batch([text], self.env)[0]]
                self.query_embedding_cache[key] = vector
                _append_embedding_cache(self.query_embedding_cache_path, key, text, vector)
                return vector
            except Exception as exc:
                last_err = exc
                time.sleep(backoff * (attempt + 1))
        raise RuntimeError(f"Query embedding failed: {last_err}")

    def sparse_query(self, query: str) -> models.SparseVector:
        if query not in self._sparse_query_cache:
            self._sparse_query_cache[query] = build_sparse_query_from_df(query, self.vocab, self.sparse_df, self.sparse_n)
        return self._sparse_query_cache[query]

    def chunk_content(self, chunk_id: str | None) -> str:
        if not chunk_id:
            return ""
        chunk = self.chunks_by_id.get(chunk_id) or {}
        return chunk.get("content_text") or chunk.get("embedding_text") or ""

    def enrich_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        doc = self.docs_by_id.get(str(payload.get("doc_id"))) or {}
        out = dict(payload)
        out["source_path"] = doc.get("source_path")
        out["doc_title"] = doc.get("title") or doc.get("file_name") or Path(str(doc.get("source_path") or "")).name
        return out

    def qdrant_search(self, query: str, using: str, limit: int) -> list[RetrievalHit]:
        if using == "dense":
            q = self.embed_query(query)
        else:
            q = self.sparse_query(query)
            if not q.indices:
                return []
        resp = self.client.query_points(
            collection_name=self.collection,
            query=q,
            using=using,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        points = sorted(resp.points, key=lambda p: float(p.score or 0.0), reverse=True)
        hits: list[RetrievalHit] = []
        for rank, point in enumerate(points, start=1):
            payload = self.enrich_payload(point.payload or {})
            cid = payload.get("chunk_id")
            hits.append(
                RetrievalHit(
                    hit_id=str(cid or point.id),
                    chunk_id=cid,
                    doc_id=payload.get("doc_id"),
                    source=using,
                    score=float(point.score or 0.0),
                    rank=rank,
                    content=self.chunk_content(cid),
                    payload=payload,
                )
            )
        return hits

    def duckdb_search(self, query: str, limit: int) -> list[RetrievalHit]:
        if not self.duckdb_path.exists():
            return []
        tokens = [t for t, _ in Counter(sparse_tokens(query)).most_common(10) if len(t) >= 2 or re.search(r"\d", t)]
        if not tokens:
            return []
        clauses: list[str] = []
        params: list[str] = []
        for tok in tokens[:8]:
            pat = f"%{tok}%"
            clauses.append("(value_text LIKE ? OR CAST(row_headers AS VARCHAR) LIKE ? OR CAST(col_headers AS VARCHAR) LIKE ?)")
            params.extend([pat, pat, pat])
        sql = f"""
            SELECT fact_id, doc_id, source_profile, table_id, row_idx, col_idx,
                   CAST(row_headers AS VARCHAR) AS row_headers,
                   CAST(col_headers AS VARCHAR) AS col_headers,
                   value_text, value_number, unit, source_cell_id
            FROM table_facts
            WHERE {' OR '.join(clauses)}
            LIMIT 80
        """
        con = duckdb.connect(str(self.duckdb_path), read_only=True)
        try:
            rows = con.execute(sql, params).fetchall()
            cols = [d[0] for d in con.description]
        finally:
            con.close()
        table_to_chunk = {}
        for chunk in self.ready_chunks:
            table_id = (chunk.get("modality_ref") or {}).get("table_id")
            if table_id and table_id not in table_to_chunk:
                table_to_chunk[table_id] = chunk["chunk_id"]
        q_terms = set(tokens)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            rec = dict(zip(cols, row))
            text = " | ".join(str(rec.get(k) or "") for k in ["row_headers", "col_headers", "value_text", "unit"])
            score = len(q_terms & set(sparse_tokens(text))) + (0.2 if re.search(r"\d", str(rec.get("value_text") or "")) else 0.0)
            scored.append((score, rec))
        scored.sort(key=lambda x: x[0], reverse=True)
        hits: list[RetrievalHit] = []
        for rank, (score, rec) in enumerate(scored[:limit], start=1):
            cid = table_to_chunk.get(str(rec.get("table_id")))
            payload = self.enrich_payload(
                {
                    "chunk_id": cid,
                    "doc_id": rec.get("doc_id"),
                    "source_profile": rec.get("source_profile"),
                    "chunk_type": "table_fact",
                    "table_id": rec.get("table_id"),
                    "fact_id": rec.get("fact_id"),
                    "row_idx": rec.get("row_idx"),
                    "col_idx": rec.get("col_idx"),
                    "value_number": rec.get("value_number"),
                    "unit": rec.get("unit"),
                    "source_cell_id": rec.get("source_cell_id"),
                }
            )
            content = f"表格事实：行头={rec.get('row_headers')}；列头={rec.get('col_headers')}；值={rec.get('value_text')}；单位={rec.get('unit') or ''}"
            if cid:
                content += "\n" + self.chunk_content(cid)[:1200]
            hits.append(
                RetrievalHit(
                    hit_id=str(rec.get("fact_id")),
                    chunk_id=cid,
                    doc_id=rec.get("doc_id"),
                    source="duckdb_tablefact",
                    score=float(score),
                    rank=rank,
                    content=content,
                    payload=payload,
                )
            )
        return hits

    def source_hint_search(self, source_hint: str | None, query: str, limit: int, source_type_hint: str | None = None) -> list[RetrievalHit]:
        terms = _hint_terms(source_hint)
        phrases = _hint_phrases(source_hint)
        if not terms and not phrases:
            return []
        exact_doc_ids: list[str] = []
        doc_scores: list[tuple[int, str]] = []
        for doc_id, doc in self.docs_by_id.items():
            hay = " ".join(str(doc.get(key) or "") for key in ["title", "file_name", "source_path", "doc_id"]).lower()
            if phrases and any(phrase in hay for phrase in phrases):
                exact_doc_ids.append(doc_id)
                continue
            score = sum(1 for term in terms if term in hay)
            if _is_preferred_profile(
                {
                    "source_profile": doc.get("source_profile"),
                    "source_path": doc.get("source_path"),
                    "doc_title": doc.get("title") or doc.get("file_name"),
                },
                source_type_hint,
            ):
                score += 1
            if score:
                doc_scores.append((score, doc_id))
        doc_scores.sort(key=lambda x: x[0], reverse=True)
        target_doc_ids = set(exact_doc_ids or [doc_id for _, doc_id in doc_scores[:5]])
        if not target_doc_ids:
            return []
        q_terms = set(sparse_tokens(query))
        scored_chunks: list[tuple[float, dict[str, Any]]] = []
        for chunk in self.ready_chunks:
            if str(chunk.get("doc_id")) not in target_doc_ids:
                continue
            text = chunk.get("bm25_text") or chunk.get("embedding_text") or chunk.get("content_text") or ""
            overlap = len(q_terms & set(sparse_tokens(text)))
            type_bonus = 0.3 if chunk.get("chunk_type") in {"text", "table_rows", "table_summary"} else 0.0
            scored_chunks.append((overlap + type_bonus, chunk))
        scored_chunks.sort(key=lambda x: x[0], reverse=True)
        hits: list[RetrievalHit] = []
        for rank, (score, chunk) in enumerate(scored_chunks[:limit], start=1):
            ref = chunk.get("modality_ref") or {}
            payload = self.enrich_payload(
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "doc_id": chunk.get("doc_id"),
                    "source_profile": chunk.get("source_profile"),
                    "chunk_type": chunk.get("chunk_type"),
                    "quality_status": chunk.get("quality_status"),
                    "page_start": chunk.get("page_start"),
                    "page_end": chunk.get("page_end"),
                    "section_path": " > ".join(chunk.get("section_path") or []),
                    "table_id": ref.get("table_id"),
                    "figure_id": ref.get("figure_id"),
                    "formula_id": ref.get("formula_id"),
                    "parent_chunk_id": chunk.get("parent_chunk_id"),
                    "token_count": chunk.get("token_count"),
                }
            )
            hits.append(
                RetrievalHit(
                    hit_id=str(chunk.get("chunk_id")),
                    chunk_id=chunk.get("chunk_id"),
                    doc_id=chunk.get("doc_id"),
                    source="source_hint",
                    score=float(score),
                    rank=rank,
                    content=chunk.get("content_text") or chunk.get("embedding_text") or "",
                    payload=payload,
                )
            )
        return hits

    @staticmethod
    def rrf_fuse(hit_lists: list[list[RetrievalHit]], k: int = 60) -> list[RetrievalHit]:
        by_key: dict[str, RetrievalHit] = {}
        scores: defaultdict[str, float] = defaultdict(float)
        sources: defaultdict[str, list[str]] = defaultdict(list)
        for hits in hit_lists:
            for hit in hits:
                key = hit.chunk_id or hit.hit_id
                if key not in by_key:
                    by_key[key] = hit
                    by_key[key].score = 0.0
                scores[key] += 1.0 / (k + max(1, hit.rank))
                if hit.source not in sources[key]:
                    sources[key].append(hit.source)
        fused = []
        for key, hit in by_key.items():
            hit.score = scores[key]
            hit.payload["retrieval_sources"] = sources[key]
            hit.source = "+".join(sources[key])
            fused.append(hit)
        return sorted(fused, key=lambda x: x.score, reverse=True)

    def apply_source_hint_boost(self, hits: list[RetrievalHit], source_hint: str | None, source_type_hint: str | None = None) -> list[RetrievalHit]:
        terms = _hint_terms(source_hint)
        if not terms and not source_type_hint:
            return hits
        hint_text = compact_text(source_hint or "").lower()
        for hit in hits:
            payload = hit.payload or {}
            hay = " ".join(
                str(payload.get(key) or "")
                for key in ["doc_title", "source_path", "section_path", "doc_id", "source_profile"]
            ).lower()
            exact_bonus = 0.05 if hint_text and hint_text in hay else 0.0
            token_bonus = min(0.08, 0.015 * sum(1 for term in terms if term in hay))
            profile_bonus = 0.04 if _is_preferred_profile(payload, source_type_hint) else 0.0
            target_bonus = 0.06 if _target_match_score(payload, source_hint, source_type_hint) >= 0.75 else 0.0
            boost = exact_bonus + token_bonus + profile_bonus + target_bonus
            if boost:
                hit.score += boost
                hit.payload["source_hint_boost"] = boost
        return sorted(hits, key=lambda x: x.score, reverse=True)

    def order_evidence(self, hits: list[RetrievalHit], source_hint: str | None, source_type_hint: str | None = None) -> list[RetrievalHit]:
        for hit in hits:
            target_score = _target_match_score(hit.payload or {}, source_hint, source_type_hint)
            hit.payload["target_match_score"] = target_score
            hit.payload["preferred_profile"] = _is_preferred_profile(hit.payload or {}, source_type_hint)
        return sorted(hits, key=lambda h: (h.payload.get("target_match_score") or 0.0, h.score), reverse=True)

    def rerank(self, query: str, hits: list[RetrievalHit], top_n: int, source_hint: str | None = None) -> tuple[list[RetrievalHit], dict[str, Any]]:
        if not hits:
            return [], {"backend": "none", "ok": True}
        docs = [h.content[:4000] for h in hits]
        rerank_query = query if not source_hint else f"目标资料：{source_hint}\n问题：{query}"
        max_retries = int(self.env.get("RERANK_MAX_RETRIES", "2") or 2)
        backoff = float(self.env.get("RERANK_RETRY_BACKOFF_SECONDS", "2") or 2)
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                pairs = call_dashscope_rerank(rerank_query, docs, self.env, min(top_n, len(hits)))
                ranked = []
                for idx, score in pairs:
                    if 0 <= idx < len(hits):
                        hit = hits[idx]
                        hit.score = score
                        ranked.append(hit)
                return ranked[:top_n], {"backend": "dashscope", "ok": True, "model": self.env.get("RERANK_MODEL")}
            except Exception as exc:
                last_err = exc
                time.sleep(backoff * (attempt + 1))
        return lexical_rerank(query, hits)[:top_n], {"backend": "lexical_fallback", "ok": False, "error": str(last_err)[:300] if last_err else None}

    def retrieve(
        self,
        query: str,
        source_hint: str | None = None,
        source_hint_query: str | None = None,
        source_type_hint: str | None = None,
        dense_top_k: int = 10,
        sparse_top_k: int = 10,
        table_top_k: int = 10,
        rrf_top_k: int = 30,
        rerank_top_k: int = 5,
    ) -> dict[str, Any]:
        route = self.route_query(query)
        errors: dict[str, str] = {}
        try:
            dense_hits = self.qdrant_search(query, "dense", dense_top_k) if route["use_dense"] else []
        except Exception as exc:
            dense_hits = []
            errors["dense"] = str(exc)[:500]
        try:
            sparse_hits = self.qdrant_search(query, "sparse", sparse_top_k) if route["use_sparse"] else []
        except Exception as exc:
            sparse_hits = []
            errors["sparse"] = str(exc)[:500]
        try:
            duckdb_hits = self.duckdb_search(query, table_top_k) if route["use_duckdb"] else []
        except Exception as exc:
            duckdb_hits = []
            errors["duckdb"] = str(exc)[:500]
        try:
            source_hint_hits = self.source_hint_search(source_hint, source_hint_query or query, max(dense_top_k, sparse_top_k), source_type_hint=source_type_hint)
        except Exception as exc:
            source_hint_hits = []
            errors["source_hint"] = str(exc)[:500]
        fused = self.rrf_fuse([dense_hits, sparse_hits, duckdb_hits, source_hint_hits])
        fused = self.apply_source_hint_boost(fused, source_hint, source_type_hint)[:rrf_top_k]
        reranked, rerank_info = self.rerank(query, fused, rerank_top_k, source_hint=source_hint)
        reranked = self.order_evidence(reranked, source_hint, source_type_hint)
        return {
            "query": query,
            "route": route,
            "source_hint": source_hint,
            "source_type_hint": source_type_hint,
            "errors": errors,
            "dense_hits": [h.to_dict() for h in dense_hits],
            "sparse_hits": [h.to_dict() for h in sparse_hits],
            "duckdb_hits": [h.to_dict() for h in duckdb_hits],
            "source_hint_hits": [h.to_dict() for h in source_hint_hits],
            "fused_hits": [h.to_dict() for h in fused],
            "reranked_hits": [h.to_dict() for h in reranked],
            "rerank": rerank_info,
        }

    def retrieve_mcq(
        self,
        question: str,
        options: dict[str, str],
        source_hint: str | None = None,
        source_type_hint: str | None = None,
        dense_top_k: int = 10,
        sparse_top_k: int = 10,
        table_top_k: int = 10,
        rrf_top_k: int = 30,
        rerank_top_k: int = 5,
    ) -> dict[str, Any]:
        visible_hint = source_hint or make_visible_source_hint(question)
        if not visible_hint:
            fallback = self.retrieve(
                question,
                source_hint=None,
                source_hint_query=None,
                source_type_hint=source_type_hint,
                dense_top_k=dense_top_k,
                sparse_top_k=sparse_top_k,
                table_top_k=table_top_k,
                rrf_top_k=rrf_top_k,
                rerank_top_k=rerank_top_k,
            )
            fallback["retrieval_mode"] = "global_mixed_no_visible_target"
            fallback["option_evidence_pack"] = {}
            return fallback

        errors: dict[str, str] = {}
        option_pack: dict[str, list[dict[str, Any]]] = {}
        hit_lists: list[list[RetrievalHit]] = []
        try:
            question_hits = self.source_hint_search(
                visible_hint,
                question,
                max(dense_top_k, sparse_top_k),
                source_type_hint=source_type_hint,
            )
        except Exception as exc:
            question_hits = []
            errors["target_question"] = str(exc)[:500]
        hit_lists.append(question_hits)

        for key, option in options.items():
            option_query = f"{question}\n{key}. {option}"
            try:
                option_hits = self.source_hint_search(
                    visible_hint,
                    option_query,
                    max(dense_top_k, sparse_top_k),
                    source_type_hint=source_type_hint,
                )
            except Exception as exc:
                option_hits = []
                errors[f"option_{key}"] = str(exc)[:500]
            hit_lists.append(option_hits)
            option_pack[key] = [h.to_dict() for h in option_hits[:rerank_top_k]]

        fused = self.rrf_fuse(hit_lists)
        fused = self.apply_source_hint_boost(fused, visible_hint, source_type_hint)[:rrf_top_k]
        rerank_query = "\n".join([question] + [f"{k}. {v}" for k, v in options.items() if v])
        reranked, rerank_info = self.rerank(rerank_query, fused, rerank_top_k, source_hint=visible_hint)
        reranked = self.order_evidence(reranked, visible_hint, source_type_hint)
        return {
            "query": question,
            "retrieval_mode": "target_doc_option_pack",
            "route": {
                "use_dense": False,
                "use_sparse": False,
                "use_duckdb": False,
                "query_type": "mcq_policy_fact",
                "target_doc_only": True,
            },
            "source_hint": visible_hint,
            "source_type_hint": source_type_hint,
            "errors": errors,
            "dense_hits": [],
            "sparse_hits": [],
            "duckdb_hits": [],
            "source_hint_hits": [h.to_dict() for h in question_hits],
            "option_evidence_pack": option_pack,
            "fused_hits": [h.to_dict() for h in fused],
            "reranked_hits": [h.to_dict() for h in reranked],
            "rerank": rerank_info,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hybrid dense+sparse+DuckDB retrieval over docling0816 indexes.")
    p.add_argument("query")
    p.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    p.add_argument("--env-path", type=Path, default=DEFAULT_ENV_PATH)
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--qdrant-path", type=Path, default=DEFAULT_QDRANT_PATH)
    p.add_argument("--duckdb-path", type=Path, default=DEFAULT_DUCKDB_PATH)
    p.add_argument("--embedding-backend", choices=["api", "hash"], default="api")
    p.add_argument("--source-hint", default=None)
    p.add_argument("--top-k", type=int, default=5)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    retriever = HybridRetriever(args.build_root, args.env_path, args.collection, args.qdrant_path, args.duckdb_path, args.embedding_backend)
    try:
        result = retriever.retrieve(args.query, source_hint=args.source_hint, rerank_top_k=args.top_k)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        retriever.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
