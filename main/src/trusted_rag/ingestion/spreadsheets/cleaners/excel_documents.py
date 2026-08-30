"""抽取模板和参考工作簿中的单元格、行与文本块。"""

# ruff: noqa: D103

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from trusted_rag.ingestion.spreadsheets.cleaners.life_insurance import (
    discover_files as discover_life_files,
)
from trusted_rag.ingestion.spreadsheets.cleaners.property_insurance import (
    TARGET_RE as PROPERTY_TARGET_RE,
)
from trusted_rag.ingestion.spreadsheets.cleaners.region_premium import (
    discover_files as discover_region_files,
)
from trusted_rag.ingestion.spreadsheets.cleaners.region_premium import (
    file_hash,
    source_cell,
    xls_sheets,
    xlsx_sheets,
)
from trusted_rag.ingestion.spreadsheets.cleaners.regulatory_tables import (
    discover_files as discover_regulatory_files,
)

OUTPUT_NAMES = {
    "cells": "workbook_cells.parquet",
    "rows": "workbook_rows.parquet",
    "chunks": "workbook_chunks.parquet",
    "template_cells": "template_cells.parquet",
    "template_rows": "template_rows.parquet",
    "template_chunks": "template_chunks.parquet",
    "quality": "quality_report.csv",
}

TEMPLATE_FILE_IDS = {
    "394",
    "395",
    "441",
    "444",
    "445",
    "446",
    "449",
    "461",
    "464",
    "466",
    "468",
    "469",
    "470",
    "471",
    "496",
}

CELL_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("source_sheet", pa.string()),
        ("row_number", pa.int32()),
        ("column_number", pa.int32()),
        ("source_cell", pa.string()),
        ("value_text", pa.string()),
        ("value_type", pa.string()),
        ("formula_raw", pa.string()),
        ("number_format", pa.string()),
    ]
)

ROW_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("source_sheet", pa.string()),
        ("row_number", pa.int32()),
        ("source_range", pa.string()),
        ("row_text", pa.string()),
        ("nonempty_cell_count", pa.int32()),
    ]
)

CHUNK_SCHEMA = pa.schema(
    [
        ("chunk_id", pa.string()),
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("source_sheet", pa.string()),
        ("start_row", pa.int32()),
        ("end_row", pa.int32()),
        ("source_range", pa.string()),
        ("text", pa.string()),
    ]
)


def text_value(value: object) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value).strip()


def load_sheets(path: Path) -> list[dict]:
    if path.suffix.lower() == ".xlsx":
        return xlsx_sheets(path)
    with TemporaryDirectory() as directory:
        copy = Path(directory) / "source.xls"
        shutil.copy2(path, copy)
        return xls_sheets(copy)


def claimed_files(input_dir: Path) -> set[Path]:
    from trusted_rag.ingestion.spreadsheets.cleaners.insurance_industry import (
        discover_files as discover_industry_files,
    )

    claimed = {path.resolve() for path, *_ in discover_region_files(input_dir)}
    claimed.update(path.resolve() for path, *_ in discover_life_files(input_dir))
    claimed.update(path.resolve() for path, *_ in discover_industry_files(input_dir))
    claimed.update(path.resolve() for path, *_ in discover_regulatory_files(input_dir))
    claimed.update(
        path.resolve()
        for path in input_dir.iterdir()
        if path.is_file() and PROPERTY_TARGET_RE.match(path.name)
    )
    return claimed


def discover_files(input_dir: Path, include_claimed_fallback: bool = False) -> list[Path]:
    claimed = claimed_files(input_dir)
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file()
        and not path.name.startswith("~$")
        and path.suffix.lower() in {".xls", ".xlsx"}
        and (include_claimed_fallback or path.resolve() not in claimed)
    )


def is_template_file(path: Path) -> bool:
    return path.name.partition("_")[0] in TEMPLATE_FILE_IDS


def make_chunks(rows: list[dict], max_chars: int = 4000) -> list[dict]:
    chunks = []
    current = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        first, last = current[0], current[-1]
        text = "\n".join(f"R{row['row_number']}: {row['row_text']}" for row in current)
        identity = f"{first['source_file']}|{first['source_sheet']}|{first['row_number']}|{last['row_number']}"
        chunks.append(
            {
                "chunk_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                "source_file": first["source_file"],
                "source_file_hash": first["source_file_hash"],
                "source_format": first["source_format"],
                "source_sheet": first["source_sheet"],
                "start_row": first["row_number"],
                "end_row": last["row_number"],
                "source_range": f"{first['source_range'].split(':')[0]}:{last['source_range'].split(':')[-1]}",
                "text": text,
            }
        )
        current, current_chars = [], 0

    for row in rows:
        line_chars = len(row["row_text"]) + 8
        if current and (
            row["source_sheet"] != current[-1]["source_sheet"]
            or current_chars + line_chars > max_chars
        ):
            flush()
        current.append(row)
        current_chars += line_chars
    flush()
    return chunks


