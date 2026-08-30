"""验证来源文件只读登记、格式识别与幂等输入清单。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trusted_rag.domain.enums import SourceFormat
from trusted_rag.infrastructure.artifacts import canonical_digest, sha256_file
from trusted_rag.ingestion.source_registry import (
    SourceRegistrar,
    build_input_manifest,
    detect_source_format,
    discover_source_files,
)


def test_register_all_supported_formats_without_changing_sources(
    supported_source_root: Path,
) -> None:
    """五种格式登记后文件摘要保持不变。"""
    paths = discover_source_files(supported_source_root)
    before = {path.name: sha256_file(path) for path in paths}
    registrar = SourceRegistrar(supported_source_root)
    created_at = datetime(2026, 8, 30, tzinfo=UTC)

    sources = [
        registrar.register(
            path,
            knowledge_base_id="kb_competition",
            run_id="source-registration-v0.01-test",
            created_at=created_at,
        )
        for path in paths
    ]

    assert len(sources) == 5
    assert {source.source_format for source in sources} == set(SourceFormat)
    assert all(source.original_file_name in before for source in sources)
    assert all(source.normalized_file_name.startswith(source.source_id.removeprefix("source_")) for source in sources)
    assert all(registrar.verify_unchanged(source) for source in sources)
    assert {path.name: sha256_file(path) for path in paths} == before


def test_content_identity_is_independent_of_file_name(supported_source_root: Path) -> None:
    """同一知识库内同内容换名后仍获得相同来源标识。"""
    original = supported_source_root / "横版附件.pdf"
    duplicate = supported_source_root / "另一个名称.pdf"
    duplicate.write_bytes(original.read_bytes())
    registrar = SourceRegistrar(supported_source_root)

    left = registrar.register(original, knowledge_base_id="kb_a", run_id="run_a")
    right = registrar.register(duplicate, knowledge_base_id="kb_a", run_id="run_b")

    assert left.source_id == right.source_id
    assert left.original_file_name != right.original_file_name


def test_manifest_is_order_independent_and_config_sensitive(supported_source_root: Path) -> None:
    """输入顺序不影响幂等键，有效配置变化会改变幂等键。"""
    registrar = SourceRegistrar(supported_source_root)
    sources = [
        registrar.register(path, knowledge_base_id="kb_a", run_id="run_a")
        for path in discover_source_files(supported_source_root)[:2]
    ]
    config_a = canonical_digest({"parser": "docling", "version": "0.07"})
    config_b = canonical_digest({"parser": "docling", "version": "0.08"})

    first = build_input_manifest(
        sources,
        knowledge_base_id="kb_a",
        effective_config_sha256=config_a,
    )
    reordered = build_input_manifest(
        list(reversed(sources)),
        knowledge_base_id="kb_a",
        effective_config_sha256=config_a,
    )
    changed = build_input_manifest(
        sources,
        knowledge_base_id="kb_a",
        effective_config_sha256=config_b,
    )

    assert first.idempotency_key == reordered.idempotency_key
    assert first.manifest_id == reordered.manifest_id
    assert first.idempotency_key != changed.idempotency_key


def test_reject_invalid_signature(tmp_path: Path) -> None:
    """扩展名正确但文件签名错误时拒绝登记。"""
    invalid = tmp_path / "伪造.pdf"
    invalid.write_text("not pdf", encoding="utf-8")

    with pytest.raises(ValueError, match="PDF 文件签名无效"):
        detect_source_format(invalid)
