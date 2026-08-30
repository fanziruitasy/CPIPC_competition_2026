"""抽取人身险公司月度经营事实。"""

# ruff: noqa: D103, E501, RUF003

from __future__ import annotations

import argparse
import csv
import re
import shutil
import unicodedata
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from trusted_rag.ingestion.spreadsheets.cleaners.region_premium import (
    TITLE_PERIOD_RE,
    decimal_value,
    file_hash,
    get_cell,
    missing_periods,
    normalize_text,
    precision_unit,
    source_cell,
    write_quality,
    xls_sheets,
    xlsx_sheets,
)

TARGET_RE = re.compile(r"^\d+_(20\d{2})年0?(\d{1,2})月人身险公司经营情况表_.*\.(?:xls|xlsx)$", re.I)
OUTPUT_NAMES = {
    "facts": "life_insurance_facts.parquet",
    "mapping": "metric_mapping.csv",
    "quality": "quality_report.csv",
}

# 代码、规范名称、单位、期间口径、顺序、来源别名。
METRICS = [
    ("original_premium_income", "原保险保费收入", "CNY_100M", "YTD", 10, ["原保险保费收入"]),
    (
        "policyholder_investment_new_contribution",
        "保户投资款新增交费",
        "CNY_100M",
        "YTD",
        20,
        ["保户投资款新增交费"],
    ),
    (
        "unit_linked_account_new_contribution",
        "投连险独立账户新增交费",
        "CNY_100M",
        "YTD",
        30,
        ["投连险独立账户新增交费"],
    ),
    (
        "claims_paid",
        "赔付支出",
        "CNY_100M",
        "YTD",
        40,
        ["原保险赔付支出", "赔付支出", "赔款与给付支出"],
    ),
    ("sum_insured", "保险金额", "CNY_100M", "YTD", 50, ["保险金额"]),
    ("new_sum_insured", "新增保险金额", "CNY_100M", "YTD", 60, ["新增保险金额"]),
    ("policy_count", "保单件数", "TEN_THOUSAND_POLICIES", "YTD", 70, ["保单件数"]),
    (
        "new_policy_count",
        "新增保单件数",
        "TEN_THOUSAND_POLICIES",
        "YTD",
        80,
        ["新增保单件数"],
    ),
    ("total_assets", "总资产", "CNY_100M", "POINT_IN_TIME", 90, ["资产总额", "总资产"]),
]

# 代码、规范名称、顺序、来源别名。
PRODUCT_LINES = [
    ("life", "寿险", 10, ["其中：寿险", "寿险"]),
    ("ordinary_life", "普通寿险", 11, ["其中：普通寿险", "普通寿险"]),
    ("accident", "意外险", 20, ["其中：意外险", "意外险"]),
    ("health", "健康险", 30, ["其中：健康险", "健康险"]),
]

EXPECTED_FACTS = {
    "S1_LEGACY_LABELS": 17,
    "S2_MODERN_LABELS": 17,
    "S3_2023_AGGREGATED": 10,
    "S4_2024_2025_NO_POLICY_COUNT": 9,
    "S5_2026_ACCOUNTING": 9,
}

BASE_EXPECTED_KEYS = {
    ("original_premium_income", "all"),
    ("original_premium_income", "life"),
    ("original_premium_income", "accident"),
    ("original_premium_income", "health"),
    ("policyholder_investment_new_contribution", "all"),
    ("unit_linked_account_new_contribution", "all"),
    ("claims_paid", "all"),
    ("total_assets", "all"),
}
EXPECTED_KEYS = {
    "S1_LEGACY_LABELS": BASE_EXPECTED_KEYS
    | {("sum_insured", product) for product in ("all", "life", "accident", "health")}
    | {("policy_count", product) for product in ("all", "life", "ordinary_life", "accident", "health")},
    "S2_MODERN_LABELS": BASE_EXPECTED_KEYS
    | {("sum_insured", product) for product in ("all", "life", "accident", "health")}
    | {("policy_count", product) for product in ("all", "life", "ordinary_life", "accident", "health")},
    "S3_2023_AGGREGATED": BASE_EXPECTED_KEYS | {("new_sum_insured", "all"), ("new_policy_count", "all")},
    "S4_2024_2025_NO_POLICY_COUNT": BASE_EXPECTED_KEYS | {("new_sum_insured", "all")},
    "S5_2026_ACCOUNTING": BASE_EXPECTED_KEYS | {("new_sum_insured", "all")},
}


