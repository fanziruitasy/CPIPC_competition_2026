from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import pyarrow as pa
import pyarrow.parquet as pq

from clean_excel_documents import TEMPLATE_FILE_IDS, text_value
from clean_region_premium import file_hash, source_cell, xls_sheets
from reclassify_excel_clean import classify_document


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = ROOT / "data" / "nfra_page_attachments_500"
DEFAULT_OUTPUT_DIR = ROOT / "output_excel_templates_clean"
DEFAULT_CACHE_DIR = ROOT / ".template_schema_cache"

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CELL_REF_RE = re.compile(r"(?<![A-Z0-9_])(\$?[A-Z]{1,3})\$?\d+", re.I)
LEGACY_SCHEMA_ROW_LIMIT = 100

CATALOG_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("content_type_code", pa.string()),
        ("content_type_name", pa.string()),
        ("domain_code", pa.string()),
        ("domain_name", pa.string()),
        ("topic_code", pa.string()),
        ("topic_name", pa.string()),
        ("dataset_family", pa.string()),
        ("document_function", pa.string()),
        ("sheet_count", pa.int32()),
        ("field_count", pa.int32()),
        ("constant_cell_count", pa.int32()),
        ("formula_pattern_count", pa.int32()),
        ("formula_cell_count", pa.int32()),
        ("validation_count", pa.int32()),
        ("parser", pa.string()),
        ("formula_extraction_status", pa.string()),
        ("validation_extraction_status", pa.string()),
        ("default_qa_eligible", pa.bool_()),
    ]
)

FIELD_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("source_sheet", pa.string()),
        ("source_cell", pa.string()),
        ("field_text", pa.string()),
        ("field_kind", pa.string()),
        ("value_type", pa.string()),
        ("number_format", pa.string()),
    ]
)

FORMULA_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("source_sheet", pa.string()),
        ("sample_cell", pa.string()),
        ("formula_raw", pa.string()),
        ("formula_pattern", pa.string()),
        ("occurrence_count", pa.int32()),
    ]
)

VALIDATION_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("source_sheet", pa.string()),
        ("source_range", pa.string()),
        ("validation_type", pa.string()),
        ("operator", pa.string()),
        ("allow_blank", pa.bool_()),
        ("formula1", pa.string()),
        ("formula2", pa.string()),
        ("prompt_title", pa.string()),
        ("prompt", pa.string()),
        ("error_title", pa.string()),
        ("error", pa.string()),
    ]
)


def discover_files(input_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".xls", ".xlsx"}
        and path.name.partition("_")[0] in TEMPLATE_FILE_IDS
    )


def formula_pattern(formula: str) -> str:
    normalized = " ".join(formula.strip().upper().split())
    return CELL_REF_RE.sub(r"\1{ROW}", normalized)


def _shared_strings(archive: ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = pa.py_buffer(archive.read(path)).to_pybytes()
    tree = __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).fromstring(root)
    return ["".join(item.itertext()).strip() for item in tree.findall(f"{MAIN_NS}si")]


def _number_formats(archive: ZipFile) -> list[str]:
    path = "xl/styles.xml"
    if path not in archive.namelist():
        return []
    import xml.etree.ElementTree as element_tree

    root = element_tree.fromstring(archive.read(path))
    custom = {
        int(node.attrib["numFmtId"]): node.attrib.get("formatCode", "")
        for node in root.findall(f".//{MAIN_NS}numFmt")
    }
    built_in = {0: "General", 1: "0", 2: "0.00", 9: "0%", 10: "0.00%", 14: "yyyy-mm-dd"}
    formats = []
    cell_xfs = root.find(f"{MAIN_NS}cellXfs")
    if cell_xfs is None:
        return formats
    for xf in cell_xfs.findall(f"{MAIN_NS}xf"):
        number_format_id = int(xf.attrib.get("numFmtId", "0"))
        formats.append(custom.get(number_format_id, built_in.get(number_format_id, str(number_format_id))))
    return formats


def _cell_value(cell, shared_strings: list[str]) -> tuple[str, str]:
    cell_type = cell.attrib.get("t", "n")
    value_node = cell.find(f"{MAIN_NS}v")
    if cell_type == "inlineStr":
        inline = cell.find(f"{MAIN_NS}is")
        return ("".join(inline.itertext()).strip() if inline is not None else "", "string")
    raw = value_node.text if value_node is not None and value_node.text is not None else ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)].strip(), "string"
        except (ValueError, IndexError):
            return raw, "string"
    if cell_type == "b":
        return ("TRUE" if raw == "1" else "FALSE"), "boolean"
    if cell_type in {"str", "e"}:
        return raw.strip(), "string" if cell_type == "str" else "error"
    return raw.strip(), "number"


