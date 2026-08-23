from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import chunk_modal_elements, chunk_tables, chunk_text_elements
from .normalize import normalize_docling_document
from .utils import ensure_dir, now_iso, read_json, read_jsonl, safe_rel_uri, write_json, write_jsonl

DEFAULT_DOCLING_ROOT = Path(r"D:\金融科技大赛\Code\data\docling0816")
DEFAULT_OUTPUT_ROOT = Path(r"D:\金融科技大赛\Code\data\processed\docling0816\p0_full")
PRIORITY_SMOKE_DOC_IDS = ["385", "361", "388", "389"]
PROFILE_RUNS = [
    ("converted-docx", Path("converted-docx") / "v0.03" / "001"),
    ("native-docx", Path("native-docx") / "v0.04" / "001"),
    ("pdf", Path("pdf") / "v0.05" / "001"),
]
KNOWN_ISSUE_DOC_IDS = {"370": ["known_pdf370_layout_issue", "disable_numeric_qa_until_reparsed"]}


@dataclass
class DoclingP0Config:
    docling_root: Path = DEFAULT_DOCLING_ROOT
    output_root: Path = DEFAULT_OUTPUT_ROOT
    sample_size: int | None = None
    doc_ids: list[str] | None = None
    soft_limit: int = 450
    hard_limit: int = 650
    table_target_limit: int = 550
    table_hard_limit: int = 700
    embedding_backend: str = "api"
    collection: str = "rag_chunks_docling0816"
    rebuild_indexes: bool = False


@dataclass
class PipelineUnits:
    all_records: list[dict[str, Any]]
    selected_records: list[dict[str, Any]]
    raw_manifest_rows: list[dict[str, Any]]


@dataclass
class NormalizedBundle:
    documents: list[dict[str, Any]]
    elements: list[dict[str, Any]]
    table_cells: list[dict[str, Any]]
    exclusions: list[dict[str, Any]]
    errors: list[dict[str, Any]]


@dataclass
class ChunkBundle:
    chunks: list[dict[str, Any]]
    parent_chunks: list[dict[str, Any]]
    table_elements: list[dict[str, Any]]
    figure_elements: list[dict[str, Any]]
    formula_elements: list[dict[str, Any]]


def load_run_manifest(run_root: Path, source_profile: str) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(run_root / "manifest.jsonl")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        doc_id = str(row.get("doc_id") or "")
        if not doc_id:
            continue
        enriched = dict(row)
        enriched["source_profile"] = row.get("source_profile") or source_profile
        enriched["run_root"] = str(run_root)
        enriched["parsed_documents_root"] = str(run_root / "parsed_documents")
        out[doc_id] = enriched
    return out