def label_key(value: object) -> str:
    return normalize_text(value).replace("：", ":")


def exact_decimal_value(value: object) -> tuple[Decimal | None, str | None]:
    number, flag = decimal_value(value)
    if flag != "PRECISION_ROUNDED_6DP":
        return number, flag
    text = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    number = Decimal(text)
    return number, "NEGATIVE_VALUE" if number < 0 else None


METRIC_BY_ALIAS = {
    label_key(alias): {
        "metric_code": code,
        "metric_name": name,
        "unit": unit,
        "period_basis": basis,
        "metric_order": order,
    }
    for code, name, unit, basis, order, aliases in METRICS
    for alias in aliases
}
PRODUCT_BY_ALIAS = {
    label_key(alias): {
        "product_line": code,
        "product_line_name": name,
        "product_order": order,
    }
    for code, name, order, aliases in PRODUCT_LINES
    for alias in aliases
}


def discover_files(input_dir: Path) -> list[tuple[Path, int, int]]:
    found = []
    for path in input_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("~$") or path.suffix.lower() not in {".xls", ".xlsx"}:
            continue
        match = TARGET_RE.match(path.name)
        if match and 1 <= int(match.group(2)) <= 12:
            found.append((path, int(match.group(1)), int(match.group(2))))
    return sorted(found, key=lambda item: (item[1], item[2], item[0].name))


def load_source(path: Path) -> tuple[list[dict], str, bytes]:
    if path.suffix.lower() == ".xlsx":
        return xlsx_sheets(path), file_hash(path), b""

    # ACE 可能拒绝读取被 Excel 占用的 XLS，私有副本可避免文件锁冲突。
    with TemporaryDirectory() as directory:
        copy = Path(directory) / "source.xls"
        shutil.copy2(path, copy)
        # ADO 只能提供 XLS 缓存值；原始公式需要由转换后的 XLSX 链路保留。
        return xls_sheets(copy), file_hash(copy), copy.read_bytes()


def find_table(sheets: list[dict]) -> tuple[dict, int]:
    for sheet in sheets:
        for row_number, row in enumerate(sheet["values"], start=1):
            if any(label_key(value) == "项目" for value in row):
                later = sheet["values"][row_number : row_number + 5]
                if any(label_key(value) == "原保险保费收入" for later_row in later for value in later_row):
                    return sheet, row_number
    raise ValueError("未找到人身险经营情况表头")


def first_number_to_right(sheet: dict, row: int, label_col: int) -> tuple[int, Decimal | None, str | None]:
    width = len(sheet["values"][row - 1])
    for col in range(label_col + 1, width + 1):
        value = get_cell(sheet, row, col)
        number, flag = exact_decimal_value(value)
        formula = get_cell(sheet, row, col, "formulas")
        if number is not None or formula:
            return col, number, flag
    return label_col + 1, None, "NON_NUMERIC_VALUE"


def note_text(sheet: dict, start_row: int) -> tuple[str, bool]:
    notes = []
    helper_labels = {"保费收入同比", "保险金额同比", "赔付支出同比"}
    possibly_truncated = False
    for row in sheet["values"][start_row - 1 :]:
        strings = [str(value).strip() for value in row if isinstance(value, str) and value.strip()]
        if not strings or label_key(strings[0]) in helper_labels:
            continue
        text = strings[0]
        possibly_truncated |= len(text) == 255
        if text not in notes:
            notes.append(text)
    return "\n".join(notes), possibly_truncated