def _worksheet_paths(archive: ZipFile) -> list[tuple[str, str]]:
    import xml.etree.ElementTree as element_tree

    workbook = element_tree.fromstring(archive.read("xl/workbook.xml"))
    relationships = element_tree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        node.attrib["Id"]: node.attrib["Target"]
        for node in relationships.findall(f"{PKG_REL_NS}Relationship")
    }
    result = []
    for sheet in workbook.findall(f".//{MAIN_NS}sheet"):
        relationship_id = sheet.attrib[f"{DOC_REL_NS}id"]
        target = targets[relationship_id].replace("\\", "/")
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = str(PurePosixPath("xl") / target)
        result.append((sheet.attrib["name"], str(PurePosixPath(path))))
    return result


def _bool_attr(value: str | None) -> bool:
    return str(value).lower() in {"1", "true"}


def parse_xlsx(path: Path, input_dir: Path) -> dict[str, list[dict] | dict]:
    import xml.etree.ElementTree as element_tree

    relative = path.relative_to(input_dir).as_posix()
    source_hash = file_hash(path)
    fields: list[dict] = []
    validations: list[dict] = []
    formula_groups: dict[tuple[str, str], dict] = {}
    formula_cell_count = 0
    constant_cell_count = 0
    sheet_names = []

    with ZipFile(path) as archive:
        shared_strings = _shared_strings(archive)
        number_formats = _number_formats(archive)
        for sheet_name, sheet_path in _worksheet_paths(archive):
            sheet_names.append(sheet_name)
            root = element_tree.fromstring(archive.read(sheet_path))
            for cell in root.findall(f".//{MAIN_NS}c"):
                coordinate = cell.attrib.get("r", "")
                formula_node = cell.find(f"{MAIN_NS}f")
                formula = ""
                if formula_node is not None:
                    formula = (formula_node.text or "").strip()
                    if not formula:
                        formula = f"SHARED_FORMULA:{formula_node.attrib.get('si', '')}"
                value, value_type = _cell_value(cell, shared_strings)
                style_index = int(cell.attrib.get("s", "0"))
                number_format = number_formats[style_index] if style_index < len(number_formats) else ""
                if formula:
                    formula_cell_count += 1
                    pattern = formula_pattern(formula)
                    key = (sheet_name, pattern)
                    if key not in formula_groups:
                        formula_groups[key] = {
                            "source_file": relative,
                            "source_file_hash": source_hash,
                            "source_format": "xlsx",
                            "source_sheet": sheet_name,
                            "sample_cell": coordinate,
                            "formula_raw": formula,
                            "formula_pattern": pattern,
                            "occurrence_count": 0,
                        }
                    formula_groups[key]["occurrence_count"] += 1
                elif value and value_type == "string":
                    fields.append(
                        {
                            "source_file": relative,
                            "source_file_hash": source_hash,
                            "source_format": "xlsx",
                            "source_sheet": sheet_name,
                            "source_cell": coordinate,
                            "field_text": value,
                            "field_kind": "label",
                            "value_type": value_type,
                            "number_format": number_format,
                        }
                    )
                elif value:
                    constant_cell_count += 1

            for validation in root.findall(f".//{MAIN_NS}dataValidation"):
                def child_text(name: str) -> str:
                    child = validation.find(f"{MAIN_NS}{name}")
                    return (child.text or "").strip() if child is not None else ""

                validations.append(
                    {
                        "source_file": relative,
                        "source_file_hash": source_hash,
                        "source_format": "xlsx",
                        "source_sheet": sheet_name,
                        "source_range": validation.attrib.get("sqref", ""),
                        "validation_type": validation.attrib.get("type", ""),
                        "operator": validation.attrib.get("operator", ""),
                        "allow_blank": _bool_attr(validation.attrib.get("allowBlank")),
                        "formula1": child_text("formula1"),
                        "formula2": child_text("formula2"),
                        "prompt_title": validation.attrib.get("promptTitle", ""),
                        "prompt": validation.attrib.get("prompt", ""),
                        "error_title": validation.attrib.get("errorTitle", ""),
                        "error": validation.attrib.get("error", ""),
                    }
                )

    return {
        "fields": fields,
        "formulas": list(formula_groups.values()),
        "validations": validations,
        "metadata": {
            "source_file": relative,
            "source_file_hash": source_hash,
            "source_format": "xlsx",
            "sheet_names": sheet_names,
            "formula_cell_count": formula_cell_count,
            "constant_cell_count": constant_cell_count,
            "parser": "direct_ooxml",
            "formula_extraction_status": "complete",
            "validation_extraction_status": "complete",
        },
    }


