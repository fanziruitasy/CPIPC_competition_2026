from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .utils import ensure_dir, l2_normalize, tokenize_for_index, write_json, write_jsonl


def build_bm25_index(chunks: list[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    ensure_dir(out_dir)
    postings: dict[str, dict[str, int]] = defaultdict(dict)
    doc_lengths: dict[str, int] = {}
    chunk_meta: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        if chunk.get("quality_status") != "ready":
            continue
        cid = chunk["chunk_id"]
        text = chunk.get("bm25_text") or chunk.get("embedding_text") or chunk.get("content_text") or ""
        counts = Counter(tokenize_for_index(text))
        doc_lengths[cid] = sum(counts.values())
        for term, tf in counts.items():
            postings[term][cid] = tf
        chunk_meta[cid] = {
            "doc_id": chunk.get("doc_id"),
            "chunk_type": chunk.get("chunk_type"),
            "section_path": chunk.get("section_path"),
            "page_start": chunk.get("page_start"),
            "page_end": chunk.get("page_end"),
            "table_id": (chunk.get("modality_ref") or {}).get("table_id"),
        }
    index = {
        "schema_version": "local_bm25_index.v1",
        "chunk_count": len(chunk_meta),
        "avg_doc_length": (sum(doc_lengths.values()) / len(doc_lengths)) if doc_lengths else 0,
        "doc_lengths": doc_lengths,
        "postings": postings,
        "chunk_meta": chunk_meta,
        "tokenizer": "cjk-bigram-plus-alnum.v1",
    }
    write_json(out_dir / "bm25_index.json", index)
    return {"chunk_count": len(chunk_meta), "term_count": len(postings), "path": str(out_dir / "bm25_index.json")}


def hash_embedding(text: str, dim: int = 256) -> list[float]:
    vec = [0.0] * dim
    tokens = tokenize_for_index(text)
    for tok in tokens:
        digest = hashlib.blake2b(tok.encode("utf-8", errors="ignore"), digest_size=8).digest()
        value = int.from_bytes(digest, "little", signed=False)
        idx = value % dim
        sign = 1.0 if (value >> 8) & 1 else -1.0
        vec[idx] += sign
    return l2_normalize(vec)


def build_hash_vector_index(chunks: list[dict[str, Any]], out_dir: Path, dim: int = 256) -> dict[str, Any]:
    ensure_dir(out_dir)
    rows = []
    for chunk in chunks:
        if chunk.get("quality_status") != "ready":
            continue
        text = chunk.get("embedding_text") or chunk.get("content_text") or ""
        rows.append({
            "schema_version": "local_hash_vector.v1",
            "chunk_id": chunk["chunk_id"],
            "doc_id": chunk.get("doc_id"),
            "chunk_type": chunk.get("chunk_type"),
            "dim": dim,
            "embedding_backend": "hash_embedding.v1",
            "vector": hash_embedding(text, dim),
            "payload": {
                "section_path": chunk.get("section_path"),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "table_id": (chunk.get("modality_ref") or {}).get("table_id"),
                "quality_status": chunk.get("quality_status"),
            },
        })
    path = out_dir / "hash_vectors.jsonl"
    write_jsonl(path, rows)
    write_json(out_dir / "vector_manifest.json", {
        "schema_version": "local_vector_manifest.v1",
        "backend": "hash_embedding.v1",
        "dim": dim,
        "chunk_count": len(rows),
        "vectors_path": str(path),
        "note": "Smoke-test local vector store. Replace backend with a real embedding model for production retrieval.",
    })
    return {"chunk_count": len(rows), "dim": dim, "path": str(path)}


def build_table_sqlite(
    documents: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    table_cells: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    db_path: Path,
) -> dict[str, Any]:
    ensure_dir(db_path.parent)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE documents (doc_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)")
        cur.execute("CREATE TABLE elements (element_id TEXT PRIMARY KEY, doc_id TEXT, element_type TEXT, table_id TEXT, record_json TEXT NOT NULL)")
        cur.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, doc_id TEXT, chunk_type TEXT, table_id TEXT, record_json TEXT NOT NULL)")
        cur.execute("CREATE TABLE table_cells (cell_id TEXT PRIMARY KEY, doc_id TEXT, table_id TEXT, row_start INTEGER, col_start INTEGER, text TEXT, record_json TEXT NOT NULL)")
        cur.execute("CREATE TABLE table_facts (fact_id TEXT PRIMARY KEY, doc_id TEXT, table_id TEXT, row_idx INTEGER, col_idx INTEGER, row_headers TEXT, col_headers TEXT, value_text TEXT, record_json TEXT NOT NULL)")
        for doc in documents:
            cur.execute("INSERT INTO documents VALUES (?, ?)", (doc["doc_id"], json.dumps(doc, ensure_ascii=False)))
        for e in elements:
            cur.execute(
                "INSERT INTO elements VALUES (?, ?, ?, ?, ?)",
                (e["element_id"], e.get("doc_id"), e.get("element_type"), e.get("table_id"), json.dumps(e, ensure_ascii=False)),
            )
        for chunk in chunks:
            table_id = (chunk.get("modality_ref") or {}).get("table_id")
            cur.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                (chunk["chunk_id"], chunk.get("doc_id"), chunk.get("chunk_type"), table_id, json.dumps(chunk, ensure_ascii=False)),
            )
        by_table_row: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for cell in table_cells:
            cur.execute(
                "INSERT INTO table_cells VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cell["cell_id"],
                    cell.get("doc_id"),
                    cell.get("table_id"),
                    cell.get("row_start"),
                    cell.get("col_start"),
                    cell.get("text"),
                    json.dumps(cell, ensure_ascii=False),
                ),
            )
            if isinstance(cell.get("row_start"), int):
                by_table_row[(cell["table_id"], cell["row_start"])].append(cell)
        fact_count = 0
        header_cache: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for cell in table_cells:
            if cell.get("is_column_header"):
                header_cache[cell["table_id"]].append(cell)
        for cell in table_cells:
            text = cell.get("text") or ""
            if not text or cell.get("is_column_header"):
                continue
            row_idx = cell.get("row_start")
            col_idx = cell.get("col_start")
            if not isinstance(row_idx, int) or not isinstance(col_idx, int):
                continue
            row_headers = [c.get("text") for c in by_table_row.get((cell["table_id"], row_idx), []) if c.get("is_row_header") and c.get("text")]
            if not row_headers:
                row_headers = [c.get("text") for c in by_table_row.get((cell["table_id"], row_idx), [])[:1] if c.get("text") and c.get("col_start") != col_idx]
            col_headers = [c.get("text") for c in header_cache.get(cell["table_id"], []) if c.get("col_start") == col_idx and c.get("text")]
            fact = {
                "fact_id": f"fact_{cell['cell_id']}",
                "doc_id": cell.get("doc_id"),
                "table_id": cell.get("table_id"),
                "row_idx": row_idx,
                "col_idx": col_idx,
                "row_headers": row_headers,
                "col_headers": col_headers,
                "value_text": text,
                "page_start": cell.get("page_start"),
                "page_end": cell.get("page_end"),
                "source_cell_id": cell["cell_id"],
            }
            cur.execute(
                "INSERT INTO table_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fact["fact_id"], fact["doc_id"], fact["table_id"], row_idx, col_idx,
                    json.dumps(row_headers, ensure_ascii=False), json.dumps(col_headers, ensure_ascii=False),
                    text, json.dumps(fact, ensure_ascii=False),
                ),
            )
            fact_count += 1
        cur.execute("CREATE INDEX idx_cells_table ON table_cells(table_id)")
        cur.execute("CREATE INDEX idx_facts_table ON table_facts(table_id)")
        cur.execute("CREATE INDEX idx_facts_value ON table_facts(value_text)")
        conn.commit()
    finally:
        conn.close()
    return {"path": str(db_path), "table_cell_count": len(table_cells), "table_fact_count": fact_count}