def schema_version(facts: list[dict]) -> str:
    raw_labels = {label_key(row["metric_name_raw"]) for row in facts if row["product_line"] == "all"}
    codes = {row["metric_code"] for row in facts}
    if "赔款与给付支出" in raw_labels:
        return "S5_2026_ACCOUNTING"
    if "new_policy_count" in codes:
        return "S3_2023_AGGREGATED"
    if "new_sum_insured" in codes:
        return "S4_2024_2025_NO_POLICY_COUNT"
    if "原保险赔付支出" in raw_labels or "资产总额" in raw_labels:
        return "S1_LEGACY_LABELS"
    return "S2_MODERN_LABELS"


def binary_contains(data: bytes, text: str) -> bool:
    return any(text.encode(encoding) in data for encoding in ("utf-16le", "utf-16be", "utf-8", "gb18030"))


def scope_version(year: int, month: int, footnotes: str, source_bytes: bytes = b"") -> str:
    if (year, month) < (2021, 6):
        return "PRE_2021_06"
    exclusions = ("不包含", "暂不包含", "不包括", "暂不包括")
    disclosed_in_text = "风险处置" in footnotes and any(word in footnotes for word in exclusions)
    disclosed_in_binary = binary_contains(source_bytes, "风险处置") and any(binary_contains(source_bytes, word) for word in exclusions)
    if disclosed_in_text or disclosed_in_binary:
        return "DISCLOSED_RISK_DISPOSAL_EXCLUSION"
    return "UNSTATED"


def identity_check(facts: list[dict]) -> tuple[Decimal | None, Decimal | None]:
    by_key = {(row["metric_code"], row["product_line"]): row["value"] for row in facts}
    diffs = []
    for metric_code in ("original_premium_income", "sum_insured", "policy_count"):
        total = by_key.get((metric_code, "all"))
        parts = [by_key.get((metric_code, product)) for product in ("life", "accident", "health")]
        if total is not None and all(value is not None for value in parts):
            diffs.append(abs(total - sum(parts, Decimal(0))))
    values = [row["value"] for row in facts if row["value"] is not None]
    tolerance = Decimal("2") if precision_unit(values) == 1 else Decimal("0.02")
    return max(diffs, default=None), tolerance