def parse_xls_com(path: Path, input_dir: Path) -> dict[str, list[dict] | dict]:
    relative = path.relative_to(input_dir).as_posix()
    source_hash = file_hash(path)
    fields: list[dict] = []
    formula_groups: dict[tuple[str, str], dict] = {}
    formula_cell_count = 0
    constant_cell_count = 0
    with TemporaryDirectory() as directory:
        copied = Path(directory) / "source.xls"
        shutil.copy2(path, copied)
        sheets = xls_sheets(copied)
    for sheet in sheets:
        for row_index, values in enumerate(sheet["values"], start=1):
            formulas = sheet["formulas"][row_index - 1]
            formats = sheet["formats"][row_index - 1]
            width = max(len(values), len(formulas), len(formats))
            for column_index in range(1, width + 1):
                value = values[column_index - 1] if column_index <= len(values) else None
                formula = formulas[column_index - 1] if column_index <= len(formulas) else None
                number_format = formats[column_index - 1] if column_index <= len(formats) else None
                coordinate = source_cell(row_index, column_index)
                formula_text = text_value(formula)
                value_text = text_value(value)
                if formula_text:
                    formula_cell_count += 1
                    pattern = formula_pattern(formula_text)
                    key = (sheet["name"], pattern)
                    if key not in formula_groups:
                        formula_groups[key] = {
                            "source_file": relative,
                            "source_file_hash": source_hash,
                            "source_format": "xls",
                            "source_sheet": sheet["name"],
                            "sample_cell": coordinate,
                            "formula_raw": formula_text,
                            "formula_pattern": pattern,
                            "occurrence_count": 0,
                        }
                    formula_groups[key]["occurrence_count"] += 1
                elif value_text and isinstance(value, str):
                    fields.append(
                        {
                            "source_file": relative,
                            "source_file_hash": source_hash,
                            "source_format": "xls",
                            "source_sheet": sheet["name"],
                            "source_cell": coordinate,
                            "field_text": value_text,
                            "field_kind": "label",
                            "value_type": type(value).__name__,
                            "number_format": text_value(number_format),
                        }
                    )
                elif value_text:
                    constant_cell_count += 1
    return {
        "fields": fields,
        "formulas": list(formula_groups.values()),
        "validations": [],
        "metadata": {
            "source_file": relative,
            "source_file_hash": source_hash,
            "source_format": "xls",
            "sheet_names": [sheet["name"] for sheet in sheets],
            "formula_cell_count": formula_cell_count,
            "constant_cell_count": constant_cell_count,
            "parser": "excel_com_structural",
            "formula_extraction_status": "complete",
            "validation_extraction_status": "unsupported_legacy_xls",
        },
    }


def parse_xls_xlrd(path: Path, input_dir: Path) -> dict[str, list[dict] | dict]:
    import xlrd

    relative = path.relative_to(input_dir).as_posix()
    source_hash = file_hash(path)
    workbook = xlrd.open_workbook(path, formatting_info=True, on_demand=True)
    fields: list[dict] = []
    constant_cell_count = 0
    sheet_names = workbook.sheet_names()
    for sheet in workbook.sheets():
        # Legacy county-reporting workbooks contain thousands of prefilled region
        # rows. Their schema lives in the heading area; retaining every region as
        # a "field" would recreate the row-expansion problem this cleaner avoids.
        for row_index in range(min(sheet.nrows, LEGACY_SCHEMA_ROW_LIMIT)):
            for column_index in range(sheet.ncols):
                cell = sheet.cell(row_index, column_index)
                value = text_value(cell.value)
                if not value:
                    continue
                if cell.ctype == xlrd.XL_CELL_TEXT:
                    fields.append(
                        {
                            "source_file": relative,
                            "source_file_hash": source_hash,
                            "source_format": "xls",
                            "source_sheet": sheet.name,
                            "source_cell": source_cell(row_index + 1, column_index + 1),
                            "field_text": value,
                            "field_kind": "label",
                            "value_type": "string",
                            "number_format": "",
                        }
                    )
                else:
                    constant_cell_count += 1
    workbook.release_resources()
    return {
        "fields": fields,
        "formulas": [],
        "validations": [],
        "metadata": {
            "source_file": relative,
            "source_file_hash": source_hash,
            "source_format": "xls",
            "sheet_names": sheet_names,
            "formula_cell_count": 0,
            "constant_cell_count": constant_cell_count,
            "parser": "xlrd_structural",
            "formula_extraction_status": "unavailable_legacy_xls",
            "validation_extraction_status": "unavailable_legacy_xls",
        },
    }


