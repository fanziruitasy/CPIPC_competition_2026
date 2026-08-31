from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import xlrd

from clean_life_insurance import discover_files as discover_life_files
from clean_insurance_industry import discover_files as discover_industry_files
from clean_property_insurance import discover_files as discover_property_files
from clean_region_premium import (
    file_hash,
    source_cell,
    xlsx_sheets,
)
from clean_region_premium import discover_files as discover_region_files
from clean_regulatory_tables import discover_files as discover_regulatory_files


OUTPUT_NAMES = {
    "rows": "workbook_rows.parquet",
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


def text_value(value: object) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value).strip()


def is_template_file(path: Path) -> bool:
    return path.name.partition("_")[0] in TEMPLATE_FILE_IDS


def load_sheets(path: Path) -> list[dict]:
    if path.suffix.lower() == ".xlsx":
        return xlsx_sheets(path)
    workbook = xlrd.open_workbook(path, on_demand=True)
    sheets = []
    for sheet in workbook.sheets():
        values = [sheet.row_values(index) for index in range(sheet.nrows)]
        sheets.append(
            {
                "name": sheet.name,
                "values": values,
                "formulas": [[None] * len(row) for row in values],
            }
        )
    workbook.release_resources()
    return sheets


def claimed_files(input_dir: Path) -> set[Path]:
    claimed = {path.resolve() for path, *_ in discover_region_files(input_dir)}
    claimed.update(path.resolve() for path, *_ in discover_life_files(input_dir))
    claimed.update(path.resolve() for path, *_ in discover_property_files(input_dir))
    claimed.update(path.resolve() for path, *_ in discover_industry_files(input_dir))
    claimed.update(path.resolve() for path, *_ in discover_regulatory_files(input_dir))
    return claimed


def discover_files(input_dir: Path) -> list[Path]:
    claimed = claimed_files(input_dir)
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file()
        and not path.name.startswith("~$")
        and path.suffix.lower() in {".xls", ".xlsx"}
        and not is_template_file(path)
        and path.resolve() not in claimed
    )


def parse_file(path: Path, input_dir: Path) -> tuple[list[dict], dict]:
    relative = path.relative_to(input_dir).as_posix()
    sha256 = file_hash(path)
    source_format = path.suffix.lower().lstrip(".")
    rows: list[dict] = []
    formula_cache_missing = 0
    formula_texts: list[str] = []
    cell_count = 0

    for sheet in load_sheets(path):
        for row_number, values in enumerate(sheet["values"], start=1):
            row_cells: list[tuple[str, str]] = []
            formulas = sheet["formulas"][row_number - 1]
            width = max(len(values), len(formulas))
            for column_number in range(1, width + 1):
                value = values[column_number - 1] if column_number <= len(values) else None
                formula = formulas[column_number - 1] if column_number <= len(formulas) else None
                value_text = text_value(value)
                formula_text = text_value(formula)
                if not value_text and not formula_text:
                    continue
                cell_count += 1
                if formula_text:
                    formula_texts.append(formula_text)
                formula_cache_missing += int(bool(formula_text and not value_text))
                row_cells.append(
                    (source_cell(row_number, column_number), value_text or formula_text)
                )

            if row_cells:
                rows.append(
                    {
                        "source_file": relative,
                        "source_file_hash": sha256,
                        "source_format": source_format,
                        "source_sheet": sheet["name"],
                        "row_number": row_number,
                        "source_range": f"{row_cells[0][0]}:{row_cells[-1][0]}",
                        "row_text": " | ".join(value for _, value in row_cells),
                        "nonempty_cell_count": len(row_cells),
                    }
                )

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
        "document_role": "reference_document",
        "default_qa_eligible": True,
        "source_file": relative,
        "source_format": source_format,
        "sheet_count": len({row["source_sheet"] for row in rows}),
        "cell_count": cell_count,
        "row_count": len(rows),
        "formula_cell_count": len(formula_texts),
        "distinct_formula_text_count": len(set(formula_texts)),
        "formula_cache_missing_count": formula_cache_missing,
        "formula_error_count": formula_error_count,
        "detail": "；".join(issues),
    }
    return rows, quality


def write_quality(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    assert text_value(None) == ""
    assert is_template_file(Path("394_template.xls"))
    assert is_template_file(Path("461_calculation.xlsx"))
    assert not is_template_file(Path("391_reference.xls"))


def run(input_dir: Path, output_dir: Path, check_only: bool) -> None:
    self_check()
    files = discover_files(input_dir)
    if not files:
        raise SystemExit("没有发现需要抽取的参考 Excel")

    rows: list[dict] = []
    quality: list[dict] = []
    for path in files:
        try:
            file_rows, item = parse_file(path, input_dir)
            rows.extend(file_rows)
            quality.append(item)
            print(
                f"{item['severity']} reference_document {path.name}: "
                f"cells={item['cell_count']} rows={len(file_rows)}"
            )
        except Exception as exc:
            quality.append(
                {
                    "record_type": "FILE",
                    "severity": "ERROR",
                    "status": "PARSE_ERROR",
                    "document_role": "reference_document",
                    "default_qa_eligible": True,
                    "source_file": path.relative_to(input_dir).as_posix(),
                    "source_format": path.suffix.lower().lstrip("."),
                    "sheet_count": 0,
                    "cell_count": 0,
                    "row_count": 0,
                    "formula_cell_count": 0,
                    "distinct_formula_text_count": 0,
                    "formula_cache_missing_count": 0,
                    "formula_error_count": 0,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"ERROR {path.name}: {exc}")

    errors = [item for item in quality if item["severity"] == "ERROR"]
    if check_only:
        print(f"CHECK files={len(files)} rows={len(rows)} errors={len(errors)}")
        if errors:
            raise SystemExit(1)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    write_quality(output_dir / OUTPUT_NAMES["quality"], quality)
    if errors:
        raise SystemExit(1)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=ROW_SCHEMA),
        output_dir / OUTPUT_NAMES["rows"],
        compression="zstd",
    )
    print(f"WROTE reference_files={len(files)} rows={len(rows)} to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将没有专用清洗器的有效参考 Excel 抽取为可追溯行文本"
    )
    parser.add_argument("--input-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output-dir", type=Path, default=Path.cwd() / "output_excel_documents_clean"
    )
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    run(args.input_dir.resolve(), args.output_dir.resolve(), args.check_only)


if __name__ == "__main__":
    main()
