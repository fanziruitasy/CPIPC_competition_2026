"""从版本化配置组装在线问答运行时，供 CLI、API 和评测共同复用。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import duckdb
import yaml
from dotenv import load_dotenv
from qdrant_client import QdrantClient

from trusted_rag.answering.dashscope_generation import DashScopeAnswerGenerator
from trusted_rag.answering.service import TrustedAnswerService
from trusted_rag.application.query_service import TrustedRagQueryService
from trusted_rag.domain.enums import RetrievalProfile
from trusted_rag.domain.ports import SnapshotReference
from trusted_rag.indexing.bm25 import JiebaBm25Indexer
from trusted_rag.indexing.dashscope_embedding import DashScopeDenseEmbedder
from trusted_rag.indexing.qdrant_store import QdrantVectorStore
from trusted_rag.infrastructure.audit import JsonlAuditRepository
from trusted_rag.retrieval.dashscope_planner import DashScopePlannerModel
from trusted_rag.retrieval.evidence_repository import CorpusEvidenceRepository
from trusted_rag.retrieval.query_planner import ControlledQueryPlanner
from trusted_rag.retrieval.reranker import DashScopeReranker
from trusted_rag.retrieval.service import RetrievalService
from trusted_rag.storage.duckdb_fact_store import DuckDbFactStore


def build_query_service(
    main_root: Path,
    config_path: Path,
    *,
    trace_id: str,
    profile: str | None = None,
) -> TrustedRagQueryService:
    """从固定快照和环境变量构造完整在线问答服务。

    :param main_root: `main` 项目根目录。
    :param config_path: 版本化检索配置。
    :param trace_id: 当前请求追踪标识。
    :param profile: 可选检索剖面覆盖。
    :return: 已连接 Qdrant、DuckDB 和模型客户端的问答服务。
    """
    main_root = main_root.resolve()
    load_dotenv(main_root.parent / ".env", override=False)
    config: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    snapshot_config = config["snapshot"]
    qdrant_url = os.getenv("QDRANT_URL", snapshot_config["qdrant_url"])
    snapshot_root = (main_root / snapshot_config["run_root"]).resolve()
    snapshot_manifest = json.loads((snapshot_root / "snapshot.json").read_text(encoding="utf-8"))
    corpus_root = (main_root / snapshot_config["corpus_root"]).resolve()
    selected_profile = RetrievalProfile(profile or config["retrieval"]["default_profile"])
    metrics, entities = _load_query_terms(snapshot_root / snapshot_config["duckdb_uri"])
    planner_model = None
    if config["query_planning"]["model_enabled"]:
        planner_model = DashScopePlannerModel(
            model=os.getenv(config["query_planning"]["model_env"], "qwen3.7-plus"),
            timeout_seconds=int(config["query_planning"]["timeout_seconds"]),
            max_retries=int(config["query_planning"]["max_retries"]),
        )
    planner = ControlledQueryPlanner(
        metrics=metrics,
        entities=entities,
        model=planner_model,
        model_threshold=float(config["query_planning"]["rule_confidence_threshold"]),
        retrieval_profile=selected_profile,
    )
    query_run_root = (main_root / config["audit"]["directory"]).resolve()
    repository = CorpusEvidenceRepository(corpus_root)
    rerank_config = config["rerank"]
    reranker = None
    if rerank_config["enabled"]:
        reranker = DashScopeReranker(
            model=rerank_config["model"],
            timeout_seconds=int(rerank_config["timeout_seconds"]),
            max_retries=int(rerank_config["max_retries"]),
            retry_backoff_seconds=float(rerank_config["retry_backoff_seconds"]),
            instruct=rerank_config["instruct"],
        )
    snapshot = SnapshotReference(
        knowledge_base_id=config["knowledge_base_id"],
        snapshot_id=snapshot_manifest["snapshot_id"],
        qdrant_collection=snapshot_config["collection_alias"],
        duckdb_uri=snapshot_config["duckdb_uri"],
    )
    retrieval = RetrievalService(
        vector_store=QdrantVectorStore(QdrantClient(url=qdrant_url)),
        fact_store=DuckDbFactStore(snapshot_root),
        embedder=DashScopeDenseEmbedder(audit_path=query_run_root / f"{trace_id}.embedding.jsonl"),
        bm25=JiebaBm25Indexer.load(snapshot_root / "bm25_vocabulary.json"),
        evidence_repository=repository,
        reranker=reranker,
        recall_top_k=int(config["retrieval"]["recall_top_k"]),
        rerank_top_k=int(rerank_config["top_k"]),
        rrf_k=int(config["retrieval"]["rrf_k"]),
    )
    generator = DashScopeAnswerGenerator(
        model=os.getenv(config["answering"]["model_env"], "qwen3.7-plus"),
        timeout_seconds=int(config["answering"]["timeout_seconds"]),
        max_retries=int(config["answering"]["max_retries"]),
        max_tokens=int(config["answering"]["max_tokens"]),
    )
    return TrustedRagQueryService(
        planner=planner,
        retrieval=retrieval,
        answering=TrustedAnswerService(generator, repository),
        audit=JsonlAuditRepository(query_run_root),
        snapshot=snapshot,
    )


def _load_query_terms(database_path: Path) -> tuple[list[str], list[str]]:
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        metrics = connection.execute(
            "SELECT DISTINCT metric_name FROM table_facts WHERE metric_name IS NOT NULL ORDER BY metric_name"
        ).fetchall()
        entities = connection.execute(
            "SELECT DISTINCT entity_name FROM table_facts WHERE entity_name IS NOT NULL ORDER BY entity_name"
        ).fetchall()
    finally:
        connection.close()
    return [str(item[0]) for item in metrics], [str(item[0]) for item in entities]