def parse_xls(path: Path, input_dir: Path) -> dict[str, list[dict] | dict]:
    try:
        return parse_xls_xlrd(path, input_dir)
    except ImportError:
        return parse_xls_com(path, input_dir)


def classify_template(parsed: dict[str, list[dict] | dict]) -> dict:
    metadata = parsed["metadata"]
    assert isinstance(metadata, dict)
    fields = parsed["fields"]
    assert isinstance(fields, list)
    text = "\n".join(metadata["sheet_names"] + [row["field_text"] for row in fields[:1000]])
    rule, _ = classify_document(text)
    content_type_code, content_type_name = rule["content_type"]
    domain_code, domain_name = rule["domain"]
    topic_code, topic_name = rule["topic"]
    return {
        "source_file": metadata["source_file"],
        "source_file_hash": metadata["source_file_hash"],
        "source_format": metadata["source_format"],
        "content_type_code": content_type_code,
        "content_type_name": content_type_name,
        "domain_code": domain_code,
        "domain_name": domain_name,
        "topic_code": topic_code,
        "topic_name": topic_name,
        "dataset_family": rule["family"],
        "document_function": rule["function"],
        "sheet_count": len(metadata["sheet_names"]),
        "field_count": len(parsed["fields"]),
        "constant_cell_count": metadata["constant_cell_count"],
        "formula_pattern_count": len(parsed["formulas"]),
        "formula_cell_count": metadata["formula_cell_count"],
        "validation_count": len(parsed["validations"]),
        "parser": metadata["parser"],
        "formula_extraction_status": metadata["formula_extraction_status"],
        "validation_extraction_status": metadata["validation_extraction_status"],
        "default_qa_eligible": False,
    }


def _cache_path(path: Path, cache_dir: Path) -> Path:
    stat = path.stat()
    identity = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|v3"
    return cache_dir / f"{hashlib.sha256(identity.encode()).hexdigest()}.json"


def parse_cached(path: Path, input_dir: Path, cache_dir: Path, use_cache: bool) -> dict:
    cache_path = _cache_path(path, cache_dir)
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    parsed = parse_xlsx(path, input_dir) if path.suffix.lower() == ".xlsx" else parse_xls(path, input_dir)
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
    return parsed


def write_table(records: list[dict], schema: pa.Schema, path: Path) -> None:
    pq.write_table(pa.Table.from_pylist(records, schema=schema), path, compression="zstd")


def run(
    input_dir: Path,
    output_dir: Path,
    check_only: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    use_cache: bool = True,
) -> None:
    files = discover_files(input_dir)
    if not files:
        raise SystemExit("没有发现模板 Excel")
    catalog: list[dict] = []
    records: dict[str, list[dict]] = defaultdict(list)
    errors = []
    for path in files:
        try:
            parsed = parse_cached(path, input_dir, cache_dir, use_cache)
            catalog.append(classify_template(parsed))
            for name in ("fields", "formulas", "validations"):
                records[name].extend(parsed[name])
            item = catalog[-1]
            print(
                f"PASS template {path.name}: sheets={item['sheet_count']} "
                f"fields={item['field_count']} formula_patterns={item['formula_pattern_count']} "
                f"validations={item['validation_count']}"
            )
        except Exception as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            print(f"ERROR template {errors[-1]}")
    if errors:
        raise RuntimeError("模板结构抽取失败：" + "；".join(errors))
    if len(catalog) != len(TEMPLATE_FILE_IDS):
        raise RuntimeError(f"模板数量不完整：{len(catalog)} != {len(TEMPLATE_FILE_IDS)}")
    if check_only:
        print(f"CHECK templates={len(catalog)}")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    write_table(catalog, CATALOG_SCHEMA, output_dir / "template_catalog.parquet")
    write_table(records["fields"], FIELD_SCHEMA, output_dir / "template_fields.parquet")
    write_table(records["formulas"], FORMULA_SCHEMA, output_dir / "template_formulas.parquet")
    write_table(records["validations"], VALIDATION_SCHEMA, output_dir / "template_validations.parquet")
    summary = {
        "template_count": len(catalog),
        "field_count": len(records["fields"]),
        "constant_cell_count": sum(row["constant_cell_count"] for row in catalog),
        "formula_pattern_count": len(records["formulas"]),
        "formula_cell_count": sum(row["formula_cell_count"] for row in catalog),
        "validation_count": len(records["validations"]),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="快速抽取模板的字段、公式模式和数据校验")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    run(
        args.input_dir.resolve(),
        args.output_dir.resolve(),
        args.check_only,
        args.cache_dir.resolve(),
        not args.no_cache,
    )


if __name__ == "__main__":
    main()
