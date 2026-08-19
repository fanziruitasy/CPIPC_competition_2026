from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import duckdb
import jieba
import requests
from qdrant_client import QdrantClient, models

from .indexes import hash_embedding
from .utils import ensure_dir, read_jsonl, tokenize_for_index, write_json

DEFAULT_BUILD_ROOT = Path(r"D:\金融科技大赛\Code\data\processed\docling0816\p0_full")
DEFAULT_COLLECTION = "rag_chunks_docling0816"

USER_TERMS = [
    "国家金融监督管理总局", "中国银保监会", "银保监会", "商业银行资本管理办法", "资本充足率",
    "风险加权资产", "流动性覆盖率", "非现场监管", "监管报表", "资本计量高级方法",
    "内部评级法", "资产证券化", "操作风险", "市场风险", "信用风险", "偿付能力",
    "资本要求", "风险暴露", "一级资本", "核心一级资本", "杠杆率", "流动性风险",
]

NUMERIC_RE = re.compile(
    r"(?P<num>[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[-+]?\d+(?:\.\d+)?)"
    r"(?P<unit>\s*(?:%|％|‰|亿元|万元|元|年|月|日|倍|个|家|项|笔)?)"
)


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        key = key.strip()
        env[key] = os.environ.get(key, value)
        os.environ.setdefault(key, value)
    return env


def stable_point_id(chunk_id: str) -> int:
    return int(hashlib.sha1(chunk_id.encode("utf-8")).hexdigest()[:15], 16)


def parse_numeric(text: str | None) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    text = str(text).strip()
    if not text or text in {"—", "-", "--", "无", "不适用", "N/A", "NA"}:
        return None, None
    if re.search(r"\d\s*[-~至]\s*\d", text):
        return None, None
    m = NUMERIC_RE.search(text)
    if not m:
        return None, None
    try:
        number = float(m.group("num").replace(",", ""))
    except Exception:
        return None, None
    unit = (m.group("unit") or "").strip() or None
    return number, unit


def init_jieba() -> None:
    for term in USER_TERMS:
        jieba.add_word(term)


def sparse_tokens(text: str) -> list[str]:
    init_jieba()
    rough = jieba.lcut(text or "")
    out: list[str] = []
    for tok in rough:
        tok = tok.strip().lower()
        if not tok:
            continue
        if re.fullmatch(r"[\W_]+", tok, flags=re.UNICODE):
            continue
        out.append(tok)
    out.extend(tokenize_for_index(text))
    return out


def build_sparse_vectors(chunks: list[dict[str, Any]], max_features: int = 80000) -> tuple[dict[str, int], dict[str, models.SparseVector], dict[str, Any]]:
    doc_tf: dict[str, Counter[str]] = {}
    df: Counter[str] = Counter()
    for chunk in chunks:
        cid = chunk["chunk_id"]
        text = chunk.get("bm25_text") or chunk.get("embedding_text") or chunk.get("content_text") or ""
        counts = Counter(sparse_tokens(text))
        doc_tf[cid] = counts
        df.update(counts.keys())
    n = max(1, len(doc_tf))
    vocab_terms = [term for term, _ in df.most_common(max_features)]
    vocab = {term: idx for idx, term in enumerate(vocab_terms)}
    sparse: dict[str, models.SparseVector] = {}
    lengths: list[int] = []
    for cid, counts in doc_tf.items():
        pairs = []
        for term, tf in counts.items():
            idx = vocab.get(term)
            if idx is None:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            value = (1 + math.log(tf)) * idf
            if value > 0:
                pairs.append((idx, float(value)))
        pairs.sort(key=lambda x: x[0])
        lengths.append(len(pairs))
        sparse[cid] = models.SparseVector(indices=[p[0] for p in pairs], values=[p[1] for p in pairs])
    stats = {
        "vocab_size": len(vocab),
        "avg_sparse_terms": sum(lengths) / len(lengths) if lengths else 0,
        "max_sparse_terms": max(lengths) if lengths else 0,
        "tokenizer": "jieba+regulatory_terms+fallback.v1",
    }
    return vocab, sparse, stats


def dashscope_embedding_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/compatible-mode/v1") or base_url.endswith("/v1"):
        return base_url + "/embeddings"
    return base_url + "/embeddings"


