from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from clean_region_premium import (
    file_hash,
    format_excel_value,
    get_cell,
    normalize_text,
    source_cell,
    xls_sheets,
    xlsx_sheets,
)


OUTPUT_NAMES = {
    "facts": "regulatory_facts.parquet",
    "mapping": "mapping.csv",
    "quality": "quality_report.csv",
    "schedule": "release_schedule.parquet",
    "definitions": "metric_definitions.parquet",
    "scopes": "institution_scopes.parquet",
}

QUARTERS = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def label_key(value: object) -> str:
    text = normalize_text(value).replace("*", "").replace("＊", "")
    text = re.sub(r"^\d+[.、]", "", text)
    text = re.sub(r"^（[一二三四五六七八九十]+）", "", text)
    while text.startswith(("其中：", "其中:")):
        text = text[3:]
    return text


ENTITIES = [
    (
        "banking_financial_institutions",
        "银行业金融机构",
        ["银行业金融机构", "银行业金融机构合计"],
    ),
    ("commercial_banks", "商业银行合计", ["商业银行", "商业银行合计"]),
    ("large_commercial_banks", "大型商业银行", ["大型商业银行"]),
    ("joint_stock_commercial_banks", "股份制商业银行", ["股份制商业银行"]),
    ("city_commercial_banks", "城市商业银行", ["城市商业银行"]),
    ("private_banks", "民营银行", ["民营银行"]),
    ("rural_financial_institutions", "农村金融机构", ["农村金融机构"]),
    ("rural_commercial_banks", "农村商业银行", ["农村商业银行"]),
    ("foreign_banks", "外资银行", ["外资银行"]),
    (
        "other_financial_institutions",
        "其他类金融机构",
        ["其他类金融机构", "其他类银行业金融机构"],
    ),
    ("insurance_companies", "保险公司", ["保险公司", "保险机构"]),
    ("property_insurers", "财产险公司", ["财产险公司", "财产保险公司"]),
    ("life_insurers", "人身险公司", ["人身险公司", "人身保险公司"]),
    ("reinsurers", "再保险公司", ["再保险公司"]),
    ("risk_rating_a", "A类公司", ["A类公司"]),
    ("risk_rating_b", "B类公司", ["B类公司"]),
    ("risk_rating_c", "C类公司", ["C类公司"]),
    ("risk_rating_d", "D类公司", ["D类公司"]),
]

ENTITY_BY_ALIAS = {
    label_key(alias): {"entity_code": code, "entity_name": name}
    for code, name, aliases in ENTITIES
    for alias in aliases
}


REGULATORY_METRICS = [
    ("normal_loan_balance", "正常类贷款", ["正常类贷款"]),
    ("special_mention_loan_balance", "关注类贷款", ["关注类贷款"]),
    ("nonperforming_loan_balance", "不良贷款余额", ["不良贷款余额"]),
    ("substandard_loan_balance", "次级类贷款余额", ["次级类贷款", "次级类贷款余额"]),
    ("doubtful_loan_balance", "可疑类贷款余额", ["可疑类贷款", "可疑类贷款余额"]),
    ("loss_loan_balance", "损失类贷款余额", ["损失类贷款", "损失类贷款余额"]),
    ("normal_loan_ratio", "正常类贷款占比", ["正常类贷款占比"]),
    ("special_mention_loan_ratio", "关注类贷款占比", ["关注类贷款占比"]),
    ("nonperforming_loan_ratio", "不良贷款率", ["不良贷款率"]),
    ("substandard_loan_ratio", "次级类贷款率", ["次级类贷款率"]),
    ("doubtful_loan_ratio", "可疑类贷款率", ["可疑类贷款率"]),
    ("loss_loan_ratio", "损失类贷款率", ["损失类贷款率"]),
    ("loan_loss_provision", "贷款损失准备", ["贷款损失准备"]),
    ("provision_coverage_ratio", "拨备覆盖率", ["拨备覆盖率"]),
    ("loan_provision_ratio", "贷款拨备率", ["贷款拨备率"]),
    ("liquidity_ratio", "流动性比例", ["流动性比例"]),
    (
        "loan_to_deposit_ratio_rmb",
        "存贷比（人民币）",
        ["存贷比（人民币）", "存贷比(人民币)"],
    ),
    ("rmb_excess_reserve_ratio", "人民币超额备付金率", ["人民币超额备付金率"]),
    ("liquidity_coverage_ratio", "流动性覆盖率", ["流动性覆盖率"]),
    ("net_stable_funding_ratio", "净稳定资金比例", ["净稳定资金比例"]),
    (
        "net_profit_ytd",
        "净利润（本年累计）",
        ["净利润", "净利润（本年累计）", "净利润(本年累计)"],
    ),
    ("return_on_assets", "资产利润率", ["资产利润率"]),
    ("return_on_equity", "资本利润率", ["资本利润率"]),
    ("net_interest_margin", "净息差", ["净息差"]),
    ("non_interest_income_ratio", "非利息收入占比", ["非利息收入占比"]),
    ("cost_income_ratio", "成本收入比", ["成本收入比"]),
    ("core_tier1_capital_net", "核心一级资本净额", ["核心一级资本净额"]),
    ("tier1_capital_net", "一级资本净额", ["一级资本净额"]),
    ("total_capital_net", "资本净额", ["资本净额"]),
    ("credit_rwa", "信用风险加权资产", ["信用风险加权资产"]),
    ("market_rwa", "市场风险加权资产", ["市场风险加权资产"]),
    ("operational_rwa", "操作风险加权资产", ["操作风险加权资产"]),
    (
        "rwa_after_floor",
        "应用资本底线后的风险加权资产合计",
        ["应用资本底线后的风险加权资产合计"],
    ),
    ("core_tier1_capital_adequacy_ratio", "核心一级资本充足率", ["核心一级资本充足率"]),
    ("tier1_capital_adequacy_ratio", "一级资本充足率", ["一级资本充足率"]),
    ("capital_adequacy_ratio", "资本充足率", ["资本充足率"]),
    ("leverage_ratio", "杠杆率", ["杠杆率"]),
    ("cumulative_fx_exposure_ratio", "累计外汇敞口头寸比例", ["累计外汇敞口头寸比例"]),
]

REGULATORY_METRIC_BY_ALIAS = {
    label_key(alias): {"metric_code": code, "metric_name": name}
    for code, name, aliases in REGULATORY_METRICS
    for alias in aliases
}

CAPITAL_METRICS = {
    "core_tier1_capital_net",
    "tier1_capital_net",
    "total_capital_net",
    "credit_rwa",
    "market_rwa",
    "operational_rwa",
    "rwa_after_floor",
    "core_tier1_capital_adequacy_ratio",
    "tier1_capital_adequacy_ratio",
    "capital_adequacy_ratio",
}

FUND_BASES = {
    "资金运用余额": ("funds_investment", "资金运用"),
    "银行存款": ("bank_deposit", "银行存款"),
    "债券": ("bonds", "债券"),
    "股票": ("stocks", "股票"),
    "证券投资基金": ("securities_investment_funds", "证券投资基金"),
    "长期股权投资": ("long_term_equity_investment", "长期股权投资"),
}

