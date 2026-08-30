"""把 Word、PDF 与 Excel 统一分块汇总为不可变语料快照。"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trusted_rag.domain.common import LineageMetadata
from trusted_rag.domain.enums import SourceFormat, SourceKind, SourceProfile
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import ChunkRecord, SourceDocument
from trusted_rag.infrastructure.artifacts import sha256_file, write_json_atomic
from trusted_rag.ingestion.chunking.parent_child_chunker import build_parent_child_chunks
from trusted_rag.ingestion.normalization.docling_standardizer import standardize_docling_json

_DOCUMENT_PROFILES = {
    "converted-docx": ("v0.03/001", SourceProfile.CONVERTED_DOCX),
    "native-docx": ("v0.04/001", SourceProfile.NATIVE_DOCX),
    "pdf": ("v0.06/005", SourceProfile.PDF),
}


def build_unified_corpus(
    *,
    docling_runs_root: Path,
    excel_run_root: Path,
    output_root: Path,
    knowledge_base_id: str,
    run_id: str,
) -> dict[str, Any]:
    """构建只含统一 v1 契约的索引输入快照。

    :param docling_runs_root: 三类 Docling 运行目录的共同根目录。
    :param excel_run_root: 已通过全量质量门禁的 Excel 运行目录。
    :param output_root: 必须尚不存在的语料快照目录。
    :param knowledge_base_id: 统一知识库标识。
    :param run_id: 本次语料构建运行标识。
    :return: 文档、证据、父块和检索块数量及输入摘要。
    :raises FileExistsError: 输出目录已经存在时抛出。
    :raises ValueError: 输入质量未通过、记录重复或契约不合法时抛出。
    """
    if output_root.exists():
        raise FileExistsError(f"语料快照目录已存在：{output_root}")
    _validate_excel_run(excel_run_root)
    output_root.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC)
    writers = _CorpusWriters(output_root)
    profile_counts: Counter[str] = Counter()
    seen_chunk_ids: set[str] = set()
    seen_source_ids: set[str] = set()
    try:
        for profile_name, (suffix, profile) in _DOCUMENT_PROFILES.items():
            run_root = docling_runs_root / profile_name / suffix
            manifest = _read_jsonl(run_root / "manifest.jsonl")
            for item in manifest:
                if item.get("status") != "success":
                    raise ValueError(f"Docling 清单存在非成功文件：{profile_name}/{item.get('doc_id')}")
                json_path = _docling_json_path(run_root, item)
                source = _source_from_docling_manifest(
                    item,
                    profile=profile,
                    knowledge_base_id=knowledge_base_id,
                    run_id=run_id,
                    created_at=started_at,
                )
                writers.write_alias(
                    {
                        "source_id": source.source_id,
                        "original_file_name": source.original_file_name,
                        "relative_path": Path(str(item["source_path"])).as_posix(),
                        "source_profile": profile.value,
                        "is_canonical": source.source_id not in seen_source_ids,
                    }
                )
                if source.source_id in seen_source_ids:
                    profile_counts[f"documents_{profile_name}_deduplicated"] += 1
                    continue
                seen_source_ids.add(source.source_id)
                normalized = standardize_docling_json(
                    json_path,
                    source=source,
                    source_profile=profile,
                    parsing_run_id=run_id,
                    parser_version="2.120.1",
                    artifact_base_uri=f"parsed/{profile_name}/{item['doc_id']}",
                    created_at=started_at,
                )
                chunked = build_parent_child_chunks(normalized)
                writers.write_document(normalized.document.model_dump(mode="json"))
                writers.write_evidence(item.model_dump(mode="json") for item in normalized.evidence)
                writers.write_parents(item.model_dump(mode="json") for item in chunked.parents)
                for chunk in chunked.chunks:
                    _write_unique_chunk(writers, chunk, seen_chunk_ids)
                profile_counts[f"documents_{profile_name}"] += 1
                profile_counts[f"chunks_{profile_name}"] += len(chunked.chunks)

        for manifest_record in _read_jsonl(excel_run_root / "manifest.jsonl"):
            if manifest_record.get("status") != "success":
                raise ValueError(f"Excel 清单存在非成功文件：{manifest_record.get('file_id')}")
            if manifest_record.get("deduplicated"):
                canonical_root = excel_run_root / "normalized" / str(
                    manifest_record["deduplicated_from_file_id"]
                )
                canonical_document = _read_json(canonical_root / "document.json")
                writers.write_alias(
                    {
                        "source_id": canonical_document["source_id"],
                        "original_file_name": manifest_record["original_file_name"],
                        "relative_path": manifest_record["original_file_name"],
                        "source_profile": SourceProfile.EXCEL.value,
                        "is_canonical": False,
                    }
                )
                profile_counts["documents_excel_deduplicated"] += 1
                continue
            artifact_root = excel_run_root / "normalized" / str(manifest_record["file_id"])
            excel_document = _read_json(artifact_root / "document.json")
            excel_source_id = str(excel_document["source_id"])
            is_canonical = excel_source_id not in seen_source_ids
            writers.write_alias(
                {
                    "source_id": excel_source_id,
                    "original_file_name": manifest_record["original_file_name"],
                    "relative_path": manifest_record["original_file_name"],
                    "source_profile": SourceProfile.EXCEL.value,
                    "is_canonical": is_canonical,
                }
            )
            if not is_canonical:
                profile_counts["documents_excel_deduplicated"] += 1
                continue
            seen_source_ids.add(excel_source_id)
            writers.write_document(excel_document)
            writers.write_evidence(_read_jsonl(artifact_root / "evidence.jsonl"))
            chunks = [ChunkRecord.model_validate(item) for item in _read_jsonl(artifact_root / "chunks.jsonl")]
            for chunk in chunks:
                _write_unique_chunk(writers, chunk, seen_chunk_ids)
            profile_counts["documents_excel"] += 1
            profile_counts["chunks_excel"] += len(chunks)
    finally:
        writers.close()

    summary = {
        "schema_version": "unified_corpus.v1",
        "run_id": run_id,
        "knowledge_base_id": knowledge_base_id,
        "created_at": started_at.isoformat(),
        "quality_passed": True,
        "counts": writers.counts,
        "profiles": dict(sorted(profile_counts.items())),
        "inputs": {
            "excel_manifest_sha256": sha256_file(excel_run_root / "manifest.jsonl"),
            "docling_manifests": {
                name: sha256_file(docling_runs_root / name / suffix / "manifest.jsonl")
                for name, (suffix, _) in _DOCUMENT_PROFILES.items()
            },
        },
        "artifacts": {
            name: {"sha256": sha256_file(output_root / name), "records": count}
            for name, count in writers.counts.items()
        },
    }
    write_json_atomic(output_root / "run.json", summary)
    return summary


class _CorpusWriters:
    """集中管理语料 JSONL 文件句柄和记录计数。"""

    def __init__(self, output_root: Path) -> None:
        self.streams = {
            name: (output_root / name).open("w", encoding="utf-8", newline="\n")
            for name in (
                "source_aliases.jsonl",
                "documents.jsonl",
                "evidence.jsonl",
                "parents.jsonl",
                "chunks.jsonl",
            )
        }
        self.counts: dict[str, int] = {name: 0 for name in self.streams}

    def write_document(self, payload: dict[str, Any]) -> None:
        """写入一份文档记录。"""
        self._write("documents.jsonl", payload)

    def write_alias(self, payload: dict[str, Any]) -> None:
        """写入一个原文件名到规范来源的映射。"""
        self._write("source_aliases.jsonl", payload)

    def write_evidence(self, payloads: Any) -> None:
        """写入一组证据记录。"""
        for payload in payloads:
            self._write("evidence.jsonl", payload)

    def write_parents(self, payloads: Any) -> None:
        """写入一组父分块记录。"""
        for payload in payloads:
            self._write("parents.jsonl", payload)

    def write_chunk(self, payload: dict[str, Any]) -> None:
        """写入一个可检索分块。"""
        self._write("chunks.jsonl", payload)

    def close(self) -> None:
        """关闭全部输出流。"""
        for stream in self.streams.values():
            stream.close()

    def _write(self, name: str, payload: dict[str, Any]) -> None:
        self.streams[name].write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self.counts[name] += 1


def _write_unique_chunk(
    writers: _CorpusWriters,
    chunk: ChunkRecord,
    seen_chunk_ids: set[str],
) -> None:
    if chunk.chunk_id in seen_chunk_ids:
        raise ValueError(f"统一语料出现重复 chunk_id：{chunk.chunk_id}")
    seen_chunk_ids.add(chunk.chunk_id)
    writers.write_chunk(chunk.model_dump(mode="json"))


def _source_from_docling_manifest(
    item: dict[str, Any],
    *,
    profile: SourceProfile,
    knowledge_base_id: str,
    run_id: str,
    created_at: datetime,
) -> SourceDocument:
    source_path = Path(str(item["source_path"]))
    original_hash = str(item["source_sha256"]).lower()
    converted = profile is SourceProfile.CONVERTED_DOCX
    normalized_hash = str(item.get("normalized_source_sha256") or original_hash).lower()
    source_id = stable_id("source", knowledge_base_id, normalized_hash)
    converted_from = stable_id("source", knowledge_base_id, original_hash) if converted else None
    normalized_path = Path(str(item.get("normalized_source_path") or item["source_path"]))
    source_format = SourceFormat.DOCX if converted else SourceFormat(str(item["source_format"]).lower())
    return SourceDocument(
        source_id=source_id,
        knowledge_base_id=knowledge_base_id,
        original_file_name=source_path.name,
        normalized_file_name=normalized_path.name,
        source_format=source_format,
        source_kind=SourceKind.CONVERTED if converted else SourceKind.ORIGINAL,
        source_sha256=normalized_hash,
        file_size_bytes=int(item["size_bytes"]),
        relative_path=normalized_path.as_posix() if converted else source_path.as_posix(),
        converted_from_source_id=converted_from,
        lineage=LineageMetadata(
            run_id=run_id,
            producer="unified_corpus_builder",
            producer_version="0.01",
            input_ids=[converted_from] if converted_from else [],
            created_at=created_at,
        ),
    )


def _docling_json_path(run_root: Path, item: dict[str, Any]) -> Path:
    candidates = [
        Path(str(artifact["path"]))
        for artifact in item["artifacts"]
        if str(artifact["path"]).endswith(".json")
    ]
    if len(candidates) != 1:
        raise ValueError(f"Docling 文件必须且只能有一个 JSON：{item.get('doc_id')}")
    path = candidates[0]
    if not path.is_absolute():
        workspace_root = run_root.parents[7]
        path = workspace_root / path
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.resolve()


def _validate_excel_run(excel_run_root: Path) -> None:
    summary = _read_json(excel_run_root / "quality" / "summary.json")
    if summary.get("status_counts", {}).get("failed", 0) or not summary.get("all_sources_unchanged"):
        raise ValueError("Excel 运行尚未通过全量质量门禁。")


def _read_json(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
