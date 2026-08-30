"""验证正式索引快照的分批进度、断点续建和 Alias 门禁。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from qdrant_client import QdrantClient

from trusted_rag.domain.common import LineageMetadata, SourceLocation
from trusted_rag.domain.enums import ChunkContentType
from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import ChunkRecord
from trusted_rag.domain.ports import DenseVector, SnapshotReference
from trusted_rag.indexing.bm25 import JiebaBm25Indexer
from trusted_rag.indexing.snapshot_builder import build_production_snapshot


class FakeEmbedder:
    """生成确定性向量并可在指定调用处模拟中断。"""

    def __init__(self, fail_on_call: int | None = None) -> None:
        """配置可选的失败调用序号。

        :param fail_on_call: 从 1 开始的模拟失败调用序号；空值表示始终成功。
        :return: 无。
        """
        self.fail_on_call = fail_on_call
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[DenseVector]:
        """按文本生成 1024 维测试向量。

        :param texts: 待编码文本。
        :return: 与输入顺序一致的确定性向量。
        :raises RuntimeError: 调用序号命中 ``fail_on_call`` 时模拟中断。
        """
        self.calls.append(list(texts))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("模拟 Embedding 中断")
        result: list[DenseVector] = []
        for index, _text in enumerate(texts, start=1):
            values = [0.0] * 1024
            values[index - 1] = 1.0
            result.append(DenseVector(values=values, request_id=f"fake-{len(self.calls)}-{index}"))
        return result


def test_production_snapshot_resumes_and_activates_only_after_completion(tmp_path: Path) -> None:
    """中断后应从已写入数量继续，并在全量通过后才切换 Alias。"""
    corpus_root = _corpus(tmp_path)
    facts_path = _facts(tmp_path)
    output_root = tmp_path / "snapshot"
    client = QdrantClient(":memory:")
    snapshot = SnapshotReference(
        knowledge_base_id="kb-test",
        snapshot_id="snapshot-test",
        qdrant_collection="documents-test-v1",
        duckdb_uri="facts.duckdb",
    )
    first_embedder = FakeEmbedder(fail_on_call=2)

    with pytest.raises(RuntimeError, match="模拟 Embedding 中断"):
        build_production_snapshot(
            corpus_root=corpus_root,
            excel_facts_parquet=facts_path,
            output_root=output_root,
            snapshot=snapshot,
            qdrant_client=client,
            embedder=first_embedder,
            bm25=JiebaBm25Indexer(user_terms=["监管要求"]),
            vector_batch_size=2,
            alias="documents-current",
        )

    progress = json.loads((output_root / "build_progress.json").read_text(encoding="utf-8"))
    assert progress["completed_chunk_count"] == 2
    assert not client.get_aliases().aliases

    resume_embedder = FakeEmbedder()
    manifest = build_production_snapshot(
        corpus_root=corpus_root,
        excel_facts_parquet=facts_path,
        output_root=output_root,
        snapshot=snapshot,
        qdrant_client=client,
        embedder=resume_embedder,
        bm25=JiebaBm25Indexer(user_terms=["监管要求"]),
        vector_batch_size=2,
        alias="documents-current",
        resume=True,
    )

    assert resume_embedder.calls[0] == ["监管要求3", "监管要求4"]
    assert manifest["qdrant_point_count"] == 4
    assert manifest["alias_switched"] is True
    assert manifest["smoke_passed"] is True
    assert json.loads((output_root / "build_progress.json").read_text(encoding="utf-8"))["status"] == "completed"
    aliases = {item.alias_name: item.collection_name for item in client.get_aliases().aliases}
    assert aliases["documents-current"] == "documents-test-v1"


def _corpus(root: Path) -> Path:
    corpus_root = root / "corpus"
    corpus_root.mkdir()
    chunks = [_chunk(index) for index in range(1, 5)]
    chunks_path = corpus_root / "chunks.jsonl"
    chunks_path.write_text(
        "".join(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n" for item in chunks),
        encoding="utf-8",
    )
    aliases = [
        {
            "source_id": chunk.source_id,
            "original_file_name": f"{index}.pdf",
            "source_profile": "pdf",
            "is_canonical": True,
        }
        for index, chunk in enumerate(chunks, start=1)
    ]
    (corpus_root / "source_aliases.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in aliases),
        encoding="utf-8",
    )
    (corpus_root / "run.json").write_text(
        json.dumps({"run_id": "corpus-test", "quality_passed": True}),
        encoding="utf-8",
    )
    return corpus_root


def _chunk(index: int) -> ChunkRecord:
    text = f"监管要求{index}"
    source_id = stable_id("source", index)
    document_id = stable_id("document", source_id)
    element_id = stable_id("element", document_id, index)
    evidence_id = stable_id("evidence", element_id)
    return ChunkRecord(
        chunk_id=stable_id("chunk", document_id, index),
        content_sha256=canonical_sha256(text),
        document_id=document_id,
        source_id=source_id,
        chunk_index=0,
        content_type=ChunkContentType.TEXT,
        display_text=text,
        embedding_text=text,
        bm25_text=text,
        token_count=4,
        source_element_ids=[element_id],
        evidence_ids=[evidence_id],
        locations=[SourceLocation(section_path=["测试"])],
        lineage=LineageMetadata(
            run_id="corpus-test",
            producer="test",
            producer_version="1",
            created_at=datetime(2026, 8, 30, tzinfo=UTC),
        ),
    )


def _facts(root: Path) -> Path:
    path = root / "facts.parquet"
    table = pa.table(
        {
            "fact_id": ["fact-1"],
            "source_id": ["source-1"],
            "sheet_name": ["Sheet1"],
            "cell_range": ["A1"],
            "metric_name": ["监管指标"],
            "period_end": ["2026-01-01"],
        }
    )
    pq.write_table(table, path)
    return path