def parse_file(
    path: Path, input_dir: Path
) -> tuple[list[dict], list[dict], list[dict], dict]:
    relative = path.relative_to(input_dir).as_posix()
    sha256 = file_hash(path)
    source_format = path.suffix.lower().lstrip(".")
    cells, rows = [], []
    formula_cache_missing = 0
    formula_texts = []

    for sheet in load_sheets(path):
        sheet_rows = []
        for row_number, values in enumerate(sheet["values"], start=1):
            row_cells = []
            width = max(
                len(values),
                len(sheet["formulas"][row_number - 1]),
                len(sheet["formats"][row_number - 1]),
            )
            for column_number in range(1, width + 1):
                value = (
                    values[column_number - 1] if column_number <= len(values) else None
                )
                formula = (
                    sheet["formulas"][row_number - 1][column_number - 1]
                    if column_number <= len(sheet["formulas"][row_number - 1])
                    else None
                )
                number_format = (
                    sheet["formats"][row_number - 1][column_number - 1]
                    if column_number <= len(sheet["formats"][row_number - 1])
                    else None
                )
                value_text = text_value(value)
                formula_text = text_value(formula)
                if not value_text and not formula_text:
                    continue
                if formula_text:
                    formula_texts.append(formula_text)
                formula_cache_missing += int(bool(formula_text and not value_text))
                cell = {
                    "source_file": relative,
                    "source_file_hash": sha256,
                    "source_format": source_format,
                    "source_sheet": sheet["name"],
                    "row_number": row_number,
                    "column_number": column_number,
                    "source_cell": source_cell(row_number, column_number),
                    "value_text": value_text,
                    "value_type": type(value).__name__
                    if value is not None
                    else "formula_without_cached_value",
                    "formula_raw": formula_text,
                    "number_format": text_value(number_format),
                }
                cells.append(cell)
                row_cells.append(cell)

            if row_cells:
                start, end = row_cells[0]["source_cell"], row_cells[-1]["source_cell"]
                row = {
                    "source_file": relative,
                    "source_file_hash": sha256,
                    "source_format": source_format,
                    "source_sheet": sheet["name"],
                    "row_number": row_number,
                    "source_range": f"{start}:{end}",
                    "row_text": " | ".join(
                        cell["value_text"] or cell["formula_raw"]
                        for cell in row_cells
                    ),
                    "nonempty_cell_count": len(row_cells),
                }
                rows.append(row)
                sheet_rows.append(row)

    chunks = make_chunks(rows)
    template = is_template_file(path)
    formula_error_count = sum(
        bool(re.search(r"#REF!|#VALUE!|#DIV/0!|#N/A", formula, re.IGNORECASE))
        for formula in formula_texts
    )
    issues = []
    if formula_cache_missing:
        issues.append(f"{formula_cache_missing} 个公式缺少缓存值")
    if formula_error_count:
        issues.append(f"{formula_error_count} 个公式包含错误引用/错误值")
    quality = {
        "record_type": "FILE",
        "severity": "WARN" if issues else "PASS",
        "status": (
            "FORMULA_ERROR"
            if formula_error_count
            else "FORMULA_CACHE_MISSING"
            if formula_cache_missing
            else "CLEAN"
        ),
        "document_role": "template_library" if template else "reference_document",
        "default_qa_eligible": not template,
        "source_file": relative,
        "source_format": source_format,
        "sheet_count": len({row["source_sheet"] for row in rows}),
        "cell_count": len(cells),
        "row_count": len(rows),
        "chunk_count": len(chunks),
        "formula_cell_count": len(formula_texts),
        "distinct_formula_text_count": len(set(formula_texts)),
        "formula_cache_missing_count": formula_cache_missing,
        "formula_error_count": formula_error_count,
        "detail": "；".join(issues),
    }
    return cells, rows, chunks, quality


