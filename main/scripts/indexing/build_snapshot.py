"""构建可断点续建的正式 Dense、Jieba BM25、Qdrant 与 DuckDB 全量快照。

功能：对统一语料全部 chunk 分批生成 text-embedding-v3 1024 维向量并写入新
Qdrant Collection，同时生成固定 BM25 词表和 DuckDB 事实快照。
输入：``--config`` 版本化 YAML；``--resume`` 从同一运行断点继续；``--activate``
仅在数量、一致性和冒烟检索全部通过后切换稳定 Alias。
输出：索引运行目录中的进度、Embedding 审计、BM25 词表、DuckDB 和快照清单。
返回：成功返回 0；失败保留进度和未激活 Collection，供 ``--resume`` 继续。
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from qdrant_client import QdrantClient

MAIN_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = MAIN_ROOT.parent
SRC_ROOT = MAIN_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trusted_rag.domain.ports import SnapshotReference
from trusted_rag.indexing.bm25 import JiebaBm25Indexer
from trusted_rag.indexing.dashscope_embedding import DashScopeDenseEmbedder
from trusted_rag.indexing.snapshot_builder import build_production_snapshot


def main() -> int:
    """加载正式索引配置并构建或续建全量快照。

    :return: 全量构建、一致性检查和可选 Alias 切换成功返回 0。
    """
    parser = argparse.ArgumentParser(description="构建正式全量 Dense/BM25 一致快照")
    parser.add_argument(
        "--config",
        type=Path,
        default=MAIN_ROOT / "configs/indexing/production_v0.01.yaml",
    )
    parser.add_argument("--resume", action="store_true", help="从配置对应的既有进度继续。")
    parser.add_argument("--activate", action="store_true", help="验收通过后切换稳定 Alias。")
    arguments = parser.parse_args()
    config_path = (
        arguments.config
        if arguments.config.is_absolute()
        else (MAIN_ROOT / arguments.config).resolve()
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    load_dotenv(WORKSPACE_ROOT / ".env", override=False)
    run_root = MAIN_ROOT / str(config["runtime_root"]) / str(config["snapshot_id"])
    embedding = config["embedding"]
    bm25_config = config["bm25"]
    embedder = DashScopeDenseEmbedder(
        model=str(embedding["model"]),
        dimensions=int(embedding["dimensions"]),
        batch_size=int(embedding["batch_size"]),
        timeout_seconds=float(embedding["timeout_seconds"]),
        max_retries=int(embedding["max_retries"]),
        retry_backoff_seconds=float(embedding["retry_backoff_seconds"]),
        audit_path=run_root / "embedding_audit.jsonl",
        price_cny_per_million_tokens=embedding.get("price_cny_per_million_tokens"),
    )
    bm25 = JiebaBm25Indexer.from_lexicon(
        MAIN_ROOT / str(bm25_config["lexicon"]),
        k1=float(bm25_config["k1"]),
        b=float(bm25_config["b"]),
        max_features=int(bm25_config["max_features"]),
    )
    snapshot = SnapshotReference(
        knowledge_base_id=str(config["knowledge_base_id"]),
        snapshot_id=str(config["snapshot_id"]),
        qdrant_collection=str(config["qdrant"]["collection"]),
        duckdb_uri="facts.duckdb",
    )
    client = QdrantClient(
        url=os.getenv("QDRANT_URL", str(config["qdrant"]["url"])),
        timeout=float(config["qdrant"]["timeout_seconds"]),
    )
    manifest = build_production_snapshot(
        corpus_root=MAIN_ROOT / "data_runtime" / "corpus_runs" / str(config["corpus_run_id"]),
        excel_facts_parquet=(
            MAIN_ROOT
            / "data_runtime"
            / "ingestion_runs"
            / str(config["excel_run_id"])
            / "normalized"
            / "all_facts.parquet"
        ),
        output_root=run_root,
        snapshot=snapshot,
        qdrant_client=client,
        embedder=embedder,
        bm25=bm25,
        vector_batch_size=int(config["vector_batch_size"]),
        alias=str(config["qdrant"]["alias"]) if arguments.activate else None,
        resume=arguments.resume,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
