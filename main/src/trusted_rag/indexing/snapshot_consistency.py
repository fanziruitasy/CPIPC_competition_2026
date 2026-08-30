"""构建 DuckDB 快照并校验其与 Qdrant、语料快照的一致性。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import duckdb

from trusted_rag.infrastructure.artifacts import sha256_file


def build_duckdb_snapshot_from_parquet(
    facts_parquet: Path,
    database_path: Path,
) -> dict[str, Any]:
    """从统一事实 Parquet 原子创建去重的只读 DuckDB 快照。

    :param facts_parquet: Excel 全量运行生成的统一事实 Parquet。
    :param database_path: 必须尚不存在的 DuckDB 文件。
    :return: 文件摘要、事实数量和去重数量。
    """
    if database_path.exists():
        raise FileExistsError(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = database_path.with_name(f".{database_path.name}.{uuid.uuid4().hex}.tmp")
    connection = duckdb.connect(str(temporary))
    try:
        connection.execute(
            """
            CREATE TABLE table_facts AS
            SELECT * EXCLUDE (duplicate_order)
            FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY fact_id ORDER BY source_id, sheet_name, cell_range
                ) AS duplicate_order
                FROM read_parquet(?)
            )
            WHERE duplicate_order = 1
            """,
            [str(facts_parquet.resolve())],
        )
        source_row = connection.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(facts_parquet.resolve())],
        ).fetchone()
        fact_row = connection.execute("SELECT count(*) FROM table_facts").fetchone()
        if source_row is None or fact_row is None:
            raise RuntimeError("DuckDB 未返回事实数量。")
        source_count = int(source_row[0])
        fact_count = int(fact_row[0])
        connection.execute("CREATE UNIQUE INDEX table_facts_id_idx ON table_facts(fact_id)")
        connection.execute("CREATE INDEX table_facts_metric_idx ON table_facts(metric_name)")
        connection.execute("CREATE INDEX table_facts_period_idx ON table_facts(period_end)")
        connection.execute("CREATE INDEX table_facts_source_idx ON table_facts(source_id)")
        connection.execute("CHECKPOINT")
        connection.close()
        os.replace(temporary, database_path)
    except Exception:
        connection.close()
        if temporary.exists():
            temporary.unlink()
        raise
    return {
        "duckdb_sha256": sha256_file(database_path),
        "source_fact_count": source_count,
        "fact_count": fact_count,
        "deduplicated_fact_count": source_count - fact_count,
    }


def validate_snapshot_manifest(manifest: dict[str, Any]) -> None:
    """校验 API 加载前必须满足的跨存储一致性字段。

    :param manifest: 索引构建器生成的快照清单。
    :return: 无。
    :raises ValueError: 任一存储未就绪、摘要缺失或快照标识不一致时抛出。
    """
    required = {
        "snapshot_id",
        "knowledge_base_id",
        "corpus_sha256",
        "qdrant_collection",
        "qdrant_point_count",
        "duckdb_uri",
        "duckdb_sha256",
        "duckdb_fact_count",
        "bm25_vocabulary_sha256",
        "embedding_model",
        "embedding_dimensions",
        "smoke_passed",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        raise ValueError(f"快照一致性清单缺少字段：{missing}")
    if not manifest["smoke_passed"]:
        raise ValueError("索引冒烟检索未通过。")
    if int(manifest["qdrant_point_count"]) <= 0 or int(manifest["duckdb_fact_count"]) <= 0:
        raise ValueError("Qdrant 或 DuckDB 快照为空。")
    if manifest["embedding_model"] != "text-embedding-v3" or int(manifest["embedding_dimensions"]) != 1024:
        raise ValueError("快照 Embedding 契约不符合 text-embedding-v3 1024 维。")