def parse_file(path: Path, year: int, month: int, input_dir: Path) -> tuple[list[dict], dict]:
    sheets, sha256, source_bytes = load_source(path)
    sheet, header_row = find_table(sheets)
    source_format = path.suffix.lower().lstrip(".")
    release_id = f"{year:04d}-{month:02d}"
    relative_path = path.relative_to(input_dir).as_posix()
    current_metric = None
    current_metric_raw = ""
    facts = []
    issues = []
    note_row = len(sheet["values"]) + 1
    unknown_numeric_rows = 0
    formula_cache_missing = 0

    for row_number in range(header_row + 1, len(sheet["values"]) + 1):
        row = sheet["values"][row_number - 1]
        nonempty_text = [str(value).strip() for value in row if isinstance(value, str) and value.strip()]
        if any(label_key(value).startswith("注") for value in nonempty_text):
            note_row = row_number
            break

        recognized = None
        for col, raw_label in enumerate(row, start=1):
            key = label_key(raw_label)
            if key in METRIC_BY_ALIAS:
                recognized = (col, raw_label, "metric", METRIC_BY_ALIAS[key])
                break
            if key in PRODUCT_BY_ALIAS:
                recognized = (col, raw_label, "product", PRODUCT_BY_ALIAS[key])
                break

        if not recognized:
            if any(exact_decimal_value(value)[0] is not None for value in row):
                unknown_numeric_rows += 1
            continue

        label_col, raw_label, kind, info = recognized
        if kind == "metric":
            current_metric = info
            current_metric_raw = "" if raw_label is None else str(raw_label)
            product = {"product_line": "all", "product_line_name": "合计", "product_order": 0}
        else:
            if current_metric is None:
                issues.append(f"{row_number}:子项没有父指标")
                continue
            product = info

        value_col, value, value_flag = first_number_to_right(sheet, row_number, label_col)
        formula = get_cell(sheet, row_number, value_col, "formulas")
        formula_cache_missing_flag = bool(formula and value is None)
        if formula_cache_missing_flag:
            formula_cache_missing += 1
        flags = [flag for flag in (value_flag,) if flag]
        if formula_cache_missing_flag:
            flags.append("FORMULA_CACHE_MISSING")
        period_end = date(year, month, monthrange(year, month)[1])
        facts.append(
            {
                "release_id": release_id,
                "period_start": period_end if current_metric["period_basis"] == "POINT_IN_TIME" else date(year, 1, 1),
                "period_end": period_end,
                "period_basis": current_metric["period_basis"],
                "entity_code": "CN_LIFE_INSURANCE_INDUSTRY_TOTAL",
                "entity_name": "全国人身险行业汇总",
                "entity_type": "industry_aggregate",
                "metric_code": current_metric["metric_code"],
                "metric_name": current_metric["metric_name"],
                "metric_name_raw": current_metric_raw,
                "metric_order": current_metric["metric_order"],
                "parent_metric_code": current_metric["metric_code"] if product["product_line"] != "all" else None,
                "product_line": product["product_line"],
                "product_line_name": product["product_line_name"],
                "product_line_raw": "" if product["product_line"] == "all" or raw_label is None else str(raw_label),
                "product_order": product["product_order"],
                "metric_path": f"{current_metric['metric_code']}.{product['product_line']}",
                "value": value,
                "value_status": "missing" if value is None else "reported_zero" if value == 0 else "reported",
                "unit": current_metric["unit"],
                "schema_version": "",
                "scope_version": "",
                "accounting_basis_version": "",
                "source_file": relative_path,
                "source_file_hash": sha256,
                "source_format": source_format,
                "source_origin": "local_attachment",
                "source_sheet": sheet["name"],
                "source_cell": source_cell(row_number, value_col),
                "formula_raw": formula,
                "number_format": get_cell(sheet, row_number, value_col, "formats"),
                "footnotes": "",
                "quality_flags": "|".join(flags),
            }
        )

    footnotes, note_maybe_truncated = note_text(sheet, note_row)
    schema = schema_version(facts)
    scope = scope_version(year, month, footnotes, source_bytes)
    accounting = "2026_FINANCIAL_INSTRUMENTS" if "金融工具会计准则" in footnotes or schema == "S5_2026_ACCOUNTING" else "2009_INSURANCE_ACCOUNTING"
    for fact in facts:
        fact["schema_version"] = schema
        fact["scope_version"] = scope
        fact["accounting_basis_version"] = accounting
        fact["footnotes"] = footnotes

    pre_header = [
        str(value).strip()
        for row in sheet["values"][:header_row]
        for value in row
        if value is not None and str(value).strip()
    ]
    title_periods = {(int(y), int(m)) for text in pre_header for y, m in TITLE_PERIOD_RE.findall(text)}
    unit_header = next((text for text in pre_header if "单位" in text), "")
    identity_diff, identity_tolerance = identity_check(facts)
    expected_count = EXPECTED_FACTS.get(schema, 0)

    errors = list(issues)
    warnings = []
    missing_values = sum(row["value"] is None for row in facts)
    actual_keys = {(row["metric_code"], row["product_line"]) for row in facts}
    expected_keys = EXPECTED_KEYS.get(schema, set())
    if len(facts) != expected_count:
        errors.append(f"fact_count={len(facts)} expected={expected_count}")
    if actual_keys != expected_keys:
        errors.append(f"missing_keys={sorted(expected_keys - actual_keys)} extra_keys={sorted(actual_keys - expected_keys)}")
    if missing_values:
        errors.append(f"missing_values={missing_values}")
    if formula_cache_missing:
        errors.append(f"formula_cache_missing={formula_cache_missing}")
    if unknown_numeric_rows:
        errors.append(f"unknown_numeric_rows={unknown_numeric_rows}")
    negative_values = sum("NEGATIVE_VALUE" in row["quality_flags"] for row in facts)
    if negative_values:
        errors.append(f"negative_values={negative_values}")
    if title_periods and (year, month) not in title_periods:
        errors.append(f"title_periods={sorted(title_periods)}")
    if not title_periods:
        warnings.append("标题未写年月，期间取自规范文件名")
    expected_unit_text = "亿元、万件" if schema in {"S1_LEGACY_LABELS", "S2_MODERN_LABELS", "S3_2023_AGGREGATED"} else "亿元"
    if expected_unit_text not in normalize_text(unit_header):
        errors.append(f"unit_header={unit_header!r} expected={expected_unit_text}")
    if identity_diff is not None and identity_diff > identity_tolerance:
        errors.append(f"identity_max_abs_diff={identity_diff} tolerance={identity_tolerance}")
    if note_maybe_truncated and source_format == "xls":
        warnings.append("xls脚注可能受ADO文本长度限制，原文件已保留")
    if (year, month) == (2021, 6):
        warnings.append("风险处置机构剔除口径自本期发生变化")
    if (year, month) == (2026, 1):
        warnings.append("赔付指标及会计基础自本期发生变化")

    severity = "ERROR" if errors else "WARN" if warnings else "PASS"
    status = "PARSE_ERROR" if errors else "CHECK_WARNING" if warnings else "CLEAN"
    quality = {
        "record_type": "FILE",
        "severity": severity,
        "period": release_id,
        "source_file": relative_path,
        "source_sheet": sheet["name"],
        "schema_version": schema,
        "scope_version": scope,
        "accounting_basis_version": accounting,
        "status": status,
        "fact_count": len(facts),
        "expected_fact_count": expected_count,
        "missing_value_count": missing_values,
        "formula_cache_missing_count": formula_cache_missing,
        "unknown_numeric_row_count": unknown_numeric_rows,
        "identity_max_abs_diff": identity_diff,
        "identity_tolerance": identity_tolerance,
        "cumulative_decrease_count": 0,
        "detail": "; ".join(errors + warnings),
    }
    return facts, quality


