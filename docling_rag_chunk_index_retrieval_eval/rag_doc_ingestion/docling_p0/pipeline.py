from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import chunk_modal_elements, chunk_tables, chunk_text_elements
from .indexes import build_bm25_index, build_hash_vector_index, build_table_sqlite
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
    vector_dim: int = 256
    skip_legacy_indexes: bool = False


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


def run_pipeline(config: DoclingP0Config) -> dict[str, Any]:
    docling_root = config.docling_root
    output_root = config.output_root
    ensure_dir(output_root)
    ensure_dir(output_root / "indexes")

    all_records = discover_inputs(docling_root)
    selected_records = select_records(config, all_records)

    documents: list[dict[str, Any]] = []
    elements: list[dict[str, Any]] = []
    table_cells: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    parent_chunks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    raw_manifest_rows = write_raw_manifest(selected_records, docling_root, output_root)

    for record in selected_records:
        doc_id = str(record["doc_id"])
        try:
            doc_json = read_json(Path(record["json_path"]))
            doc_record, doc_elements, doc_table_cells, doc_exclusions = normalize_docling_document(
                doc_json, record, docling_root, Path(record.get("run_root") or docling_root)
            )
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
            documents.append(doc_record)
            elements.extend(doc_elements)
            table_cells.extend(doc_table_cells)
            exclusions.extend(doc_exclusions)
            chunks.extend(doc_text_chunks + doc_modal_chunks + doc_table_chunks)
            parent_chunks.extend(doc_parent_chunks)
        except Exception as exc:  # pragma: no cover - report path
            errors.append({"doc_id": doc_id, "source_profile": record.get("source_profile"), "error": repr(exc)})

    for idx, chunk in enumerate(chunks):
        chunk["global_order"] = idx

    table_elements = build_table_element_records(elements)
    figure_elements = [e for e in elements if e.get("element_type") == "figure"]
    formula_elements = [e for e in elements if e.get("element_type") == "formula"]

    write_jsonl(output_root / "normalized_documents.jsonl", documents)
    write_jsonl(output_root / "normalized_elements.jsonl", elements)
    write_jsonl(output_root / "normalized_tables.jsonl", table_elements)
    write_jsonl(output_root / "figures.jsonl", figure_elements)
    write_jsonl(output_root / "formulas.jsonl", formula_elements)
    write_jsonl(output_root / "table_cells.jsonl", table_cells)
    write_jsonl(output_root / "chunks.jsonl", chunks)
    write_jsonl(output_root / "parent_chunks.jsonl", parent_chunks)
    write_jsonl(output_root / "exclusions.jsonl", exclusions)
    write_jsonl(output_root / "errors.jsonl", errors)

    bm25_stats: dict[str, Any] = {"skipped": True}
    vector_stats: dict[str, Any] = {"skipped": True}
    sqlite_stats: dict[str, Any] = {"skipped": True}
    if not config.skip_legacy_indexes:
        bm25_stats = build_bm25_index(chunks, output_root / "indexes" / "bm25")
        vector_stats = build_hash_vector_index(chunks, output_root / "indexes" / "vector", dim=config.vector_dim)
        sqlite_stats = build_table_sqlite(
            documents, elements, table_cells, chunks, output_root / "indexes" / "sqlite" / "rag_tables.sqlite"
        )

    token_counts = sorted([c.get("token_count", 0) for c in chunks if isinstance(c.get("token_count"), int)])

    def percentile(p: float) -> int:
        if not token_counts:
            return 0
        idx = min(len(token_counts) - 1, int(round((len(token_counts) - 1) * p)))
        return token_counts[idx]

    report = {
        "schema_version": "docling_p0_build_report.v2",
        "created_at": now_iso(),
        "docling_root": str(docling_root),
        "output_root": str(output_root),
        "sample_size": config.sample_size,
        "selected_doc_ids": [r["doc_id"] for r in selected_records],
        "raw_manifest_count": len(raw_manifest_rows),
        "doc_count": len(documents),
        "available_doc_count": len(all_records),
        "error_count": len(errors),
        "element_count": len(elements),
        "chunk_count": len(chunks),
        "parent_chunk_count": len(parent_chunks),
        "table_count": len(table_elements),
        "figure_count": len(figure_elements),
        "formula_count": len(formula_elements),
        "table_cell_count": len(table_cells),
        "exclusion_count": len(exclusions),
        "source_profile_counts": count_by(documents, "source_profile"),
        "quality_counts": count_by(elements, "quality_status"),
        "chunk_type_counts": count_by(chunks, "chunk_type"),
        "element_type_counts": count_by(elements, "element_type"),
        "token_p50": percentile(0.50),
        "token_p90": percentile(0.90),
        "token_p99": percentile(0.99),
        "oversized_chunks": [
            {"chunk_id": c["chunk_id"], "token_count": c.get("token_count"), "chunk_type": c.get("chunk_type")}
            for c in chunks if (c.get("token_count") or 0) > config.hard_limit
        ][:50],
        "known_issue_doc_ids": KNOWN_ISSUE_DOC_IDS,
        "bm25": bm25_stats,
        "vector": vector_stats,
        "sqlite": sqlite_stats,
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
    parser.add_argument("--vector-dim", type=int, default=256)
    parser.add_argument("--skip-legacy-indexes", action="store_true", help="Only write JSONL/Object Store files; skip old local BM25/hash-vector/SQLite indexes.")
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
        vector_dim=args.vector_dim,
        skip_legacy_indexes=args.skip_legacy_indexes,
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
