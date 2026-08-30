"""把统一语料构建为 Dense、BM25、Qdrant 与 DuckDB 一致快照。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
from qdrant_client import QdrantClient

from trusted_rag.domain.enums import RetrievalProfile
from trusted_rag.domain.knowledge import ChunkRecord
from trusted_rag.domain.ports import DenseEmbedder, IndexRecord, SearchRequest, SnapshotReference
from trusted_rag.domain.query import QueryFilters
from trusted_rag.indexing.bm25 import JiebaBm25Indexer
from trusted_rag.indexing.qdrant_store import QdrantVectorStore
from trusted_rag.indexing.snapshot_consistency import (
    build_duckdb_snapshot_from_parquet,
    validate_snapshot_manifest,
)
from trusted_rag.infrastructure.artifacts import canonical_digest, sha256_file, write_json_atomic


def build_representative_snapshot(
    *,
    corpus_root: Path,
    excel_facts_parquet: Path,
    output_root: Path,
    snapshot: SnapshotReference,
    qdrant_client: QdrantClient,
    embedder: DenseEmbedder,
    bm25: JiebaBm25Indexer,
    per_profile_limit: int,
    alias: str | None = None,
) -> dict[str, Any]:
    """构建并冒烟验证一份四类来源均覆盖的代表性索引快照。

    :param corpus_root: 已通过质量门禁的统一语料目录。
    :param excel_facts_parquet: Excel 全量事实 Parquet。
    :param output_root: 必须尚不存在的索引快照运行目录。
    :param snapshot: Qdrant 与 DuckDB 共用的快照引用。
    :param qdrant_client: Qdrant 服务客户端。
    :param embedder: DashScope Dense 客户端。
    :param bm25: 已配置 Jieba 词典的 BM25 索引器。
    :param per_profile_limit: 每类来源选择的最大分块数。
    :param alias: 冒烟和一致性校验通过后可选切换的稳定 Alias。
    :return: 跨存储一致性清单。
    """
    if per_profile_limit <= 0:
        raise ValueError("per_profile_limit 必须为正数。")
    aliases = _aliases_by_source(corpus_root / "source_aliases.jsonl")
    chunks = [ChunkRecord.model_validate(item) for item in _read_jsonl(corpus_root / "chunks.jsonl")]
    selected = _select_by_profile(chunks, aliases, per_profile_limit)
    return _build_snapshot(
        corpus_root=corpus_root,
        excel_facts_parquet=excel_facts_parquet,
        output_root=output_root,
        snapshot=snapshot,
        qdrant_client=qdrant_client,
        embedder=embedder,
        bm25=bm25,
        selected=selected,
        aliases=aliases,
        vector_batch_size=64,
        alias=alias,
        resume=False,
    )


def build_production_snapshot(
    *,
    corpus_root: Path,
    excel_facts_parquet: Path,
    output_root: Path,
    snapshot: SnapshotReference,
    qdrant_client: QdrantClient,
    embedder: DenseEmbedder,
    bm25: JiebaBm25Indexer,
    vector_batch_size: int = 64,
    alias: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """构建支持断点续建的全量 Dense、BM25、Qdrant 与 DuckDB 快照。

    :param corpus_root: 已通过质量门禁的统一语料目录。
    :param excel_facts_parquet: Excel 全量事实 Parquet。
    :param output_root: 版本化索引快照运行目录。
    :param snapshot: Qdrant 与 DuckDB 共用的快照引用。
    :param qdrant_client: Qdrant 服务客户端。
    :param embedder: text-embedding-v3 Dense 客户端。
    :param bm25: 已配置 Jieba 词典的 BM25 索引器。
    :param vector_batch_size: 每次向 Qdrant 写入的分块数。
    :param alias: 全量校验通过后可选切换的稳定 Alias。
    :param resume: 是否从同一运行目录的进度清单继续。
    :return: 跨存储一致性清单。
    """
    if vector_batch_size <= 0:
        raise ValueError("vector_batch_size 必须为正数。")
    aliases = _aliases_by_source(corpus_root / "source_aliases.jsonl")
    chunks = [ChunkRecord.model_validate(item) for item in _read_jsonl(corpus_root / "chunks.jsonl")]
    return _build_snapshot(
        corpus_root=corpus_root,
        excel_facts_parquet=excel_facts_parquet,
        output_root=output_root,
        snapshot=snapshot,
        qdrant_client=qdrant_client,
        embedder=embedder,
        bm25=bm25,
        selected=chunks,
        aliases=aliases,
        vector_batch_size=vector_batch_size,
        alias=alias,
        resume=resume,
    )


def _build_snapshot(
    *,
    corpus_root: Path,
    excel_facts_parquet: Path,
    output_root: Path,
    snapshot: SnapshotReference,
    qdrant_client: QdrantClient,
    embedder: DenseEmbedder,
    bm25: JiebaBm25Indexer,
    selected: list[ChunkRecord],
    aliases: dict[str, list[dict[str, Any]]],
    vector_batch_size: int,
    alias: str | None,
    resume: bool,
) -> dict[str, Any]:
    """执行代表性或全量快照的公共分批写入与一致性门禁。"""
    if not selected:
        raise ValueError("索引没有选中任何分块。")
    corpus_run = _read_json(corpus_root / "run.json")
    if not corpus_run.get("quality_passed"):
        raise ValueError("统一语料尚未通过质量门禁。")
    corpus_sha256 = sha256_file(corpus_root / "chunks.jsonl")
    selection_sha256 = canonical_digest([chunk.chunk_id for chunk in selected])
    progress_path = output_root / "build_progress.json"
    vocabulary_path = output_root / "bm25_vocabulary.json"
    store = QdrantVectorStore(qdrant_client)

    if resume:
        if not output_root.is_dir() or not progress_path.is_file():
            raise FileNotFoundError("断点续建要求已有运行目录和进度清单。")
        progress = _read_json(progress_path)
        _validate_progress(progress, snapshot, corpus_sha256, selection_sha256, len(selected))
        if not qdrant_client.collection_exists(snapshot.qdrant_collection):
            raise FileNotFoundError(snapshot.qdrant_collection)
        active_bm25 = JiebaBm25Indexer.load(vocabulary_path)
    else:
        if output_root.exists():
            raise FileExistsError(output_root)
        output_root.mkdir(parents=True, exist_ok=False)
        bm25.fit([chunk.bm25_text for chunk in selected])
        bm25.save(vocabulary_path)
        store.create_snapshot(snapshot)
        progress = {
            "schema_version": "index_build_progress.v1",
            "snapshot_id": snapshot.snapshot_id,
            "qdrant_collection": snapshot.qdrant_collection,
            "corpus_sha256": corpus_sha256,
            "selection_sha256": selection_sha256,
            "selected_chunk_count": len(selected),
            "completed_chunk_count": 0,
            "status": "indexing",
        }
        write_json_atomic(progress_path, progress)
        active_bm25 = bm25

    completed = int(progress["completed_chunk_count"])
    for start in range(completed, len(selected), vector_batch_size):
        batch = selected[start : start + vector_batch_size]
        dense_vectors = embedder.embed([chunk.embedding_text for chunk in batch])
        bm25_vectors = active_bm25.encode([chunk.bm25_text for chunk in batch])
        records = [
            IndexRecord(
                chunk=chunk,
                dense_vector=dense,
                bm25_vector=sparse,
                payload=_source_payload(chunk.source_id, aliases),
            )
            for chunk, dense, sparse in zip(batch, dense_vectors, bm25_vectors, strict=True)
        ]
        store.upsert(snapshot, records)
        progress["completed_chunk_count"] = start + len(batch)
        write_json_atomic(progress_path, progress)

    point_count = int(
        qdrant_client.count(collection_name=snapshot.qdrant_collection, exact=True).count
    )
    if point_count != len(selected):
        raise ValueError("Qdrant Point 数量与选中分块数不一致。")
    duckdb_path = output_root / snapshot.duckdb_uri
    duckdb_result = (
        _inspect_duckdb_snapshot(excel_facts_parquet, duckdb_path)
        if duckdb_path.exists()
        else build_duckdb_snapshot_from_parquet(excel_facts_parquet, duckdb_path)
    )
    first_dense = embedder.embed([selected[0].embedding_text])[0]
    first_sparse = active_bm25.encode([selected[0].bm25_text])[0]
    first_record = IndexRecord(
        chunk=selected[0],
        dense_vector=first_dense,
        bm25_vector=first_sparse,
        payload=_source_payload(selected[0].source_id, aliases),
    )
    smoke_passed = _smoke(store, snapshot, first_record)
    manifest = {
        "schema_version": "index_snapshot.v1",
        "snapshot_id": snapshot.snapshot_id,
        "knowledge_base_id": snapshot.knowledge_base_id,
        "corpus_run_id": corpus_run["run_id"],
        "corpus_sha256": corpus_sha256,
        "selected_chunk_count": len(selected),
        "selected_profile_counts": _selected_profile_counts(selected, aliases),
        "qdrant_collection": snapshot.qdrant_collection,
        "qdrant_point_count": point_count,
        "duckdb_uri": snapshot.duckdb_uri,
        "duckdb_sha256": duckdb_result["duckdb_sha256"],
        "duckdb_fact_count": duckdb_result["fact_count"],
        "duckdb_deduplicated_fact_count": duckdb_result["deduplicated_fact_count"],
        "bm25_vocabulary_sha256": sha256_file(vocabulary_path),
        "embedding_model": "text-embedding-v3",
        "embedding_dimensions": 1024,
        "smoke_passed": smoke_passed,
        "alias": alias,
        "alias_switched": False,
    }
    validate_snapshot_manifest(manifest)
    if alias:
        store.activate(snapshot, alias=alias)
        manifest["alias_switched"] = True
    write_json_atomic(output_root / "snapshot.json", manifest)
    progress["completed_chunk_count"] = len(selected)
    progress["status"] = "completed"
    write_json_atomic(progress_path, progress)
    return manifest


def _validate_progress(
    progress: dict[str, Any],
    snapshot: SnapshotReference,
    corpus_sha256: str,
    selection_sha256: str,
    selected_count: int,
) -> None:
    expected = {
        "schema_version": "index_build_progress.v1",
        "snapshot_id": snapshot.snapshot_id,
        "qdrant_collection": snapshot.qdrant_collection,
        "corpus_sha256": corpus_sha256,
        "selection_sha256": selection_sha256,
        "selected_chunk_count": selected_count,
    }
    for field, value in expected.items():
        if progress.get(field) != value:
            raise ValueError(f"断点进度字段 {field} 与当前输入不一致。")
    completed = int(progress.get("completed_chunk_count", -1))
    if not 0 <= completed <= selected_count:
        raise ValueError("断点完成数量不合法。")


def _inspect_duckdb_snapshot(facts_parquet: Path, database_path: Path) -> dict[str, Any]:
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        fact_row = connection.execute("SELECT count(*) FROM table_facts").fetchone()
        source_row = connection.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(facts_parquet.resolve())],
        ).fetchone()
    finally:
        connection.close()
    if fact_row is None or source_row is None:
        raise RuntimeError("DuckDB 断点检查未返回事实数量。")
    fact_count = int(fact_row[0])
    source_count = int(source_row[0])
    return {
        "duckdb_sha256": sha256_file(database_path),
        "source_fact_count": source_count,
        "fact_count": fact_count,
        "deduplicated_fact_count": source_count - fact_count,
    }


def _smoke(
    store: QdrantVectorStore,
    snapshot: SnapshotReference,
    record: IndexRecord,
) -> bool:
    request = SearchRequest(
        query=record.chunk.display_text[:100],
        profile=RetrievalProfile.DENSE_BM25,
        dense_vector=record.dense_vector,
        bm25_vector=record.bm25_vector,
        filters=QueryFilters(source_ids=[record.chunk.source_id]),
        top_k=5,
    )
    hits = store.search(snapshot, request)
    return bool(hits and hits[0].chunk_id == record.chunk.chunk_id)


def _select_by_profile(
    chunks: Sequence[ChunkRecord],
    aliases: dict[str, list[dict[str, Any]]],
    limit: int,
) -> list[ChunkRecord]:
    groups: dict[str, list[ChunkRecord]] = defaultdict(list)
    for chunk in chunks:
        profile = _canonical_alias(chunk.source_id, aliases).get("source_profile", "unknown")
        if len(groups[profile]) < limit:
            groups[profile].append(chunk)
    selected: list[ChunkRecord] = []
    for profile in ("converted_docx", "native_docx", "pdf", "excel"):
        selected.extend(groups.get(profile, []))
    return selected


def _selected_profile_counts(
    chunks: Sequence[ChunkRecord],
    aliases: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for chunk in chunks:
        counts[str(_canonical_alias(chunk.source_id, aliases).get("source_profile", "unknown"))] += 1
    return dict(sorted(counts.items()))


def _source_payload(source_id: str, aliases: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    source_aliases = aliases[source_id]
    canonical = _canonical_alias(source_id, aliases)
    return {
        "original_file_names": [str(item["original_file_name"]) for item in source_aliases],
        "source_format": Path(str(canonical["original_file_name"])).suffix.lower().lstrip("."),
        "source_profile": canonical["source_profile"],
    }


def _canonical_alias(source_id: str, aliases: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    candidates = aliases[source_id]
    return next((item for item in candidates if item.get("is_canonical")), candidates[0])


def _aliases_by_source(path: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in _read_jsonl(path):
        result[str(item["source_id"])].append(item)
    return dict(result)


def _read_json(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