def apply_dataset_checks(facts: list[dict], quality: list[dict], periods: set[tuple[int, int]]) -> list[str]:
    keys = [(row["release_id"], row["metric_code"], row["product_line"]) for row in facts]
    duplicate_count = len(keys) - len(set(keys))
    if duplicate_count:
        quality.append(
            {
                "record_type": "DATASET",
                "severity": "ERROR",
                "period": "",
                "source_file": "",
                "source_sheet": "",
                "schema_version": "",
                "scope_version": "",
                "accounting_basis_version": "",
                "status": "DUPLICATE_KEY",
                "fact_count": len(facts),
                "expected_fact_count": "",
                "missing_value_count": "",
                "formula_cache_missing_count": "",
                "unknown_numeric_row_count": "",
                "identity_max_abs_diff": "",
                "identity_tolerance": "",
                "cumulative_decrease_count": "",
                "detail": f"duplicate_fact_keys={duplicate_count}",
            }
        )

    hashes_by_period = {row["release_id"]: row["source_file_hash"] for row in facts}
    periods_by_hash = defaultdict(list)
    for period, sha256 in hashes_by_period.items():
        periods_by_hash[sha256].append(period)
    duplicate_hashes = [sorted(group) for group in periods_by_hash.values() if len(group) > 1]
    if duplicate_hashes:
        quality.append(
            {
                "record_type": "DATASET",
                "severity": "ERROR",
                "period": "",
                "source_file": "",
                "source_sheet": "",
                "schema_version": "",
                "scope_version": "",
                "accounting_basis_version": "",
                "status": "DUPLICATE_SOURCE_HASH",
                "fact_count": len(facts),
                "expected_fact_count": "",
                "missing_value_count": "",
                "formula_cache_missing_count": "",
                "unknown_numeric_row_count": "",
                "identity_max_abs_diff": "",
                "identity_tolerance": "",
                "cumulative_decrease_count": "",
                "detail": f"duplicate_source_hash_periods={duplicate_hashes}",
            }
        )

    file_quality = {row["period"]: row for row in quality if row["record_type"] == "FILE"}
    previous: dict[tuple, tuple[str, Decimal]] = {}
    for row in sorted(facts, key=lambda item: (item["period_end"], item["metric_order"], item["product_order"])):
        if row["value"] is None or row["period_basis"] != "YTD":
            continue
        key = (
            row["period_end"].year,
            row["metric_code"],
            row["product_line"],
            row["scope_version"],
            row["accounting_basis_version"],
        )
        if key in previous and row["value"] < previous[key][1]:
            qc = file_quality[row["release_id"]]
            qc["cumulative_decrease_count"] += 1
            if qc["severity"] == "PASS":
                qc["severity"], qc["status"] = "WARN", "CUMULATIVE_DECREASE"
            message = f"累计值下降:{row['metric_path']} {previous[key][0]}={previous[key][1]} -> {row['release_id']}={row['value']}"
            qc["detail"] = "; ".join(filter(None, [qc["detail"], message]))
        previous[key] = (row["release_id"], row["value"])

    gaps = missing_periods(periods)
    for period in gaps:
        quality.append(
            {
                "record_type": "PERIOD",
                "severity": "WARN",
                "period": period,
                "source_file": "",
                "source_sheet": "",
                "schema_version": "",
                "scope_version": "",
                "accounting_basis_version": "",
                "status": "MISSING_PERIOD",
                "fact_count": "",
                "expected_fact_count": "",
                "missing_value_count": "",
                "formula_cache_missing_count": "",
                "unknown_numeric_row_count": "",
                "identity_max_abs_diff": "",
                "identity_tolerance": "",
                "cumulative_decrease_count": "",
                "detail": "源文件缺失，不插值、不推造单月值",
            }
        )
    return gaps