def embed_api_batch(texts: list[str], env: dict[str, str]) -> list[list[float]]:
    base_url = env.get("DASHSCOPE_BASE_URL", "")
    api_key = env.get("DASHSCOPE_API_KEY", "")
    model = env.get("EMBEDDING_MODEL", "")
    timeout = float(env.get("EMBEDDING_TIMEOUT_SECONDS", "60") or 60)
    if not base_url or not api_key or not model:
        raise RuntimeError("Missing DASHSCOPE_BASE_URL / DASHSCOPE_API_KEY / EMBEDDING_MODEL in .env")
    url = dashscope_embedding_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": texts}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"Embedding API failed {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    rows = data.get("data") or []
    rows = sorted(rows, key=lambda x: x.get("index", 0))
    vectors = [r.get("embedding") for r in rows]
    if len(vectors) != len(texts) or any(v is None for v in vectors):
        raise RuntimeError("Embedding API returned malformed data")
    return vectors


def build_dense_vectors(
    chunks: list[dict[str, Any]],
    backend: str,
    env: dict[str, str],
    dim: int | None,
    cache_path: Path,
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    ensure_dir(cache_path.parent)
    cache: dict[str, list[float]] = {}
    if cache_path.exists():
        for row in read_jsonl(cache_path):
            cache[row["chunk_id"]] = row["vector"]
    vectors: dict[str, list[float]] = {}
    if backend == "hash":
        use_dim = dim or int(env.get("EMBEDDING_DIMENSIONS", "1024") or 1024)
        for chunk in chunks:
            text = chunk.get("embedding_text") or chunk.get("content_text") or ""
            vectors[chunk["chunk_id"]] = hash_embedding(text, use_dim)
        return vectors, {"backend": "hash", "dim": use_dim, "cached": 0, "created": len(vectors)}

    missing = [c for c in chunks if c["chunk_id"] not in cache]
    batch_size = int(env.get("EMBEDDING_BATCH_SIZE", "16") or 16)
    max_retries = int(env.get("EMBEDDING_MAX_RETRIES", "3") or 3)
    backoff = float(env.get("EMBEDDING_RETRY_BACKOFF_SECONDS", "2") or 2)
    created_total = 0
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        texts = [(c.get("embedding_text") or c.get("content_text") or "")[:12000] for c in batch]
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                embs = embed_api_batch(texts, env)
                with cache_path.open("a", encoding="utf-8") as f:
                    for chunk, vec in zip(batch, embs):
                        clean_vec = [float(x) for x in vec]
                        cache[chunk["chunk_id"]] = clean_vec
                        f.write(json.dumps({"chunk_id": chunk["chunk_id"], "vector": clean_vec}, ensure_ascii=False) + "\n")
                        created_total += 1
                break
            except Exception as exc:
                last_err = exc
                time.sleep(backoff * (attempt + 1))
        else:
            raise RuntimeError(f"Embedding batch failed at offset {start}: {last_err}")
    for chunk in chunks:
        vectors[chunk["chunk_id"]] = cache[chunk["chunk_id"]]
    inferred_dim = len(next(iter(vectors.values()))) if vectors else 0
    return vectors, {
        "backend": "api",
        "model": env.get("EMBEDDING_MODEL"),
        "dim": inferred_dim,
        "cached": len(chunks) - len(missing),
        "created": created_total,
        "cache_path": str(cache_path),
    }


def build_dense_vectors(
    chunks: list[dict[str, Any]],
    backend: str,
    env: dict[str, str],
    dim: int | None,
    cache_path: Path,
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ensure_dir(cache_path.parent)
    cache: dict[str, list[float]] = {}
    if cache_path.exists():
        for row in read_jsonl(cache_path):
            cache[row["chunk_id"]] = row["vector"]
    if backend == "hash":
        use_dim = dim or int(env.get("EMBEDDING_DIMENSIONS", "1024") or 1024)
        vectors = {}
        for chunk in chunks:
            text = chunk.get("embedding_text") or chunk.get("content_text") or ""
            vectors[chunk["chunk_id"]] = hash_embedding(text, use_dim)
        return vectors, {"backend": "hash", "dim": use_dim, "cached": 0, "created": len(vectors)}

    missing = [c for c in chunks if c["chunk_id"] not in cache]
    batch_size = int(env.get("EMBEDDING_BATCH_SIZE", "8") or 8)
    max_retries = int(env.get("EMBEDDING_MAX_RETRIES", "5") or 5)
    backoff = float(env.get("EMBEDDING_RETRY_BACKOFF_SECONDS", "4") or 4)
    concurrency = int(env.get("EMBEDDING_CONCURRENCY", "4") or 4)
    batches = [missing[i : i + batch_size] for i in range(0, len(missing), batch_size)]

    def run_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        texts = [(c.get("embedding_text") or c.get("content_text") or "")[:12000] for c in batch]
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                embs = embed_api_batch(texts, env)
                return [
                    {"chunk_id": chunk["chunk_id"], "vector": [float(x) for x in vec]}
                    for chunk, vec in zip(batch, embs)
                ]
            except Exception as exc:
                last_err = exc
                time.sleep(backoff * (attempt + 1))
        raise RuntimeError(f"Embedding batch failed after retries: {last_err}")

    created_total = 0
    if batches:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [pool.submit(run_batch, b) for b in batches]
            with cache_path.open("a", encoding="utf-8") as f:
                for fut in as_completed(futures):
                    rows = fut.result()
                    for row in rows:
                        cache[row["chunk_id"]] = row["vector"]
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        created_total += 1
                    f.flush()
    vectors = {chunk["chunk_id"]: cache[chunk["chunk_id"]] for chunk in chunks}
    inferred_dim = len(next(iter(vectors.values()))) if vectors else 0
    return vectors, {
        "backend": "api",
        "model": env.get("EMBEDDING_MODEL"),
        "dim": inferred_dim,
        "cached": len(chunks) - len(missing),
        "created": created_total,
        "cache_path": str(cache_path),
        "batch_size": batch_size,
        "concurrency": concurrency,
    }


def load_build_rows(build_root: Path, limit_docs: int | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    docs = read_jsonl(build_root / "normalized_documents.jsonl")
    elements = read_jsonl(build_root / "normalized_elements.jsonl")
    chunks = read_jsonl(build_root / "chunks.jsonl")
    tables = read_jsonl(build_root / "normalized_tables.jsonl")
    cells = read_jsonl(build_root / "table_cells.jsonl")
    if limit_docs:
        keep_doc_ids = {d["doc_id"] for d in docs[:limit_docs]}
        docs = [d for d in docs if d["doc_id"] in keep_doc_ids]
        elements = [e for e in elements if e.get("doc_id") in keep_doc_ids]
        chunks = [c for c in chunks if c.get("doc_id") in keep_doc_ids]
        tables = [t for t in tables if t.get("doc_id") in keep_doc_ids]
        cells = [c for c in cells if c.get("doc_id") in keep_doc_ids]
    return docs, elements, chunks, tables, cells


def build_duckdb(build_root: Path, out_path: Path, limit_docs: int | None = None) -> dict[str, Any]:
    ensure_dir(out_path.parent)
    if out_path.exists():
        out_path.unlink()
    docs, elements, chunks, tables, cells = load_build_rows(build_root, limit_docs)
    con = duckdb.connect(str(out_path))
    try:
        con.execute("CREATE TABLE documents (doc_id VARCHAR, source_profile VARCHAR, source_path VARCHAR, record_json JSON)")
        con.execute("CREATE TABLE elements (element_id VARCHAR, doc_id VARCHAR, source_profile VARCHAR, element_type VARCHAR, quality_status VARCHAR, table_id VARCHAR, record_json JSON)")
        con.execute("CREATE TABLE chunks (chunk_id VARCHAR, doc_id VARCHAR, source_profile VARCHAR, chunk_type VARCHAR, table_id VARCHAR, quality_status VARCHAR, token_count INTEGER, record_json JSON)")
        con.execute("CREATE TABLE tables (table_id VARCHAR, doc_id VARCHAR, source_profile VARCHAR, num_rows INTEGER, num_cols INTEGER, page_start INTEGER, page_end INTEGER, record_json JSON)")
        con.execute("CREATE TABLE table_cells (cell_id VARCHAR, doc_id VARCHAR, source_profile VARCHAR, table_id VARCHAR, row_start INTEGER, col_start INTEGER, text VARCHAR, is_column_header BOOLEAN, is_row_header BOOLEAN, record_json JSON)")
        con.execute("CREATE TABLE table_facts (fact_id VARCHAR, doc_id VARCHAR, source_profile VARCHAR, table_id VARCHAR, row_idx INTEGER, col_idx INTEGER, row_headers JSON, col_headers JSON, value_text VARCHAR, value_number DOUBLE, unit VARCHAR, source_cell_id VARCHAR, record_json JSON)")
        con.execute("CREATE TABLE numeric_values (fact_id VARCHAR, doc_id VARCHAR, table_id VARCHAR, value_number DOUBLE, unit VARCHAR, value_text VARCHAR)")

        for d in docs:
            con.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", [d.get("doc_id"), d.get("source_profile"), d.get("source_path"), json.dumps(d, ensure_ascii=False)])
        for e in elements:
            con.execute("INSERT INTO elements VALUES (?, ?, ?, ?, ?, ?, ?)", [e.get("element_id"), e.get("doc_id"), e.get("source_profile"), e.get("element_type"), e.get("quality_status"), e.get("table_id"), json.dumps(e, ensure_ascii=False)])
        for c in chunks:
            table_id = (c.get("modality_ref") or {}).get("table_id")
            con.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [c.get("chunk_id"), c.get("doc_id"), c.get("source_profile"), c.get("chunk_type"), table_id, c.get("quality_status"), c.get("token_count"), json.dumps(c, ensure_ascii=False)])
        for t in tables:
            con.execute("INSERT INTO tables VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [t.get("table_id"), t.get("doc_id"), t.get("source_profile"), t.get("num_rows"), t.get("num_cols"), t.get("page_start"), t.get("page_end"), json.dumps(t, ensure_ascii=False)])
        by_table_row: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        header_cache: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for cell in cells:
            con.execute("INSERT INTO table_cells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                cell.get("cell_id"), cell.get("doc_id"), cell.get("source_profile"), cell.get("table_id"),
                cell.get("row_start"), cell.get("col_start"), cell.get("text"), bool(cell.get("is_column_header")),
                bool(cell.get("is_row_header")), json.dumps(cell, ensure_ascii=False),
            ])
            if isinstance(cell.get("row_start"), int):
                by_table_row[(cell["table_id"], cell["row_start"])].append(cell)
            if cell.get("is_column_header"):
                header_cache[cell["table_id"]].append(cell)

        fact_count = 0
        numeric_count = 0
        for cell in cells:
            text = cell.get("text") or ""
            if not text or cell.get("is_column_header"):
                continue
            row_idx = cell.get("row_start")
            col_idx = cell.get("col_start")
            if not isinstance(row_idx, int) or not isinstance(col_idx, int):
                continue
            row_cells = by_table_row.get((cell["table_id"], row_idx), [])
            row_headers = [c.get("text") for c in row_cells if c.get("is_row_header") and c.get("text")]
            if not row_headers:
                row_headers = [c.get("text") for c in row_cells[:1] if c.get("text") and c.get("col_start") != col_idx]
            col_headers = [c.get("text") for c in header_cache.get(cell["table_id"], []) if c.get("col_start") == col_idx and c.get("text")]
            number, unit = parse_numeric(text)
            fact = {
                "fact_id": f"fact_{cell['cell_id']}",
                "doc_id": cell.get("doc_id"),
                "source_profile": cell.get("source_profile"),
                "table_id": cell.get("table_id"),
                "row_idx": row_idx,
                "col_idx": col_idx,
                "row_headers": row_headers,
                "col_headers": col_headers,
                "value_text": text,
                "value_number": number,
                "unit": unit,
                "source_cell_id": cell.get("cell_id"),
            }
            con.execute("INSERT INTO table_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                fact["fact_id"], fact["doc_id"], fact["source_profile"], fact["table_id"], row_idx, col_idx,
                json.dumps(row_headers, ensure_ascii=False), json.dumps(col_headers, ensure_ascii=False),
                text, number, unit, cell.get("cell_id"), json.dumps(fact, ensure_ascii=False),
            ])
            fact_count += 1
            if number is not None:
                con.execute("INSERT INTO numeric_values VALUES (?, ?, ?, ?, ?, ?)", [fact["fact_id"], fact["doc_id"], fact["table_id"], number, unit, text])
                numeric_count += 1

        for sql in [
            "CREATE INDEX idx_chunks_doc ON chunks(doc_id)",
            "CREATE INDEX idx_chunks_type ON chunks(chunk_type)",
            "CREATE INDEX idx_cells_table ON table_cells(table_id)",
            "CREATE INDEX idx_facts_table ON table_facts(table_id)",
            "CREATE INDEX idx_numeric_value ON numeric_values(value_number)",
        ]:
            con.execute(sql)
    finally:
        con.close()
    return {
        "path": str(out_path),
        "documents": len(docs),
        "elements": len(elements),
        "chunks": len(chunks),
        "tables": len(tables),
        "table_cells": len(cells),
        "table_facts": fact_count,
        "numeric_values": numeric_count,
    }


def create_qdrant(
    build_root: Path,
    out_dir: Path,
    collection: str,
    embedding_backend: str,
    env: dict[str, str],
    limit_chunks: int | None = None,
    recreate: bool = True,
) -> dict[str, Any]:
    ensure_dir(out_dir)
    chunks = [c for c in read_jsonl(build_root / "chunks.jsonl") if c.get("quality_status") == "ready"]
    if limit_chunks:
        chunks = chunks[:limit_chunks]

    dense_cache = build_root / "indexes" / "embedding_cache" / f"{embedding_backend}_embeddings.jsonl"
    dim_arg = int(env.get("EMBEDDING_DIMENSIONS", "0") or 0) or None
    dense, dense_stats = build_dense_vectors(chunks, embedding_backend, env, dim_arg, dense_cache)
    dense_dim = len(next(iter(dense.values()))) if dense else (dim_arg or 1024)

    vocab, sparse, sparse_stats = build_sparse_vectors(chunks)
    vocab_path = build_root / "indexes" / "bm25s" / "jieba_sparse_vocab.json"
    ensure_dir(vocab_path.parent)
    write_json(vocab_path, {"vocab": vocab, "stats": sparse_stats})

    client = QdrantClient(path=str(out_dir))
    if recreate:
        try:
            client.delete_collection(collection_name=collection)
        except Exception:
            pass
        client.create_collection(
            collection_name=collection,
            vectors_config={"dense": models.VectorParams(size=dense_dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse": models.SparseVectorParams(index=models.SparseIndexParams(on_disk=False))},
        )

    batch_size = 128
    points: list[models.PointStruct] = []
    for chunk in chunks:
        cid = chunk["chunk_id"]
        ref = chunk.get("modality_ref") or {}
        payload = {
            "chunk_id": cid,
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
        points.append(models.PointStruct(
            id=stable_point_id(cid),
            vector={"dense": dense[cid], "sparse": sparse[cid]},
            payload=payload,
        ))
        if len(points) >= batch_size:
            client.upsert(collection_name=collection, points=points)
            points = []
    if points:
        client.upsert(collection_name=collection, points=points)
    info = client.get_collection(collection_name=collection)
    client.close()
    return {
        "path": str(out_dir),
        "collection": collection,
        "points_attempted": len(chunks),
        "dense": dense_stats,
        "sparse": sparse_stats,
        "qdrant_points_count": getattr(info, "points_count", None),
        "qdrant_vectors_count": getattr(info, "vectors_count", None),
        "vocab_path": str(vocab_path),
    }


def run_index_build(args: argparse.Namespace) -> dict[str, Any]:
    build_root = args.build_root
    env = load_dotenv(args.env_path)
    out_root = build_root / "indexes"
    ensure_dir(out_root)
    duck_stats = build_duckdb(build_root, out_root / "duckdb" / "rag_tables.duckdb", limit_docs=args.limit_docs)
    qdrant_stats = create_qdrant(
        build_root,
        out_root / "qdrant_local",
        args.collection,
        args.embedding_backend,
        env,
        limit_chunks=args.limit_chunks,
        recreate=not args.no_recreate,
    )
    report = {"schema_version": "docling0816_index_build.v1", "build_root": str(build_root), "duckdb": duck_stats, "qdrant": qdrant_stats}
    write_json(out_root / "index_build_report.json", report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build DuckDB TableFact/Cell and Qdrant dense+sparse indexes from P0 chunks.")
    p.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    p.add_argument("--env-path", type=Path, default=Path(r"D:\金融科技大赛\Code\.env"))
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--embedding-backend", choices=["api", "hash"], default="api")
    p.add_argument("--limit-docs", type=int, default=None)
    p.add_argument("--limit-chunks", type=int, default=None)
    p.add_argument("--no-recreate", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_index_build(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

