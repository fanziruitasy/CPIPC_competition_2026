"""构建 Dense、Jieba BM25、Qdrant 与 DuckDB 的代表性一致快照。

输入：版本化索引 YAML、工作区 `.env`、统一语料和 Excel 事实 Parquet。
输出：不可变 Qdrant Collection、DuckDB、BM25 词表、调用审计和快照清单。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from qdrant_client import QdrantClient

MAIN_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = MAIN_ROOT.parent
sys.path.insert(0, str(MAIN_ROOT / "src"))

from trusted_rag.domain.ports import SnapshotReference  # noqa: E402
from trusted_rag.indexing.bm25 import JiebaBm25Indexer  # noqa: E402
from trusted_rag.indexing.dashscope_embedding import DashScopeDenseEmbedder  # noqa: E402
from trusted_rag.indexing.snapshot_builder import build_representative_snapshot  # noqa: E402


def main() -> int:
    """加载配置并构建代表性一致快照。

    :return: 构建和冒烟验证成功返回 0。
    """
    parser = argparse.ArgumentParser(description="构建代表性 Dense/BM25 一致快照")
    parser.add_argument("--config", type=Path, default=Path("configs/indexing/representative_v0.01.yaml"))
    parser.add_argument("--activate", action="store_true")
    arguments = parser.parse_args()
    config_path = arguments.config if arguments.config.is_absolute() else MAIN_ROOT / arguments.config
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
    client = QdrantClient(url=str(config["qdrant"]["url"]), timeout=60)
    manifest = build_representative_snapshot(
        corpus_root=MAIN_ROOT / "data_runtime" / "corpus_runs" / str(config["corpus_run_id"]),
        excel_facts_parquet=MAIN_ROOT
        / "data_runtime"
        / "ingestion_runs"
        / str(config["excel_run_id"])
        / "normalized"
        / "all_facts.parquet",
        output_root=run_root,
        snapshot=snapshot,
        qdrant_client=client,
        embedder=embedder,
        bm25=bm25,
        per_profile_limit=int(config["per_profile_limit"]),
        alias=str(config["qdrant"]["alias"]) if arguments.activate else None,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