def discover_inputs(docling_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source_profile, rel_run in PROFILE_RUNS:
        run_root = docling_root / rel_run
        parsed_root = run_root / "parsed_documents"
        if not parsed_root.exists():
            continue
        manifest = load_run_manifest(run_root, source_profile)
        for doc_dir in sorted([p for p in parsed_root.iterdir() if p.is_dir()], key=lambda p: int(p.name) if p.name.isdigit() else 999999):
            doc_id = doc_dir.name
            json_path = doc_dir / f"{doc_id}.json"
            if not json_path.exists():
                continue
            row = dict(manifest.get(doc_id, {}))
            row.update({
                "doc_id": doc_id,
                "source_profile": row.get("source_profile") or source_profile,
                "profile_run": rel_run.as_posix(),
                "run_root": str(run_root),
                "parsed_doc_dir": str(doc_dir),
                "json_path": str(json_path),
                "md_path": str(doc_dir / f"{doc_id}.md"),
                "html_path": str(doc_dir / f"{doc_id}.html"),
                "artifact_dir": str(doc_dir / f"{doc_id}_artifacts"),
                "known_issue_flags": KNOWN_ISSUE_DOC_IDS.get(doc_id, []),
            })
            records.append(row)
    return records


def select_records(config: DoclingP0Config, all_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_doc = {str(r["doc_id"]): r for r in all_records}
    if config.doc_ids:
        return [by_doc[d] for d in config.doc_ids if d in by_doc]
    if config.sample_size is None:
        return all_records
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for doc_id in PRIORITY_SMOKE_DOC_IDS:
        if doc_id in by_doc:
            selected.append(by_doc[doc_id])
            used.add(doc_id)
    for record in all_records:
        if len(selected) >= config.sample_size:
            break
        if record["doc_id"] not in used:
            selected.append(record)
            used.add(record["doc_id"])
    return selected


def write_raw_manifest(records: list[dict[str, Any]], docling_root: Path, output_root: Path) -> list[dict[str, Any]]:
    rows = []
    for r in records:
        json_path = Path(r["json_path"])
        md_path = Path(r["md_path"])
        html_path = Path(r["html_path"])
        artifact_dir = Path(r["artifact_dir"])
        rows.append({
            "schema_version": "raw_manifest.v2",
            "doc_id": r["doc_id"],
            "source_profile": r.get("source_profile"),
            "profile_run": r.get("profile_run"),
            "source_format": r.get("source_format"),
            "parser_input_format": r.get("parser_input_format"),
            "source_path": r.get("source_path"),
            "normalized_source_path": r.get("normalized_source_path"),
            "docling_json_uri": safe_rel_uri(docling_root, json_path, "raw"),
            "markdown_uri": safe_rel_uri(docling_root, md_path, "raw") if md_path.exists() else None,
            "html_uri": safe_rel_uri(docling_root, html_path, "raw") if html_path.exists() else None,
            "artifact_dir_uri": safe_rel_uri(docling_root, artifact_dir, "artifact") if artifact_dir.exists() else None,
            "source_sha256": r.get("source_sha256"),
            "normalized_source_sha256": r.get("normalized_source_sha256"),
            "docling_status": r.get("docling_status") or r.get("status"),
            "known_issue_flags": r.get("known_issue_flags") or [],
        })
    write_jsonl(output_root / "raw_manifest.jsonl", rows)
    return rows


def build_table_element_records(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in elements if e.get("element_type") == "table"]


def build_units(config: DoclingP0Config) -> PipelineUnits:
    """Stage 1: discover Docling outputs and freeze the raw unit manifest."""
    docling_root = config.docling_root
    output_root = config.output_root
    ensure_dir(output_root)

    all_records = discover_inputs(docling_root)
    selected_records = select_records(config, all_records)
    raw_manifest_rows = write_raw_manifest(selected_records, docling_root, output_root)
    return PipelineUnits(
        all_records=all_records,
        selected_records=selected_records,
        raw_manifest_rows=raw_manifest_rows,
    )


def build_normalized(config: DoclingP0Config, units: PipelineUnits) -> NormalizedBundle:
    """Stage 2: normalize profile-specific Docling JSON into a stable element contract."""
    docling_root = config.docling_root
    documents: list[dict[str, Any]] = []
    elements: list[dict[str, Any]] = []
    table_cells: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for record in units.selected_records:
        doc_id = str(record["doc_id"])
        try:
            doc_json = read_json(Path(record["json_path"]))
            doc_record, doc_elements, doc_table_cells, doc_exclusions = normalize_docling_document(
                doc_json, record, docling_root, Path(record.get("run_root") or docling_root)
            )
            documents.append(doc_record)
            elements.extend(doc_elements)
            table_cells.extend(doc_table_cells)
            exclusions.extend(doc_exclusions)
        except Exception as exc:  # pragma: no cover - report path
            errors.append({"doc_id": doc_id, "source_profile": record.get("source_profile"), "error": repr(exc)})
    return NormalizedBundle(
        documents=documents,
        elements=elements,
        table_cells=table_cells,
        exclusions=exclusions,
        errors=errors,
    )


def build_chunks(config: DoclingP0Config, normalized: NormalizedBundle) -> ChunkBundle:
    """Stage 3: chunk normalized text/table/figure/formula records."""
    by_doc: dict[str, dict[str, Any]] = {str(doc["doc_id"]): doc for doc in normalized.documents}
    elements_by_doc: dict[str, list[dict[str, Any]]] = {}
    cells_by_doc: dict[str, list[dict[str, Any]]] = {}
    for element in normalized.elements:
        elements_by_doc.setdefault(str(element.get("doc_id")), []).append(element)
    for cell in normalized.table_cells:
        cells_by_doc.setdefault(str(cell.get("doc_id")), []).append(cell)

    chunks: list[dict[str, Any]] = []
    parent_chunks: list[dict[str, Any]] = []
    for doc_id in sorted(by_doc, key=lambda x: int(x) if x.isdigit() else x):
        doc_record = by_doc[doc_id]
        doc_elements = elements_by_doc.get(doc_id, [])
        doc_table_cells = cells_by_doc.get(doc_id, [])
        doc_text_chunks, doc_parent_chunks = chunk_text_elements(
            doc_record, doc_elements, soft_limit=config.soft_limit, hard_limit=config.hard_limit
        )
        doc_modal_chunks = chunk_modal_elements(doc_record, doc_elements, start_order=len(doc_text_chunks))
        doc_table_chunks = chunk_tables(
            doc_record,
            build_table_element_records(doc_elements),
            doc_table_cells,
            target_limit=config.table_target_limit,
            hard_limit=config.table_hard_limit,
            start_order=len(doc_text_chunks) + len(doc_modal_chunks),
        )
        chunks.extend(doc_text_chunks + doc_modal_chunks + doc_table_chunks)
        parent_chunks.extend(doc_parent_chunks)

    for idx, chunk in enumerate(chunks):
        chunk["global_order"] = idx

    return ChunkBundle(
        chunks=chunks,
        parent_chunks=parent_chunks,
        table_elements=build_table_element_records(normalized.elements),
        figure_elements=[e for e in normalized.elements if e.get("element_type") == "figure"],
        formula_elements=[e for e in normalized.elements if e.get("element_type") == "formula"],
    )


def write_normalized_and_chunks(
    output_root: Path,
    normalized: NormalizedBundle,
    chunked: ChunkBundle,
) -> None:
    """Stage 4a: write JSONL/Object-Store artifacts used by retrieval and audit."""
    write_jsonl(output_root / "normalized_documents.jsonl", normalized.documents)
    write_jsonl(output_root / "normalized_elements.jsonl", normalized.elements)
    write_jsonl(output_root / "normalized_tables.jsonl", chunked.table_elements)
    write_jsonl(output_root / "figures.jsonl", chunked.figure_elements)
    write_jsonl(output_root / "formulas.jsonl", chunked.formula_elements)
    write_jsonl(output_root / "table_cells.jsonl", normalized.table_cells)
    write_jsonl(output_root / "chunks.jsonl", chunked.chunks)
    write_jsonl(output_root / "parent_chunks.jsonl", chunked.parent_chunks)
    write_jsonl(output_root / "exclusions.jsonl", normalized.exclusions)
    write_jsonl(output_root / "errors.jsonl", normalized.errors)


def describe_index_rebuild(config: DoclingP0Config) -> dict[str, Any]:
    """Stage 4b: declare the P2 index contract without rebuilding by default."""
    output_root = config.output_root
    return {
        "enabled": bool(config.rebuild_indexes),
        "status": "planned_only" if not config.rebuild_indexes else "external_entrypoint_required",
        "entrypoint": "rag_doc_ingestion/scripts/run_docling_p2_index.py",
        "build_root": str(output_root),
        "duckdb_path": str(output_root / "indexes" / "duckdb" / "rag_tables.duckdb"),
        "qdrant_path": str(output_root / "indexes" / "qdrant_local"),
        "collection": config.collection,
        "embedding_backend": config.embedding_backend,
        "note": (
            "P0 pipeline only writes normalized/chunk artifacts. "
            "DuckDB and Qdrant are rebuilt by run_docling_p2_index.py when explicitly required."
        ),
    }


def run_pipeline(config: DoclingP0Config) -> dict[str, Any]:
    output_root = config.output_root
    ensure_dir(output_root)

    units = build_units(config)
    normalized = build_normalized(config, units)
    chunked = build_chunks(config, normalized)
    write_normalized_and_chunks(output_root, normalized, chunked)

    token_counts = sorted([c.get("token_count", 0) for c in chunked.chunks if isinstance(c.get("token_count"), int)])

    def percentile(p: float) -> int:
        if not token_counts:
            return 0
        idx = min(len(token_counts) - 1, int(round((len(token_counts) - 1) * p)))
        return token_counts[idx]

    report = {
        "schema_version": "docling_p0_build_report.v2",
        "created_at": now_iso(),
        "pipeline_stages": ["units", "normalized", "chunking", "index_rebuild"],
        "docling_root": str(config.docling_root),
        "output_root": str(output_root),
        "sample_size": config.sample_size,
        "selected_doc_ids": [r["doc_id"] for r in units.selected_records],
        "raw_manifest_count": len(units.raw_manifest_rows),
        "doc_count": len(normalized.documents),
        "available_doc_count": len(units.all_records),
        "error_count": len(normalized.errors),
        "element_count": len(normalized.elements),
        "chunk_count": len(chunked.chunks),
        "parent_chunk_count": len(chunked.parent_chunks),
        "table_count": len(chunked.table_elements),
        "figure_count": len(chunked.figure_elements),
        "formula_count": len(chunked.formula_elements),
        "table_cell_count": len(normalized.table_cells),
        "exclusion_count": len(normalized.exclusions),
        "source_profile_counts": count_by(normalized.documents, "source_profile"),
        "quality_counts": count_by(normalized.elements, "quality_status"),
        "chunk_type_counts": count_by(chunked.chunks, "chunk_type"),
        "element_type_counts": count_by(normalized.elements, "element_type"),
        "token_p50": percentile(0.50),
        "token_p90": percentile(0.90),
        "token_p99": percentile(0.99),
        "oversized_chunks": [
            {"chunk_id": c["chunk_id"], "token_count": c.get("token_count"), "chunk_type": c.get("chunk_type")}
            for c in chunked.chunks if (c.get("token_count") or 0) > config.hard_limit
        ][:50],
        "known_issue_doc_ids": KNOWN_ISSUE_DOC_IDS,
        "index_rebuild": describe_index_rebuild(config),
        "outputs": {
            "raw_manifest": str(output_root / "raw_manifest.jsonl"),
            "normalized_documents": str(output_root / "normalized_documents.jsonl"),
            "normalized_elements": str(output_root / "normalized_elements.jsonl"),
            "normalized_tables": str(output_root / "normalized_tables.jsonl"),
            "figures": str(output_root / "figures.jsonl"),
            "formulas": str(output_root / "formulas.jsonl"),
            "table_cells": str(output_root / "table_cells.jsonl"),
            "chunks": str(output_root / "chunks.jsonl"),
            "parent_chunks": str(output_root / "parent_chunks.jsonl"),
            "exclusions": str(output_root / "exclusions.jsonl"),
            "errors": str(output_root / "errors.jsonl"),
        },
    }
    write_json(output_root / "build_report.json", report)
    return report


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build P0 chunks from docling0816 parsed documents.")
    parser.add_argument("--docling-root", type=Path, default=DEFAULT_DOCLING_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--sample-size", type=int, default=0, help="Number of documents for smoke test. Use 0 for all docs.")
    parser.add_argument("--doc-ids", nargs="*", default=None, help="Explicit doc ids, e.g. 385 361 388 389")
    parser.add_argument("--soft-limit", type=int, default=450)
    parser.add_argument("--hard-limit", type=int, default=650)
    parser.add_argument("--embedding-backend", default="api")
    parser.add_argument("--collection", default="rag_chunks_docling0816")
    parser.add_argument(
        "--rebuild-indexes",
        action="store_true",
        help="Only records the P2 index rebuild intent. Use run_docling_p2_index.py for the actual rebuild.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sample_size = None if args.sample_size == 0 else args.sample_size
    config = DoclingP0Config(
        docling_root=args.docling_root,
        output_root=args.output_root,
        sample_size=sample_size,
        doc_ids=args.doc_ids,
        soft_limit=args.soft_limit,
        hard_limit=args.hard_limit,
        embedding_backend=args.embedding_backend,
        collection=args.collection,
        rebuild_indexes=args.rebuild_indexes,
    )
    report = run_pipeline(config)
    print(json.dumps({
        "output_root": report["output_root"],
        "doc_count": report["doc_count"],
        "element_count": report["element_count"],
        "chunk_count": report["chunk_count"],
        "table_count": report["table_count"],
        "figure_count": report["figure_count"],
        "formula_count": report["formula_count"],
        "table_cell_count": report["table_cell_count"],
        "error_count": report["error_count"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