def write_quality(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    sample = [
        {
            "source_file": "a.xlsx",
            "source_file_hash": "x",
            "source_format": "xlsx",
            "source_sheet": "S",
            "row_number": 1,
            "source_range": "A1:B1",
            "row_text": "a | b",
            "nonempty_cell_count": 2,
        }
    ]
    assert make_chunks(sample, 10)[0]["source_range"] == "A1:B1"
    assert text_value(None) == ""
    assert is_template_file(Path("394_template.xls"))
    assert is_template_file(Path("461_calculation.xlsx"))
    assert not is_template_file(Path("391_reference.xls"))


def run(
    input_dir: Path,
    output_dir: Path,
    check_only: bool,
    include_claimed_fallback: bool = False,
) -> None:
    self_check()
    claimed = claimed_files(input_dir)
    files = discover_files(input_dir, include_claimed_fallback)
    if not files:
        raise SystemExit("没有发现需要通用抽取的 Excel 文件")

    ordinary = {"cells": [], "rows": [], "chunks": []}
    templates = {"cells": [], "rows": [], "chunks": []}
    quality = []
    for path in files:
        raw_fallback = path.resolve() in claimed
        try:
            cells, rows, chunks, qc = parse_file(path, input_dir)
            if raw_fallback:
                qc.update(
                    record_type="RAW_FALLBACK",
                    document_role="semantic_raw_fallback",
                    default_qa_eligible=False,
                )
            target = templates if is_template_file(path) else ordinary
            target["cells"].extend(cells)
            target["rows"].extend(rows)
            target["chunks"].extend(chunks)
            quality.append(qc)
            print(
                f"{qc['severity']} {qc['document_role']} {path.name}: "
                f"cells={len(cells)} rows={len(rows)} chunks={len(chunks)}"
            )
        except Exception as exc:
            template = is_template_file(path)
            quality.append(
                {
                    "record_type": "RAW_FALLBACK" if raw_fallback else "FILE",
                    "severity": "WARN" if raw_fallback else "ERROR",
                    "status": "RAW_FALLBACK_PARSE_ERROR" if raw_fallback else "PARSE_ERROR",
                    "document_role": (
                        "semantic_raw_fallback"
                        if raw_fallback
                        else "template_library"
                        if template
                        else "reference_document"
                    ),
                    "default_qa_eligible": False if raw_fallback else not template,
                    "source_file": path.relative_to(input_dir).as_posix(),
                    "source_format": path.suffix.lower().lstrip("."),
                    "sheet_count": 0,
                    "cell_count": 0,
                    "row_count": 0,
                    "chunk_count": 0,
                    "formula_cell_count": 0,
                    "distinct_formula_text_count": 0,
                    "formula_cache_missing_count": 0,
                    "formula_error_count": 0,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"{'WARN' if raw_fallback else 'ERROR'} {path.name}: {exc}")

    errors = [row for row in quality if row["severity"] == "ERROR"]
    if check_only:
        print(
            f"CHECK files={len(files)} ordinary_cells={len(ordinary['cells'])} "
            f"template_cells={len(templates['cells'])} "
            f"raw_fallback_files={sum(path.resolve() in claimed for path in files)} "
            f"errors={len(errors)}"
        )
        if errors:
            raise SystemExit(1)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    write_quality(output_dir / OUTPUT_NAMES["quality"], quality)
    if errors:
        raise SystemExit(1)
    for records, schema, output_name in (
        (ordinary["cells"], CELL_SCHEMA, "cells"),
        (ordinary["rows"], ROW_SCHEMA, "rows"),
        (ordinary["chunks"], CHUNK_SCHEMA, "chunks"),
        (templates["cells"], CELL_SCHEMA, "template_cells"),
        (templates["rows"], ROW_SCHEMA, "template_rows"),
        (templates["chunks"], CHUNK_SCHEMA, "template_chunks"),
    ):
        pq.write_table(
            pa.Table.from_pylist(records, schema=schema),
            output_dir / OUTPUT_NAMES[output_name],
            compression="zstd",
        )
    print(
        f"WROTE ordinary_files={sum(not is_template_file(path) for path in files)} "
        f"template_files={sum(is_template_file(path) for path in files)} "
        f"raw_fallback_files={sum(path.resolve() in claimed for path in files)} to {output_dir}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将没有专用清洗器的 Excel 抽取为可追溯单元格、行和 RAG 文本块"
    )
    parser.add_argument("--input-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output-dir", type=Path, default=Path.cwd() / "output_excel_documents_clean"
    )
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--include-claimed-fallback",
        action="store_true",
        help="同时保留已由专用清洗器认领文件的原始单元格/行，作为语义事实缺失时的回退层",
    )
    args = parser.parse_args()
    run(
        args.input_dir.resolve(),
        args.output_dir.resolve(),
        args.check_only,
        args.include_claimed_fallback,
    )


if __name__ == "__main__":
    main()