FACT_SCHEMA = pa.schema(
    [
        ("release_id", pa.string()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("period_basis", pa.string()),
        ("entity_code", pa.string()),
        ("entity_name", pa.string()),
        ("entity_type", pa.string()),
        ("metric_code", pa.string()),
        ("metric_name", pa.string()),
        ("metric_name_raw", pa.string()),
        ("metric_order", pa.int8()),
        ("parent_metric_code", pa.string()),
        ("product_line", pa.string()),
        ("product_line_name", pa.string()),
        ("product_line_raw", pa.string()),
        ("product_order", pa.int8()),
        ("metric_path", pa.string()),
        ("value", pa.decimal128(38, 18)),
        ("value_status", pa.string()),
        ("unit", pa.string()),
        ("schema_version", pa.string()),
        ("scope_version", pa.string()),
        ("accounting_basis_version", pa.string()),
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("source_origin", pa.string()),
        ("source_sheet", pa.string()),
        ("source_cell", pa.string()),
        ("formula_raw", pa.string()),
        ("number_format", pa.string()),
        ("footnotes", pa.string()),
        ("quality_flags", pa.string()),
    ]
)


def write_mapping(path: Path) -> None:
    fields = ["mapping_type", "alias_raw", "alias_normalized", "canonical_code", "canonical_name", "unit", "period_basis"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for code, name, unit, basis, _, aliases in METRICS:
            for alias in aliases:
                writer.writerow(
                    {
                        "mapping_type": "metric",
                        "alias_raw": alias,
                        "alias_normalized": label_key(alias),
                        "canonical_code": code,
                        "canonical_name": name,
                        "unit": unit,
                        "period_basis": basis,
                    }
                )
        for code, name, _, aliases in PRODUCT_LINES:
            for alias in aliases:
                writer.writerow(
                    {
                        "mapping_type": "product_line",
                        "alias_raw": alias,
                        "alias_normalized": label_key(alias),
                        "canonical_code": code,
                        "canonical_name": name,
                        "unit": "",
                        "period_basis": "",
                    }
                )


def self_check() -> None:
    assert METRIC_BY_ALIAS[label_key("资产总额")]["metric_code"] == "total_assets"
    assert METRIC_BY_ALIAS[label_key("赔款与给付支出")]["metric_code"] == "claims_paid"
    assert PRODUCT_BY_ALIAS[label_key("其中：普通寿险")]["product_line"] == "ordinary_life"
    assert exact_decimal_value(17708.2965292032)[0] == Decimal("17708.2965292032")
    assert scope_version(2024, 9, "因部分机构正在进行风险处置，暂不包括这部分机构") == "DISCLOSED_RISK_DISPOSAL_EXCLUSION"
    assert missing_periods({(2025, 5), (2025, 7)}) == ["2025-06"]
    assert [note_text({"values": [["x" * length]]}, 1)[1] for length in (254, 255, 256)] == [False, True, False]


def run(input_dir: Path, output_dir: Path, check_only: bool) -> None:
    self_check()
    files = discover_files(input_dir)
    if not files:
        raise SystemExit("未找到人身险公司经营情况表")
    periods = {(year, month) for _, year, month in files}
    if len(periods) != len(files):
        raise SystemExit("同一月份发现多个源文件，请先确认版本")

    facts, quality = [], []
    for path, year, month in files:
        relative_path = path.relative_to(input_dir).as_posix()
        try:
            file_facts, file_quality = parse_file(path, year, month, input_dir)
            facts.extend(file_facts)
            quality.append(file_quality)
            print(f"{file_quality['severity']} {year:04d}-{month:02d} {path.name}: {len(file_facts)} facts")
        except Exception as exc:
            quality.append(
                {
                    "record_type": "FILE",
                    "severity": "ERROR",
                    "period": f"{year:04d}-{month:02d}",
                    "source_file": relative_path,
                    "source_sheet": "",
                    "schema_version": "",
                    "scope_version": "",
                    "accounting_basis_version": "",
                    "status": "PARSE_ERROR",
                    "fact_count": 0,
                    "expected_fact_count": "",
                    "missing_value_count": "",
                    "formula_cache_missing_count": "",
                    "unknown_numeric_row_count": "",
                    "identity_max_abs_diff": "",
                    "identity_tolerance": "",
                    "cumulative_decrease_count": "",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"ERROR {year:04d}-{month:02d} {path.name}: {exc}")

    facts.sort(key=lambda item: (item["period_end"], item["metric_order"], item["product_order"]))
    gaps = apply_dataset_checks(facts, quality, periods)
    blocking_errors = [row for row in quality if row["severity"] == "ERROR"]

    if check_only:
        print(f"CHECK COMPLETED: files={len(files)}, facts={len(facts)}, missing_periods={gaps}, blocking_errors={len(blocking_errors)}")
        if blocking_errors:
            raise SystemExit(1)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    write_mapping(output_dir / OUTPUT_NAMES["mapping"])
    write_quality(output_dir / OUTPUT_NAMES["quality"], quality)
    if blocking_errors:
        print(f"BLOCKED: {len(blocking_errors)} quality errors; mapping and quality report were written, facts were not published")
        raise SystemExit(1)
    pq.write_table(pa.Table.from_pylist(facts, schema=FACT_SCHEMA), output_dir / OUTPUT_NAMES["facts"], compression="zstd")
    print(f"WROTE files={len(files)}, facts={len(facts)} to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="复用地区保费清洗底层，将人身险公司经营情况表清洗为可追溯长表")
    parser.add_argument("--input-dir", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "output_life_insurance_clean")
    parser.add_argument("--check-only", action="store_true", help="全量解析和校验，不写结果文件")
    args = parser.parse_args()
    run(args.input_dir.resolve(), args.output_dir.resolve(), args.check_only)


if __name__ == "__main__":
    main()
