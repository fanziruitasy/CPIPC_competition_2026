"""验证跨存储快照一致性清单门禁。"""

from __future__ import annotations

import pytest

from trusted_rag.indexing.snapshot_consistency import validate_snapshot_manifest


def test_snapshot_manifest_requires_smoke_and_fixed_embedding() -> None:
    """仅完整、非空且冒烟通过的固定模型快照可进入 Alias 切换。"""
    manifest = {
        "snapshot_id": "snapshot_001",
        "knowledge_base_id": "kb",
        "corpus_sha256": "a" * 64,
        "qdrant_collection": "collection_001",
        "qdrant_point_count": 20,
        "duckdb_uri": "facts.duckdb",
        "duckdb_sha256": "b" * 64,
        "duckdb_fact_count": 100,
        "bm25_vocabulary_sha256": "c" * 64,
        "embedding_model": "text-embedding-v3",
        "embedding_dimensions": 1024,
        "smoke_passed": True,
    }

    validate_snapshot_manifest(manifest)
    manifest["smoke_passed"] = False

    with pytest.raises(ValueError, match="冒烟"):
        validate_snapshot_manifest(manifest)