FUND_YIELDS = {
    "财务收益率年化": ("annualized_financial_investment_yield", "年化财务投资收益率"),
    "年化财务收益率": ("annualized_financial_investment_yield", "年化财务投资收益率"),
    "年化财务投资收益率": (
        "annualized_financial_investment_yield",
        "年化财务投资收益率",
    ),
    "综合收益率年化": (
        "annualized_comprehensive_investment_yield",
        "年化综合投资收益率",
    ),
    "年化综合收益率": (
        "annualized_comprehensive_investment_yield",
        "年化综合投资收益率",
    ),
    "年化综合投资收益率": (
        "annualized_comprehensive_investment_yield",
        "年化综合投资收益率",
    ),
}


FACT_SCHEMA = pa.schema(
    [
        ("dataset_id", pa.string()),
        ("release_id", pa.string()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("frequency", pa.string()),
        ("period_basis", pa.string()),
        ("scope", pa.string()),
        ("entity_code", pa.string()),
        ("entity_name", pa.string()),
        ("entity_name_raw", pa.string()),
        ("metric_code", pa.string()),
        ("metric_name", pa.string()),
        ("metric_name_raw", pa.string()),
        ("metric_group", pa.string()),
        ("measure_type", pa.string()),
        ("value", pa.decimal128(38, 6)),
        ("value_storage", pa.decimal128(38, 18)),
        ("value_display", pa.string()),
        ("value_decimal", pa.decimal128(38, 6)),
        ("value_raw", pa.string()),
        ("value_status", pa.string()),
        ("unit", pa.string()),
        ("unit_raw", pa.string()),
        ("schema_version", pa.string()),
        ("scope_version", pa.string()),
        ("comparability_flag", pa.string()),
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_format", pa.string()),
        ("source_origin", pa.string()),
        ("source_sheet", pa.string()),
        ("source_cell", pa.string()),
        ("formula_raw", pa.string()),
        ("number_format", pa.string()),
        ("calculation_basis", pa.string()),
        ("footnotes", pa.string()),
        ("quality_flags", pa.string()),
    ]
)

SCHEDULE_SCHEMA = pa.schema(
    [
        ("release_year", pa.int16()),
        ("row_order", pa.int16()),
        ("frequency", pa.string()),
        ("release_timing", pa.string()),
        ("institution_scope", pa.string()),
        ("data_scope", pa.string()),
        ("indicator_names", pa.string()),
        ("notes", pa.string()),
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_sheet", pa.string()),
        ("source_cell", pa.string()),
    ]
)

DEFINITION_SCHEMA = pa.schema(
    [
        ("release_year", pa.int16()),
        ("sequence", pa.int16()),
        ("metric_name", pa.string()),
        ("definition", pa.string()),
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_sheet", pa.string()),
        ("source_cell", pa.string()),
    ]
)

SCOPE_SCHEMA = pa.schema(
    [
        ("release_year", pa.int16()),
        ("sequence", pa.int16()),
        ("institution_type", pa.string()),
        ("scope_definition", pa.string()),
        ("source_file", pa.string()),
        ("source_file_hash", pa.string()),
        ("source_sheet", pa.string()),
        ("source_cell", pa.string()),
    ]
)


def classify_file(path: Path) -> tuple[str, int] | None:
    match = re.match(r"^\d+_(20\d{2})年", path.name)
    if not match or path.suffix.lower() not in {".xls", ".xlsx"}:
        return None
    year = int(match.group(1))
    name = re.sub(r"\s+", "", path.name)
    if year < 2020:
        return (
            ("regulatory_release_schedule", year)
            if any(
                token in name
                for token in ("监管统计信息发布日程", "机构范围", "指标解释")
            )
            else None
        )
    if "监管统计信息发布日程表" in name:
        return "regulatory_release_schedule", year
    if "银行业总资产、总负债（月度）" in name:
        return "bank_balance_monthly", year
    if re.search(r"[一二三四1234]季度保险(?:业|公司)资金运用情况表", name):
        return "insurance_funds_investment", year
    if "偿付能力" in name and ("状况表" in name or "情况表" in name):
        return "insurance_solvency", year
    if "普惠型小微企业贷款情况" in name:
        return "inclusive_small_business_loans", year
    if "普惠型涉农贷款情况" in name:
        return "inclusive_agricultural_loans", year
    if "保障性安居工程贷款情况" in name:
        return "affordable_housing_loans", year
    if "商业银行主要指标分机构类情况表" in name:
        return "commercial_bank_indicators_by_type", year
    if "商业银行主要监管指标情况表" in name:
        return "commercial_bank_regulatory_indicators", year
    if "总资产、总负债（季度）" in name:
        return "bank_balance_quarterly", year
    return None


def discover_files(input_dir: Path) -> list[tuple[Path, str, int]]:
    found = []
    for path in input_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        classified = classify_file(path)
        if classified:
            dataset_id, year = classified
            found.append((path, dataset_id, year))
    return sorted(found, key=lambda item: (item[2], item[1], item[0].name))


def period_from_token(year: int, value: object) -> tuple[date, date, str] | None:
    text = normalize_text(value)
    month_match = re.fullmatch(r"(\d{1,2})月", text)
    if month_match:
        month = int(month_match.group(1))
        if 1 <= month <= 12:
            return (
                date(year, month, 1),
                date(year, month, monthrange(year, month)[1]),
                "month",
            )
    quarter_match = re.fullmatch(r"([一二三四1234])季度(?:末)?", text)
    if quarter_match:
        quarter = QUARTERS[quarter_match.group(1)]
        start_month = quarter * 3 - 2
        end_month = quarter * 3
        return (
            date(year, start_month, 1),
            date(year, end_month, monthrange(year, end_month)[1]),
            "quarter",
        )
    return None


def quarter_from_filename(path: Path, year: int) -> tuple[int, date, date]:
    match = re.search(r"([一二三四1234])季度", normalize_text(path.name))
    if not match:
        raise ValueError("文件名中没有季度")
    quarter = QUARTERS[match.group(1)]
    start_month = quarter * 3 - 2
    end_month = quarter * 3
    return (
        quarter,
        date(year, start_month, 1),
        date(year, end_month, monthrange(year, end_month)[1]),
    )


def canonical_entity(value: object) -> dict | None:
    return ENTITY_BY_ALIAS.get(label_key(value))


def parse_decimal(
    value: object, quantum: Decimal | None = Decimal("0.000001")
) -> tuple[Decimal | None, str, list[str]]:
    if value is None or not str(value).strip():
        return None, "missing", []
    raw = unicodedata.normalize("NFKC", str(value)).strip()
    if raw in {"-", "--", "—", "/", "不适用"}:
        return None, "not_reported", []
    if raw.startswith("#"):
        return None, "parse_error", ["EXCEL_ERROR"]
    text = raw.replace(",", "").replace("%", "").replace("％", "").replace("*", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, "parse_error", ["NON_NUMERIC_VALUE"]
    rounded = number.quantize(quantum) if quantum is not None else number
    flags = ["PRECISION_ROUNDED_6DP"] if quantum is not None and rounded != number else []
    if rounded < 0:
        flags.append("NEGATIVE_VALUE")
    return rounded, "reported_zero" if rounded == 0 else "reported", flags


def regulatory_sheets(path: Path) -> list[dict]:
    if path.suffix.lower() == ".xlsx":
        return xlsx_sheets(path)
    return xls_sheets(path)


def extract_footnotes(sheet: dict) -> str:
    notes, started = [], False
    for row in sheet["values"]:
        values = [
            str(value).strip()
            for value in row[:10]
            if value is not None and str(value).strip()
        ]
        if not values:
            continue
        if not started and normalize_text(values[0]).startswith(("注", "备注", "说明")):
            started = True
        if started:
            for value in values:
                if value not in notes:
                    notes.append(value)
    return "\n".join(notes)


def select_sheet(sheets: list[dict], required: tuple[str, ...]) -> dict:
    for sheet in sheets:
        text = "|".join(
            normalize_text(value)
            for row in sheet["values"][:100]
            for value in row[:12]
            if value is not None
        )
        if all(item in text for item in required):
            return sheet
    raise ValueError(f"未找到包含表头 {required} 的工作表")


def metric_unit(metric_code: str, metric_name: str) -> str:
    if metric_code in {"net_profit_ytd"}:
        return "亿元"
    if any(
        token in metric_name for token in ("率", "占比", "净息差", "存贷比", "比例")
    ):
        return "%"
    return "亿元"


def make_context(
    path: Path,
    input_dir: Path,
    dataset_id: str,
    year: int,
    sheet: dict,
    release_id: str,
) -> dict:
    return {
        "dataset_id": dataset_id,
        "year": year,
        "release_id": release_id,
        "source_file": path.relative_to(input_dir).as_posix(),
        "source_file_hash": file_hash(path),
        "source_format": path.suffix.lower().lstrip("."),
        "source_sheet": sheet["name"],
        "sheet": sheet,
    }


def make_fact(
    context: dict,
    period: tuple[date, date, str],
    entity: dict,
    entity_raw: object,
    metric_code: str,
    metric_name: str,
    metric_raw: object,
    metric_group: str,
    measure_type: str,
    raw_value: object,
    unit: str,
    unit_raw: str,
    row: int,
    col: int,
    schema_version: str,
    scope: str,
    scope_version: str,
    footnotes: str,
    period_basis: str,
    comparability_flag: str = "",
    divisor: Decimal | None = None,
) -> dict:
    storage_based = context["source_format"] == "xlsx"
    number_format = get_cell(context["sheet"], row, col, "formats")
    display_raw = (
        format_excel_value(raw_value, number_format)
        if storage_based
        else get_cell(context["sheet"], row, col, "display_values")
    )
    # Percentages must use displayed percentage points across xls and xlsx.
    value_input = display_raw if unit == "%" else raw_value if storage_based else display_raw
    value, status, flags = parse_decimal(value_input)
    value_storage = parse_decimal(raw_value, Decimal("0.000000000000000001"))[0]
    if value is not None and divisor:
        value = (value / divisor).quantize(Decimal("0.000001"))
        flags.append(f"UNIT_CONVERTED_{unit_raw}_TO_{unit}")
    formula_raw = get_cell(context["sheet"], row, col, "formulas")
    value_display = "" if display_raw is None else str(display_raw).strip()
    return {
        "dataset_id": context["dataset_id"],
        "release_id": context["release_id"],
        "period_start": period[0],
        "period_end": period[1],
        "frequency": period[2],
        "period_basis": period_basis,
        "scope": scope,
        "entity_code": entity["entity_code"],
        "entity_name": entity["entity_name"],
        "entity_name_raw": "" if entity_raw is None else str(entity_raw).strip(),
        "metric_code": metric_code,
        "metric_name": metric_name,
        "metric_name_raw": "" if metric_raw is None else str(metric_raw).strip(),
        "metric_group": metric_group,
        "measure_type": measure_type,
        "value": value,
        "value_storage": value_storage,
        "value_display": value_display,
        "value_decimal": value,
        "value_raw": value_display,
        "value_status": status,
        "unit": unit,
        "unit_raw": unit_raw,
        "schema_version": schema_version,
        "scope_version": scope_version,
        "comparability_flag": comparability_flag,
        "source_file": context["source_file"],
        "source_file_hash": context["source_file_hash"],
        "source_format": context["source_format"],
        "source_origin": "local_attachment",
        "source_sheet": context["source_sheet"],
        "source_cell": source_cell(row, col),
        "formula_raw": formula_raw,
        "number_format": number_format,
        "calculation_basis": "STORAGE" if storage_based else "DISPLAYED",
        "footnotes": footnotes,
        "quality_flags": "|".join(dict.fromkeys(flags)),
    }


def nonempty(value: object) -> bool:
    return value is not None and bool(str(value).strip())


def sequence_number(value: object) -> int | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return int(number) if number == number.to_integral_value() else None


def parse_balance(
    path: Path, input_dir: Path, dataset_id: str, year: int
) -> list[dict]:
    sheet = select_sheet(regulatory_sheets(path), ("总资产", "总负债", "项目"))
    context = make_context(path, input_dir, dataset_id, year, sheet, str(year))
    footnotes = extract_footnotes(sheet)
    monthly = dataset_id.endswith("monthly")
    scope = "境内" if monthly else "法人"
    frequency = "month" if monthly else "quarter"
    schema_version = (
        "BANK_BALANCE_MONTHLY_V1" if monthly else "BANK_BALANCE_QUARTERLY_V1"
    )
    current_entity = None
    current_entity_raw = None
    current_base = None
    period_columns: dict[int, tuple[date, date, str]] = {}
    facts = []

    for row_number, row in enumerate(sheet["values"], start=1):
        raw_label = row[0] if row else None
        entity = canonical_entity(raw_label)
        if entity and label_key(raw_label) in {
            "银行业金融机构",
            "商业银行合计",
            "大型商业银行",
            "股份制商业银行",
            "城市商业银行",
            "农村金融机构",
            "其他类金融机构",
            "其他类银行业金融机构",
        }:
            current_entity, current_entity_raw = entity, raw_label
            current_base = None
            continue

        if label_key(raw_label) == "项目":
            period_columns = {
                col: parsed
                for col in range(2, len(row) + 1)
                if (parsed := period_from_token(year, get_cell(sheet, row_number, col)))
                and parsed[2] == frequency
            }
            continue
        if not current_entity or not period_columns:
            continue

        key = label_key(raw_label)
        if key == "总资产":
            current_base = ("total_assets", "总资产")
            metric_code, metric_name, measure_type, unit = (
                "total_assets",
                "总资产",
                "balance",
                "亿元",
            )
        elif key == "总负债":
            current_base = ("total_liabilities", "总负债")
            metric_code, metric_name, measure_type, unit = (
                "total_liabilities",
                "总负债",
                "balance",
                "亿元",
            )
        elif key == "比上年同期增长率" and current_base:
            metric_code = current_base[0] + "_yoy"
            metric_name = current_base[1] + "比上年同期增长率"
            measure_type, unit = "year_over_year", "%"
        elif key == "占银行业金融机构比例" and current_base:
            metric_code = current_base[0] + "_industry_share"
            metric_name = current_base[1] + "占银行业金融机构比例"
            measure_type, unit = "industry_share", "%"
        else:
            continue

        comparability = (
            "SCOPE_CHANGE_2023_WEALTH_MANAGEMENT_INCLUDED" if year >= 2023 else ""
        )
        for col, period in period_columns.items():
            raw_value = get_cell(sheet, row_number, col)
            if not nonempty(raw_value):
                continue
            facts.append(
                make_fact(
                    context,
                    period,
                    current_entity,
                    current_entity_raw,
                    metric_code,
                    metric_name,
                    raw_label,
                    "资产负债",
                    measure_type,
                    raw_value,
                    unit,
                    unit,
                    row_number,
                    col,
                    schema_version,
                    scope,
                    "DOMESTIC" if monthly else "LEGAL_ENTITY",
                    footnotes,
                    "month_end" if monthly else "quarter_end",
                    comparability,
                )
            )
    return facts


def parse_loans(path: Path, input_dir: Path, dataset_id: str, year: int) -> list[dict]:
    loan_types = {
        "inclusive_small_business_loans": (
            "普惠型小微企业贷款",
            "inclusive_small_business_loan_balance",
            "普惠型小微企业贷款余额",
            "INCLUSIVE_SMALL_BUSINESS_LOANS_V1",
        ),
        "inclusive_agricultural_loans": (
            "普惠型涉农贷款",
            "inclusive_agricultural_loan_balance",
            "普惠型涉农贷款余额",
            "INCLUSIVE_AGRICULTURAL_LOANS_V1",
        ),
        "affordable_housing_loans": (
            "保障性安居工程贷款",
            "affordable_housing_loan_balance",
            "保障性安居工程贷款余额",
            "AFFORDABLE_HOUSING_LOANS_V1",
        ),
    }
    required_label, metric_code, metric_name, schema_version = loan_types[dataset_id]
    required = (required_label, "项目")
    sheet = select_sheet(regulatory_sheets(path), required)
    context = make_context(path, input_dir, dataset_id, year, sheet, str(year))
    footnotes = extract_footnotes(sheet)
    header_row = None
    period_columns = {}
    for row_number, row in enumerate(sheet["values"], start=1):
        if label_key(row[0] if row else None) != "项目":
            continue
        columns = {
            col: parsed
            for col in range(2, len(row) + 1)
            if (parsed := period_from_token(year, get_cell(sheet, row_number, col)))
            and parsed[2] == "quarter"
        }
        if columns:
            header_row, period_columns = row_number, columns
            break
    if not header_row:
        raise ValueError("未找到贷款季度表头")

    facts = []
    for row_number in range(header_row + 1, len(sheet["values"]) + 1):
        raw_entity = get_cell(sheet, row_number, 1)
        if normalize_text(raw_entity).startswith(("注", "备注")):
            break
        entity = canonical_entity(raw_entity)
        if not entity:
            continue
        for col, period in period_columns.items():
            raw_value = get_cell(sheet, row_number, col)
            if not nonempty(raw_value):
                continue
            facts.append(
                make_fact(
                    context,
                    period,
                    entity,
                    raw_entity,
                    metric_code,
                    metric_name,
                    raw_entity,
                    "政策性贷款",
                    "balance",
                    raw_value,
                    "亿元",
                    "亿元",
                    row_number,
                    col,
                    schema_version,
                    "监管公布口径",
                    "PUBLISHED_SCOPE",
                    footnotes,
                    "quarter_end",
                )
            )
    return facts


def parse_indicators_by_type(
    path: Path, input_dir: Path, dataset_id: str, year: int
) -> list[dict]:
    sheet = select_sheet(
        regulatory_sheets(path), ("时间/指标", "大型商业银行", "不良贷款余额")
    )
    context = make_context(path, input_dir, dataset_id, year, sheet, str(year))
    footnotes = extract_footnotes(sheet)
    entity_columns = {}
    header_row = None
    for row_number, row in enumerate(sheet["values"], start=1):
        if label_key(row[0] if row else None) != "机构":
            continue
        for col in range(2, len(row) + 1):
            raw_entity = get_cell(sheet, row_number, col)
            entity = canonical_entity(raw_entity)
            if entity:
                entity_columns[col] = (entity, raw_entity)
        if entity_columns:
            header_row = row_number
            break
    if not header_row:
        raise ValueError("未找到分机构类表头")

    current_period = None
    facts = []
    for row_number in range(header_row + 1, len(sheet["values"]) + 1):
        raw_period = get_cell(sheet, row_number, 1)
        if normalize_text(raw_period).startswith(("注", "备注")):
            break
        if parsed := period_from_token(year, raw_period):
            current_period = parsed
        raw_metric = get_cell(sheet, row_number, 2)
        if not current_period or not nonempty(raw_metric):
            continue
        metric = REGULATORY_METRIC_BY_ALIAS.get(label_key(raw_metric))
        numeric_values = [get_cell(sheet, row_number, col) for col in entity_columns]
        if not metric:
            if any(nonempty(value) for value in numeric_values):
                raise ValueError(f"未知分机构指标：{raw_metric}")
            continue
        unit = metric_unit(metric["metric_code"], metric["metric_name"])
        comparability = (
            "CAPITAL_RULES_2024_BREAK"
            if year >= 2024 and metric["metric_code"] in CAPITAL_METRICS
            else ""
        )
        for col, (entity, raw_entity) in entity_columns.items():
            raw_value = get_cell(sheet, row_number, col)
            if not nonempty(raw_value):
                continue
            facts.append(
                make_fact(
                    context,
                    current_period,
                    entity,
                    raw_entity,
                    metric["metric_code"],
                    metric["metric_name"],
                    raw_metric,
                    "主要指标",
                    "value",
                    raw_value,
                    unit,
                    unit,
                    row_number,
                    col,
                    "BANK_INDICATORS_BY_TYPE_V2"
                    if year >= 2024
                    else "BANK_INDICATORS_BY_TYPE_V1",
                    "法人",
                    "LEGAL_ENTITY",
                    footnotes,
                    "year_to_date"
                    if metric["metric_code"] == "net_profit_ytd"
                    else "quarter_end",
                    comparability,
                )
            )
    return facts


def parse_regulatory_indicators(
    path: Path, input_dir: Path, dataset_id: str, year: int
) -> list[dict]:
    sheet = select_sheet(
        regulatory_sheets(path), ("信用风险指标", "不良贷款余额", "资本充足率")
    )
    context = make_context(path, input_dir, dataset_id, year, sheet, str(year))
    footnotes = extract_footnotes(sheet)
    commercial_banks = ENTITY_BY_ALIAS[label_key("商业银行")]
    period_columns = {}
    header_row = None
    for row_number, row in enumerate(sheet["values"], start=1):
        if label_key(row[0] if row else None) != "时间":
            continue
        columns = {
            col: parsed
            for col in range(2, len(row) + 1)
            if (parsed := period_from_token(year, get_cell(sheet, row_number, col)))
            and parsed[2] == "quarter"
        }
        if columns:
            header_row, period_columns = row_number, columns
            break
    if not header_row:
        raise ValueError("未找到主要监管指标季度表头")

    current_group = ""
    facts = []
    for row_number in range(header_row + 1, len(sheet["values"]) + 1):
        raw_metric = get_cell(sheet, row_number, 1)
        normalized = normalize_text(raw_metric)
        if normalized.startswith(("注", "备注")):
            break
        if re.match(r"^（[一二三四五]）", normalized):
            current_group = re.sub(r"^（[一二三四五]）", "", normalized).replace(
                "*", ""
            )
            continue
        metric = REGULATORY_METRIC_BY_ALIAS.get(label_key(raw_metric))
        numeric_values = [get_cell(sheet, row_number, col) for col in period_columns]
        if not metric:
            if any(nonempty(value) for value in numeric_values):
                raise ValueError(f"未知主要监管指标：{raw_metric}")
            continue
        unit = metric_unit(metric["metric_code"], metric["metric_name"])
        comparability = (
            "CAPITAL_RULES_2024_BREAK"
            if year >= 2024 and metric["metric_code"] in CAPITAL_METRICS
            else ""
        )
        for col, period in period_columns.items():
            raw_value = get_cell(sheet, row_number, col)
            if not nonempty(raw_value):
                continue
            facts.append(
                make_fact(
                    context,
                    period,
                    commercial_banks,
                    "商业银行",
                    metric["metric_code"],
                    metric["metric_name"],
                    raw_metric,
                    current_group,
                    "value",
                    raw_value,
                    unit,
                    unit,
                    row_number,
                    col,
                    "BANK_REGULATORY_INDICATORS_V2"
                    if year >= 2024
                    else "BANK_REGULATORY_INDICATORS_V1",
                    "法人",
                    "LEGAL_ENTITY",
                    footnotes,
                    "year_to_date"
                    if metric["metric_code"] == "net_profit_ytd"
                    else "quarter_end",
                    comparability,
                )
            )
    return facts


def parse_solvency(
    path: Path, input_dir: Path, dataset_id: str, year: int
) -> list[dict]:
    sheet = select_sheet(regulatory_sheets(path), ("综合偿付能力", "核心偿付能力"))
    context = make_context(path, input_dir, dataset_id, year, sheet, str(year))
    footnotes = extract_footnotes(sheet)
    period_columns = {}
    header_row = None
    for row_number, row in enumerate(sheet["values"], start=1):
        columns = {
            col: parsed
            for col in range(2, len(row) + 1)
            if (parsed := period_from_token(year, get_cell(sheet, row_number, col)))
            and parsed[2] == "quarter"
        }
        if len(columns) == 4:
            header_row, period_columns = row_number, columns
            break
    if not header_row:
        raise ValueError("未找到偿付能力季度表头")

    current_metric = None
    facts = []
    for row_number in range(header_row + 1, len(sheet["values"]) + 1):
        raw_metric = get_cell(sheet, row_number, 1)
        raw_entity = get_cell(sheet, row_number, 2)
        if normalize_text(raw_metric).startswith(("注", "备注")):
            break
        metric_key = label_key(raw_metric)
        if "综合偿付能力充足率" in metric_key:
            current_metric = (
                "comprehensive_solvency_adequacy_ratio",
                "综合偿付能力充足率",
                "偿付能力",
                "%",
                "ratio",
            )
        elif "核心偿付能力充足率" in metric_key:
            current_metric = (
                "core_solvency_adequacy_ratio",
                "核心偿付能力充足率",
                "偿付能力",
                "%",
                "ratio",
            )
        elif "风险综合评级" in metric_key:
            current_metric = (
                "risk_comprehensive_rating_company_count",
                "风险综合评级公司数量",
                "风险综合评级",
                "家",
                "count",
            )
        entity = canonical_entity(raw_entity)
        if not current_metric or not entity:
            continue
        metric_code, metric_name, metric_group, unit, measure_type = current_metric
        comparability = "RISK_RATING_NOT_PUBLISHED_2025" if year >= 2025 else ""
        for col, period in period_columns.items():
            raw_value = get_cell(sheet, row_number, col)
            if not nonempty(raw_value):
                continue
            facts.append(
                make_fact(
                    context,
                    period,
                    entity,
                    raw_entity,
                    metric_code,
                    metric_name,
                    raw_metric,
                    metric_group,
                    measure_type,
                    raw_value,
                    unit,
                    unit,
                    row_number,
                    col,
                    "INSURANCE_SOLVENCY_V2"
                    if year >= 2025
                    else "INSURANCE_SOLVENCY_V1",
                    "法人" if year <= 2022 else "监管公布口径",
                    "PUBLISHED_AGGREGATE",
                    footnotes,
                    "quarter_end",
                    comparability,
                )
            )
    return facts


def fund_base(value: object) -> tuple[str, str] | None:
    key = label_key(value)
    return next(
        (item for alias, item in FUND_BASES.items() if label_key(alias) == key), None
    )


def fund_yield(value: object) -> tuple[str, str] | None:
    key = label_key(value)
    return next(
        (item for alias, item in FUND_YIELDS.items() if label_key(alias) == key), None
    )


def fund_comparability(year: int, footnotes: str) -> str:
    flags = []
    if "风险处置" in footnotes or "不含明天系" in footnotes:
        flags.append("RISK_DISPOSAL_SCOPE_EXCLUSION")
    if year >= 2025:
        flags.append("YOY_AND_YIELDS_NOT_PUBLISHED_2025")
    if "口径不可比" in footnotes or "导致相关口径不可比" in footnotes:
        flags.append("ACCOUNTING_ADJUSTMENT_NOT_COMPARABLE")
    return "|".join(flags)


def add_fund_row(
    facts: list[dict],
    context: dict,
    period: tuple[date, date, str],
    entity: dict,
    entity_raw: object,
    raw_metric: object,
    values: tuple[tuple[int, str], ...],
    row_number: int,
    schema_version: str,
    balance_unit_raw: str,
    footnotes: str,
    comparability: str,
) -> None:
    yield_metric = fund_yield(raw_metric)
    if yield_metric:
        col = values[0][0]
        raw_value = get_cell(context["sheet"], row_number, col)
        if nonempty(raw_value):
            facts.append(
                make_fact(
                    context,
                    period,
                    entity,
                    entity_raw,
                    yield_metric[0],
                    yield_metric[1],
                    raw_metric,
                    "资金运用收益",
                    "annualized_rate",
                    raw_value,
                    "%",
                    "%",
                    row_number,
                    col,
                    schema_version,
                    "境内",
                    "PUBLISHED_SCOPE",
                    footnotes,
                    "year_to_date_annualized",
                    comparability,
                )
            )
        return

    base = fund_base(raw_metric)
    if not base:
        return
    suffixes = {
        "balance": (base[0] + "_balance", base[1] + "余额", "亿元"),
        "share": (base[0] + "_share", base[1] + "占比", "%"),
        "yoy": (base[0] + "_yoy", base[1] + "同比增长", "%"),
    }
    for col, measure_type in values:
        raw_value = get_cell(context["sheet"], row_number, col)
        if not nonempty(raw_value):
            continue
        metric_code, metric_name, unit = suffixes[measure_type]
        unit_raw = balance_unit_raw if measure_type == "balance" else "%"
        facts.append(
            make_fact(
                context,
                period,
                entity,
                entity_raw,
                metric_code,
                metric_name,
                raw_metric,
                "资金运用",
                measure_type,
                raw_value,
                unit,
                unit_raw,
                row_number,
                col,
                schema_version,
                "境内",
                "PUBLISHED_SCOPE",
                footnotes,
                "quarter_end",
                comparability,
                Decimal("10000")
                if measure_type == "balance" and balance_unit_raw == "万元"
                else None,
            )
        )


def parse_funds(path: Path, input_dir: Path, dataset_id: str, year: int) -> list[dict]:
    sheets = regulatory_sheets(path)
    sheet = select_sheet(sheets, ("资金运用余额",))
    quarter, period_start, period_end = quarter_from_filename(path, year)
    context = make_context(
        path, input_dir, dataset_id, year, sheet, f"{year}-Q{quarter}"
    )
    context["sheet"] = sheet
    period = (period_start, period_end, "quarter")
    footnotes = extract_footnotes(sheet)
    comparability = fund_comparability(year, footnotes)
    new_layout = any(
        label_key(get_cell(sheet, row, 1)) == "机构类别/指标"
        for row in range(1, min(10, len(sheet["values"])) + 1)
    )
    schema_version = (
        "INSURANCE_FUNDS_V3"
        if year >= 2025
        else "INSURANCE_FUNDS_V2"
        if new_layout
        else "INSURANCE_FUNDS_V1"
    )
    balance_unit_raw = "万元" if year == 2023 and quarter == 1 else "亿元"
    insurance_company = ENTITY_BY_ALIAS[label_key("保险公司")]
    current_entity, current_entity_raw = insurance_company, "保险公司"
    facts = []

    for row_number in range(1, len(sheet["values"]) + 1):
        first = get_cell(sheet, row_number, 1)
        if normalize_text(first).startswith(("注", "备注")):
            break
        if new_layout:
            if entity := canonical_entity(first):
                current_entity, current_entity_raw = entity, first
            raw_metric = get_cell(sheet, row_number, 2)
            if not nonempty(raw_metric):
                continue
            add_fund_row(
                facts,
                context,
                period,
                current_entity,
                current_entity_raw,
                raw_metric,
                ((3, "balance"), (4, "share"), (5, "yoy")),
                row_number,
                schema_version,
                balance_unit_raw,
                footnotes,
                comparability,
            )
        else:
            entity = canonical_entity(first)
            if entity and label_key(first) in {
                "人身险公司",
                "财产险公司",
                "财产保险公司",
            }:
                current_entity, current_entity_raw = entity, first
                continue
            if not nonempty(first):
                continue
            add_fund_row(
                facts,
                context,
                period,
                current_entity,
                current_entity_raw,
                first,
                ((2, "balance"), (3, "share"), (4, "yoy")),
                row_number,
                schema_version,
                balance_unit_raw,
                footnotes,
                comparability,
            )
    context.pop("sheet", None)
    return facts


def parse_schedule(
    path: Path, input_dir: Path, year: int
) -> tuple[list[dict], list[dict], list[dict]]:
    sheets = regulatory_sheets(path)
    relative = path.relative_to(input_dir).as_posix()
    digest = file_hash(path)
    tables = {}
    for sheet in sheets:
        for row_number, row in enumerate(sheet["values"], start=1):
            normalized = [normalize_text(value) for value in row]
            for table, required in {
                "schedule": (
                    "频度",
                    "信息发布时间",
                    "机构范围",
                    "数据范围",
                    "指标名称",
                ),
                "definitions": ("序号", "指标名称", "指标范围及计算公式"),
                "scopes": ("序号", "机构类", "机构范围"),
            }.items():
                if table not in tables and all(
                    value in normalized for value in required
                ):
                    tables[table] = (
                        sheet,
                        row_number,
                        {value: normalized.index(value) + 1 for value in required},
                    )

    schedule_rows = []
    if "schedule" in tables:
        schedule_sheet, header_row, columns = tables["schedule"]
        notes = extract_footnotes(schedule_sheet)
        current_frequency = ""
        current_timing = ""
        for row_number in range(header_row + 1, len(schedule_sheet["values"]) + 1):
            frequency = get_cell(schedule_sheet, row_number, columns["频度"])
            if normalize_text(frequency).startswith(("注", "备注")):
                break
            timing = get_cell(schedule_sheet, row_number, columns["信息发布时间"])
            indicator = get_cell(schedule_sheet, row_number, columns["指标名称"])
            if nonempty(frequency):
                current_frequency = str(frequency).strip()
            if nonempty(timing):
                current_timing = str(timing).strip()
            if not nonempty(indicator):
                continue
            schedule_rows.append(
                {
                    "release_year": year,
                    "row_order": row_number - header_row,
                    "frequency": current_frequency,
                    "release_timing": current_timing,
                    "institution_scope": str(
                        get_cell(schedule_sheet, row_number, columns["机构范围"]) or ""
                    ).strip(),
                    "data_scope": str(
                        get_cell(schedule_sheet, row_number, columns["数据范围"]) or ""
                    ).strip(),
                    "indicator_names": str(indicator).strip(),
                    "notes": notes,
                    "source_file": relative,
                    "source_file_hash": digest,
                    "source_sheet": schedule_sheet["name"],
                    "source_cell": source_cell(row_number, columns["指标名称"]),
                }
            )

    definitions = []
    if "definitions" in tables:
        definition_sheet, header_row, columns = tables["definitions"]
        for row_number in range(header_row + 1, len(definition_sheet["values"]) + 1):
            normalized = [
                normalize_text(value)
                for value in definition_sheet["values"][row_number - 1]
            ]
            if "机构类" in normalized and "机构范围" in normalized:
                break
            metric_name = get_cell(definition_sheet, row_number, columns["指标名称"])
            definition = get_cell(
                definition_sheet, row_number, columns["指标范围及计算公式"]
            )
            sequence = sequence_number(
                get_cell(definition_sheet, row_number, columns["序号"])
            )
            if sequence is None:
                continue
            if nonempty(metric_name) and nonempty(definition):
                definitions.append(
                    {
                        "release_year": year,
                        "sequence": sequence,
                        "metric_name": str(metric_name).strip(),
                        "definition": str(definition).strip(),
                        "source_file": relative,
                        "source_file_hash": digest,
                        "source_sheet": definition_sheet["name"],
                        "source_cell": source_cell(row_number, columns["指标名称"]),
                    }
                )

    scopes = []
    if "scopes" in tables:
        scope_sheet, header_row, columns = tables["scopes"]
        for row_number in range(header_row + 1, len(scope_sheet["values"]) + 1):
            institution_type = get_cell(scope_sheet, row_number, columns["机构类"])
            scope_definition = get_cell(scope_sheet, row_number, columns["机构范围"])
            sequence = sequence_number(
                get_cell(scope_sheet, row_number, columns["序号"])
            )
            if sequence is None:
                continue
            if nonempty(institution_type) and nonempty(scope_definition):
                scopes.append(
                    {
                        "release_year": year,
                        "sequence": sequence,
                        "institution_type": str(institution_type).strip(),
                        "scope_definition": str(scope_definition).strip(),
                        "source_file": relative,
                        "source_file_hash": digest,
                        "source_sheet": scope_sheet["name"],
                        "source_cell": source_cell(row_number, columns["机构类"]),
                    }
                )

    if not any((schedule_rows, definitions, scopes)):
        raise ValueError("未找到发布日程、指标解释或机构范围表头")
    return schedule_rows, definitions, scopes


PARSERS = {
    "bank_balance_monthly": parse_balance,
    "bank_balance_quarterly": parse_balance,
    "insurance_funds_investment": parse_funds,
    "insurance_solvency": parse_solvency,
    "inclusive_small_business_loans": parse_loans,
    "inclusive_agricultural_loans": parse_loans,
    "affordable_housing_loans": parse_loans,
    "commercial_bank_indicators_by_type": parse_indicators_by_type,
    "commercial_bank_regulatory_indicators": parse_regulatory_indicators,
}


def expected_fact_count(dataset_id: str, year: int, facts: list[dict]) -> int | None:
    periods = len({row["period_end"] for row in facts})
    if dataset_id.startswith("bank_balance_"):
        return periods * 40
    if dataset_id == "insurance_funds_investment":
        return 26 if year >= 2025 else 45
    if dataset_id == "insurance_solvency":
        return 32 if year >= 2025 else 48
    if dataset_id in {"inclusive_small_business_loans", "inclusive_agricultural_loans"}:
        return 20
    if dataset_id == "affordable_housing_loans":
        return 28
    if dataset_id == "commercial_bank_indicators_by_type":
        return 264
    if dataset_id == "commercial_bank_regulatory_indicators":
        return 152 if year >= 2024 else 148
    return None


def file_quality(
    dataset_id: str, year: int, source_file: str, facts: list[dict]
) -> dict:
    expected = expected_fact_count(dataset_id, year, facts)
    parse_errors = sum(row["value_status"] == "parse_error" for row in facts)
    flags = sorted(
        {flag for row in facts for flag in row["comparability_flag"].split("|") if flag}
    )
    details = []
    severity, status = "PASS", "OK"
    if expected is not None and len(facts) != expected:
        severity, status = "ERROR", "UNEXPECTED_FACT_COUNT"
        details.append(f"expected={expected}, actual={len(facts)}")
    if parse_errors:
        severity, status = "ERROR", "VALUE_PARSE_ERROR"
        details.append(f"parse_errors={parse_errors}")
    elif flags and severity == "PASS":
        severity, status = "WARN", "COMPARABILITY_CAVEAT"
    if flags:
        details.append("flags=" + "|".join(flags))
    return {
        "record_type": "FILE",
        "severity": severity,
        "dataset_id": dataset_id,
        "period": str(year),
        "source_file": source_file,
        "source_sheet": facts[0]["source_sheet"] if facts else "",
        "status": status,
        "fact_count": len(facts),
        "expected_fact_count": "" if expected is None else expected,
        "detail": "; ".join(details),
    }


def dataset_checks(facts: list[dict], quality: list[dict]) -> None:
    key_fields = (
        "dataset_id",
        "period_end",
        "scope",
        "entity_code",
        "metric_code",
        "measure_type",
    )
    seen, duplicates = set(), []
    for row in facts:
        key = tuple(row[field] for field in key_fields)
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        quality.append(
            {
                "record_type": "DATASET",
                "severity": "ERROR",
                "dataset_id": "*",
                "period": "",
                "source_file": "",
                "source_sheet": "",
                "status": "DUPLICATE_KEYS",
                "fact_count": len(facts),
                "expected_fact_count": "",
                "detail": f"duplicate_key_count={len(duplicates)}; first={duplicates[0]}",
            }
        )

    value_rows = {
        (
            row["dataset_id"],
            row["period_end"],
            row["entity_code"],
            row["metric_code"],
        ): row
        for row in facts
        if row["value"] is not None
    }
    values = {
        (
            row["dataset_id"],
            row["period_end"],
            row["entity_code"],
            row["metric_code"],
        ): row["value"]
        for row in facts
        if row["value"] is not None
    }

    def display_precision(row: dict) -> Decimal:
        text = row["value_raw"].replace(",", "").replace("%", "").replace("％", "")
        decimals = len(text.rsplit(".", 1)[1]) if "." in text else 0
        return Decimal(1).scaleb(-decimals)

    npl_failures = []
    for key, npl in values.items():
        dataset_id, period_end, entity_code, metric_code = key
        if (
            dataset_id != "commercial_bank_regulatory_indicators"
            or metric_code != "nonperforming_loan_balance"
        ):
            continue
        components = [
            values.get((dataset_id, period_end, entity_code, code))
            for code in (
                "substandard_loan_balance",
                "doubtful_loan_balance",
                "loss_loan_balance",
            )
        ]
        component_keys = [
            (dataset_id, period_end, entity_code, code)
            for code in (
                "substandard_loan_balance",
                "doubtful_loan_balance",
                "loss_loan_balance",
            )
        ]
        if all(value is not None for value in components):
            rows = [value_rows[key], *(value_rows[key] for key in component_keys)]
            tolerance = sum((display_precision(row) / 2 for row in rows), Decimal(0))
            if abs(npl - sum(components)) > tolerance:
                npl_failures.append(
                    f"{period_end}: diff={npl - sum(components)}, tolerance={tolerance}"
                )
    if npl_failures:
        quality.append(
            {
                "record_type": "DATASET",
                "severity": "ERROR",
                "dataset_id": "commercial_bank_regulatory_indicators",
                "period": "",
                "source_file": "",
                "source_sheet": "",
                "status": "NPL_IDENTITY_FAILED",
                "fact_count": "",
                "expected_fact_count": "",
                "detail": "; ".join(npl_failures[:10]),
            }
        )

    solvency_failures = []
    for key, core in values.items():
        dataset_id, period_end, entity_code, metric_code = key
        if (
            dataset_id != "insurance_solvency"
            or metric_code != "core_solvency_adequacy_ratio"
        ):
            continue
        comprehensive = values.get(
            (
                dataset_id,
                period_end,
                entity_code,
                "comprehensive_solvency_adequacy_ratio",
            )
        )
        if comprehensive is not None and core > comprehensive:
            solvency_failures.append(
                f"{period_end}/{entity_code}: core={core}, comprehensive={comprehensive}"
            )
    if solvency_failures:
        quality.append(
            {
                "record_type": "DATASET",
                "severity": "ERROR",
                "dataset_id": "insurance_solvency",
                "period": "",
                "source_file": "",
                "source_sheet": "",
                "status": "SOLVENCY_ORDER_FAILED",
                "fact_count": "",
                "expected_fact_count": "",
                "detail": "; ".join(solvency_failures[:10]),
            }
        )


def write_mapping(path: Path, facts: list[dict]) -> None:
    rows = set()
    for row in facts:
        rows.add(
            (
                "metric",
                row["dataset_id"],
                row["metric_name_raw"],
                label_key(row["metric_name_raw"]),
                row["metric_code"],
                row["metric_name"],
                row["unit"],
            )
        )
        rows.add(
            (
                "entity",
                row["dataset_id"],
                row["entity_name_raw"],
                label_key(row["entity_name_raw"]),
                row["entity_code"],
                row["entity_name"],
                "",
            )
        )
    fields = [
        "mapping_type",
        "dataset_id",
        "alias_raw",
        "alias_normalized",
        "canonical_code",
        "canonical_name",
        "unit",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(sorted(rows))


def write_quality(path: Path, quality: list[dict]) -> None:
    fields = [
        "record_type",
        "severity",
        "dataset_id",
        "period",
        "source_file",
        "source_sheet",
        "status",
        "fact_count",
        "expected_fact_count",
        "detail",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(quality)


def self_check() -> None:
    assert classify_file(Path("001_2026年银行业总资产、总负债（月度）_x.xls")) == (
        "bank_balance_monthly",
        2026,
    )
    assert classify_file(
        Path("015_2025年银行业金融机构普惠型涉农贷款情况表_x.xls")
    ) == ("inclusive_agricultural_loans", 2025)
    assert classify_file(Path("359_2019年银保监会监管统计信息发布日程表_x.xls")) == (
        "regulatory_release_schedule",
        2019,
    )
    assert period_from_token(2025, "四季度末")[1] == date(2025, 12, 31)
    assert canonical_entity("其中：财产保险公司")["entity_code"] == "property_insurers"
    assert fund_base("资金运用余额") == ("funds_investment", "资金运用")
    assert parse_decimal("3.40%")[0] == Decimal("3.400000")
    assert parse_decimal("—")[1] == "not_reported"
    assert format_excel_value(Decimal("12461.058486995"), "0_ ") == "12461"
    assert format_excel_value(Decimal("0.0223489054088857"), "0.00%") == "2.23%"


def run(input_dir: Path, output_dir: Path, check_only: bool) -> None:
    self_check()
    files = discover_files(input_dir)
    if not files:
        raise SystemExit("未找到目标监管统计 Excel 文件")

    facts, schedules, definitions, scopes, quality = [], [], [], [], []
    for path, dataset_id, year in files:
        relative = path.relative_to(input_dir).as_posix()
        try:
            if dataset_id == "regulatory_release_schedule":
                file_schedules, file_definitions, file_scopes = parse_schedule(
                    path, input_dir, year
                )
                schedules.extend(file_schedules)
                definitions.extend(file_definitions)
                scopes.extend(file_scopes)
                quality.append(
                    {
                        "record_type": "FILE",
                        "severity": "PASS",
                        "dataset_id": dataset_id,
                        "period": str(year),
                        "source_file": relative,
                        "source_sheet": next(
                            (
                                rows[0]["source_sheet"]
                                for rows in (
                                    file_schedules,
                                    file_definitions,
                                    file_scopes,
                                )
                                if rows
                            ),
                            "",
                        ),
                        "status": "OK",
                        "fact_count": len(file_schedules)
                        + len(file_definitions)
                        + len(file_scopes),
                        "expected_fact_count": "",
                        "detail": (
                            f"schedule={len(file_schedules)}, definitions={len(file_definitions)}, scopes={len(file_scopes)}"
                        ),
                    }
                )
                print(
                    f"PASS {year} {dataset_id}: schedule={len(file_schedules)}, definitions={len(file_definitions)}, scopes={len(file_scopes)}"
                )
                continue

            file_facts = PARSERS[dataset_id](path, input_dir, dataset_id, year)
            facts.extend(file_facts)
            qc = file_quality(dataset_id, year, relative, file_facts)
            quality.append(qc)
            print(f"{qc['severity']} {year} {dataset_id}: {len(file_facts)} facts")
        except Exception as exc:
            quality.append(
                {
                    "record_type": "FILE",
                    "severity": "ERROR",
                    "dataset_id": dataset_id,
                    "period": str(year),
                    "source_file": relative,
                    "source_sheet": "",
                    "status": "PARSE_ERROR",
                    "fact_count": 0,
                    "expected_fact_count": "",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"ERROR {year} {dataset_id} {path.name}: {exc}")

    facts.sort(
        key=lambda row: (
            row["dataset_id"],
            row["period_end"],
            row["entity_code"],
            row["metric_code"],
            row["measure_type"],
        )
    )
    schedules.sort(key=lambda row: (row["release_year"], row["row_order"]))
    definitions.sort(key=lambda row: (row["release_year"], row["sequence"]))
    scopes.sort(key=lambda row: (row["release_year"], row["sequence"]))
    dataset_checks(facts, quality)
    blocking_errors = [row for row in quality if row["severity"] == "ERROR"]

    if check_only:
        print(
            f"CHECK files={len(files)} facts={len(facts)} schedules={len(schedules)} errors={len(blocking_errors)}"
        )
        if blocking_errors:
            for row in blocking_errors:
                print(f"BLOCKING {row['dataset_id']} {row['status']}: {row['detail']}")
            raise SystemExit(1)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    write_mapping(output_dir / OUTPUT_NAMES["mapping"], facts)
    write_quality(output_dir / OUTPUT_NAMES["quality"], quality)
    if blocking_errors:
        print(
            f"BLOCKED {len(blocking_errors)} errors; quality report written to {output_dir}"
        )
        raise SystemExit(1)

    pq.write_table(
        pa.Table.from_pylist(facts, schema=FACT_SCHEMA),
        output_dir / OUTPUT_NAMES["facts"],
        compression="zstd",
    )
    pq.write_table(
        pa.Table.from_pylist(schedules, schema=SCHEDULE_SCHEMA),
        output_dir / OUTPUT_NAMES["schedule"],
        compression="zstd",
    )
    pq.write_table(
        pa.Table.from_pylist(definitions, schema=DEFINITION_SCHEMA),
        output_dir / OUTPUT_NAMES["definitions"],
        compression="zstd",
    )
    pq.write_table(
        pa.Table.from_pylist(scopes, schema=SCOPE_SCHEMA),
        output_dir / OUTPUT_NAMES["scopes"],
        compression="zstd",
    )
    print(
        f"WROTE files={len(files)} facts={len(facts)} schedules={len(schedules)} to {output_dir}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="清洗 2020 年代银行保险监管统计表为可追溯长表"
    )
    parser.add_argument("--input-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output-dir", type=Path, default=Path.cwd() / "output_regulatory_tables_clean"
    )
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    run(args.input_dir.resolve(), args.output_dir.resolve(), args.check_only)


if __name__ == "__main__":
    main()
