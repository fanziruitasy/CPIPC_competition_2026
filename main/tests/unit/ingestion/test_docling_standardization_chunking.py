"""验证 Docling JSON 标准化、父子分块和证据回指。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trusted_rag.domain.common import LineageMetadata
from trusted_rag.domain.enums import SourceFormat, SourceKind, SourceProfile
from trusted_rag.domain.knowledge import SourceDocument
from trusted_rag.ingestion.chunking.parent_child_chunker import build_parent_child_chunks
from trusted_rag.ingestion.normalization.docling_standardizer import standardize_docling_json

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
RUNS = WORKSPACE_ROOT / "Data" / "staging" / "document_preprocessing" / "runs" / "docling"


def _source(source_format: SourceFormat, name: str) -> SourceDocument:
    return SourceDocument(
        source_id="source_0123456789abcdef01234567",
        knowledge_base_id="kb-test",
        original_file_name=name,
        normalized_file_name=name,
        source_format=source_format,
        source_kind=SourceKind.ORIGINAL,
        source_sha256="0" * 64,
        file_size_bytes=1,
        relative_path=name,
        lineage=LineageMetadata(
            run_id="run-test",
            producer="test",
            producer_version="0.01",
            created_at=datetime.now(UTC),
        ),
    )


@pytest.mark.source_characterization
@pytest.mark.parametrize(
    ("path", "profile", "source_format", "minimum_elements"),
    [
        (
            "native-docx/v0.04/001/parsed_documents/405/405.json",
            SourceProfile.NATIVE_DOCX,
            SourceFormat.DOCX,
            300,
        ),
        (
            "pdf/v0.06/005/parsed_documents/370/370.json",
            SourceProfile.PDF,
            SourceFormat.PDF,
            1,
        ),
    ],
)
def test_standardized_chunks_trace_to_elements_and_source(
    path: str,
    profile: SourceProfile,
    source_format: SourceFormat,
    minimum_elements: int,
) -> None:
    """Word 与 PDF 分块必须能回到元素、证据位置和同一来源。"""
    json_path = RUNS / path
    if not json_path.is_file():
        pytest.skip("本地未提供现有 Docling 产物。")
    normalized = standardize_docling_json(
        json_path,
        source=_source(source_format, json_path.with_suffix(f".{source_format.value}").name),
        source_profile=profile,
        parsing_run_id="run-test",
        parser_version="2.120.1",
        artifact_base_uri="parsed/source_0123456789abcdef01234567",
        created_at=datetime.now(UTC),
    )
    chunked = build_parent_child_chunks(normalized)

    assert len(normalized.elements) >= minimum_elements
    assert len(normalized.elements) == len(normalized.evidence)
    assert chunked.chunks and chunked.parents
    element_ids = {item.element_id for item in normalized.elements}
    evidence_ids = {item.evidence_id for item in normalized.evidence}
    assert all(set(chunk.source_element_ids) <= element_ids for chunk in chunked.chunks)
    assert all(set(chunk.evidence_ids) <= evidence_ids for chunk in chunked.chunks)
    assert all(chunk.locations for chunk in chunked.chunks)
