"""验证端口可由测试替身实现且领域层不依赖具体 SDK。"""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from trusted_rag.domain.enums import RetrievalProfile
from trusted_rag.domain.ports import (
    DenseVector,
    IndexRecord,
    SearchHit,
    SearchRequest,
    SnapshotReference,
    SparseVector,
    VectorStore,
)


class InMemoryVectorStore:
    """只为接口测试提供的内存向量存储替身。"""

    def __init__(self) -> None:
        """初始化空快照和记录列表。"""
        self.snapshots: list[SnapshotReference] = []
        self.records: list[IndexRecord] = []
        self.aliases: dict[str, SnapshotReference] = {}

    def create_snapshot(self, snapshot: SnapshotReference) -> None:
        """记录新建快照。"""
        self.snapshots.append(snapshot)

    def upsert(self, snapshot: SnapshotReference, records: list[IndexRecord]) -> None:
        """记录写入请求。"""
        if snapshot not in self.snapshots:
            raise ValueError("快照不存在。")
        self.records.extend(records)

    def search(self, snapshot: SnapshotReference, request: SearchRequest) -> list[SearchHit]:
        """返回空候选以验证调用接口。"""
        if snapshot not in self.snapshots or request.top_k < 1:
            raise ValueError("检索请求无效。")
        return []

    def activate(self, snapshot: SnapshotReference, *, alias: str) -> None:
        """记录别名当前指向。"""
        if snapshot not in self.snapshots:
            raise ValueError("快照不存在。")
        self.aliases[alias] = snapshot


def test_vector_store_protocol_accepts_test_double() -> None:
    """应用测试可以只实现端口而不安装 Qdrant SDK。"""
    assert isinstance(InMemoryVectorStore(), VectorStore)


def test_search_request_requires_vectors_for_selected_profile() -> None:
    """双路检索请求必须同时包含 Dense 和 BM25 表示。"""
    dense = DenseVector(values=[0.0] * 1024)

    with pytest.raises(ValidationError):
        SearchRequest(
            query="资本管理要求",
            profile=RetrievalProfile.DENSE_BM25,
            dense_vector=dense,
            bm25_vector=None,
        )


def test_sparse_vector_requires_matching_unique_indices() -> None:
    """BM25 稀疏向量索引和值必须一一对应且索引唯一。"""
    with pytest.raises(ValidationError):
        SparseVector(indices=[1, 1], values=[0.5, 0.8])


def test_domain_package_does_not_import_external_sdks() -> None:
    """领域契约与端口不得直接导入解析器或数据库 SDK。"""
    domain_root = Path(__file__).parents[3] / "src" / "trusted_rag" / "domain"
    prohibited_roots = {"docling", "duckdb", "qdrant_client", "dashscope", "openai"}
    imported_roots: set[str] = set()
    for source_path in domain_root.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(prohibited_roots)

