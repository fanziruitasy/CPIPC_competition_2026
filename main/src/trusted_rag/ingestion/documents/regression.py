"""对现有 Word/PDF Docling 产物执行全量可复现质量回归。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trusted_rag.domain.common import LineageMetadata
from trusted_rag.domain.enums import SourceFormat, SourceKind, SourceProfile
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import SourceDocument
from trusted_rag.infrastructure.artifacts import write_json_atomic
from trusted_rag.ingestion.chunking.parent_child_chunker import build_parent_child_chunks
from trusted_rag.ingestion.documents.docling_artifacts import inspect_docling_json
from trusted_rag.ingestion.normalization.docling_standardizer import standardize_docling_json

_PROFILES = {
    "converted-docx": ("v0.03/001", SourceProfile.CONVERTED_DOCX, SourceFormat.DOCX, 32),
    "native-docx": ("v0.04/001", SourceProfile.NATIVE_DOCX, SourceFormat.DOCX, 34),
    "pdf": ("v0.06/005", SourceProfile.PDF, SourceFormat.PDF, 45),
}


def run_document_regression(runs_root: Path, output_root: Path) -> dict[str, Any]:
    """检查 111 份现有解析结果并生成 JSON 与 Markdown 报告。

    :param runs_root: ``runs/docling`` 根目录。
    :param output_root: 本次回归报告输出目录。
    :return: 汇总、分 profile 统计和逐文件失败清单。
    """
    started_at = datetime.now(UTC)
    details: list[dict[str, Any]] = []
    profile_summaries: dict[str, Any] = {}
    for profile_name, (run_suffix, profile, source_format, expected) in _PROFILES.items():
        parsed_root = runs_root / profile_name / run_suffix / "parsed_documents"
        json_paths = sorted(parsed_root.glob("*/*.json"), key=lambda path: path.as_posix().casefold())
        successes = 0
        totals = {"elements": 0, "evidence": 0, "parents": 0, "chunks": 0, "artifacts": 0}
        for json_path in json_paths:
            document_name = json_path.stem
            record: dict[str, Any] = {"profile": profile_name, "document_name": document_name, "status": "failed"}
            try:
                quality = inspect_docling_json(json_path, source_profile=profile)
                source = _synthetic_source(document_name, profile, source_format, started_at)
                normalized = standardize_docling_json(
                    json_path,
                    source=source,
                    source_profile=profile,
                    parsing_run_id=f"regression-{profile_name}",
                    parser_version="2.120.1",
                    artifact_base_uri=f"parsed/{source.source_id}",
                    created_at=started_at,
                )
                chunked = build_parent_child_chunks(normalized)
                required = [json_path.with_suffix(".md"), json_path.with_suffix(".html")]
                missing = [path.name for path in required if not path.is_file()]
                if missing:
                    raise FileNotFoundError(f"缺少导出产物：{missing}")
                successes += 1
                totals["elements"] += len(normalized.elements)
                totals["evidence"] += len(normalized.evidence)
                totals["parents"] += len(chunked.parents)
                totals["chunks"] += len(chunked.chunks)
                totals["artifacts"] += 3
                record.update(
                    status="passed",
                    quality_status=quality.quality_status,
                    elements=len(normalized.elements),
                    chunks=len(chunked.chunks),
                )
            except Exception as exc:
                record["error_type"] = type(exc).__name__
                record["error_message"] = str(exc)
            details.append(record)
        profile_summaries[profile_name] = {
            "expected_documents": expected,
            "discovered_documents": len(json_paths),
            "passed_documents": successes,
            "coverage_rate": successes / expected if expected else 0.0,
            **totals,
        }
    expected_total = sum(value[3] for value in _PROFILES.values())
    passed_total = sum(value["passed_documents"] for value in profile_summaries.values())
    report = {
        "schema_version": "document_regression.v1",
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "expected_documents": expected_total,
        "passed_documents": passed_total,
        "failed_documents": expected_total - passed_total,
        "coverage_rate": passed_total / expected_total,
        "profiles": profile_summaries,
        "failures": [record for record in details if record["status"] == "failed"],
        "documents": details,
    }
    output_root.mkdir(parents=True, exist_ok=False)
    write_json_atomic(output_root / "document_regression_report.json", report)
    (output_root / "document_regression_report.md").write_text(
        _markdown_report(report), encoding="utf-8", newline="\n"
    )
    return report


def _synthetic_source(
    name: str,
    profile: SourceProfile,
    source_format: SourceFormat,
    created_at: datetime,
) -> SourceDocument:
    source_id = stable_id("source", "kb_competition", profile, name)
    original_id = stable_id("source", "kb_competition", "original-doc", name)
    converted = profile is SourceProfile.CONVERTED_DOCX
    return SourceDocument(
        source_id=source_id,
        knowledge_base_id="kb_competition",
        original_file_name=f"{name}.{source_format.value}",
        normalized_file_name=f"{name}.{source_format.value}",
        source_format=source_format,
        source_kind=SourceKind.CONVERTED if converted else SourceKind.ORIGINAL,
        source_sha256="0" * 64,
        file_size_bytes=0,
        relative_path=f"{profile.value}/{name}.{source_format.value}",
        converted_from_source_id=original_id if converted else None,
        lineage=LineageMetadata(
            run_id="document-regression",
            producer="document_regression",
            producer_version="0.01",
            created_at=created_at,
        ),
    )


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Word/PDF 解析与分块全量回归报告",
        "",
        f"- 预期文档：{report['expected_documents']}",
        f"- 通过文档：{report['passed_documents']}",
        f"- 失败文档：{report['failed_documents']}",
        f"- 覆盖率：{report['coverage_rate']:.2%}",
        "",
        "| 来源类型 | 预期 | 发现 | 通过 | 元素 | 证据 | 父块 | 子块 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, value in report["profiles"].items():
        lines.append(
            f"| {name} | {value['expected_documents']} | {value['discovered_documents']} | "
            f"{value['passed_documents']} | {value['elements']} | {value['evidence']} | "
            f"{value['parents']} | {value['chunks']} |"
        )
    lines.extend(["", "## 失败清单", ""])
    if report["failures"]:
        lines.extend(
            f"- {item['profile']}/{item['document_name']}：{item['error_type']} - {item['error_message']}"
            for item in report["failures"]
        )
    else:
        lines.append("无。")
    return "\n".join(lines) + "\n"
