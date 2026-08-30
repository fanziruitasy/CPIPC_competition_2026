"""抽取保险业月度经营事实。"""

# ruff: noqa: D103, RUF003

from __future__ import annotations

import argparse
import re
from decimal import Decimal
from pathlib import Path

from trusted_rag.ingestion.spreadsheets.cleaners import life_insurance as cleaner

TARGET_RE = re.compile(
    r"^\d+_(20\d{2})年0?(\d{1,2})月保险业经营情况表_.*\.(?:xls|xlsx)$", re.I
)
OUTPUT_NAMES = {
    "facts": "insurance_industry_facts.parquet",
    "mapping": "metric_mapping.csv",
    "quality": "quality_report.csv",
}

# 代码、规范名称、单位、期间口径、顺序、来源别名。
METRICS = [
    (
        "original_premium_income",
        "原保险保费收入",
        "CNY_100M",
        "YTD",
        10,
        ["原保险保费收入"],
    ),
    ("sum_insured", "保险金额", "CNY_100M", "YTD", 20, ["保险金额"]),
    ("policy_count", "保单件数", "TEN_THOUSAND_POLICIES", "YTD", 30, ["保单件数"]),
    (
        "claims_paid",
        "赔付支出",
        "CNY_100M",
        "YTD",
        40,
        ["原保险赔付支出", "赔付支出", "赔款与给付支出"],
    ),
    (
        "operating_management_expense",
        "业务及管理费",
        "CNY_100M",
        "YTD",
        50,
        ["业务及管理费"],
    ),
    (
        "funds_employed_balance",
        "资金运用余额",
        "CNY_100M",
        "POINT_IN_TIME",
        60,
        ["资金运用余额"],
    ),
    ("total_assets", "总资产", "CNY_100M", "POINT_IN_TIME", 70, ["资产总额", "总资产"]),
    ("net_assets", "净资产", "CNY_100M", "POINT_IN_TIME", 80, ["净资产"]),
]

# 共享清洗器把该维度称为 product_line；此处表示各指标的公开拆分项。
PRODUCT_LINES = [
    (
        "property_insurance",
        "财产险",
        10,
        ["1、财产险", "1、财产保险", "财产险", "财产保险"],
    ),
    ("personal_insurance", "人身险", 20, ["2、人身险", "人身险"]),
    ("life_insurance", "寿险", 21, ["（1）寿险", "(1)寿险", "寿险"]),
    ("health_insurance", "健康险", 22, ["（2）健康险", "(2)健康险", "健康险"]),
    (
        "accident_insurance",
        "人身意外伤害险",
        23,
        ["（3）人身意外伤害险", "(3)人身意外伤害险", "人身意外伤害险"],
    ),
    ("bank_deposits", "银行存款", 30, ["其中：银行存款", "银行存款"]),
    ("bonds", "债券", 31, ["债券"]),
    ("stocks_and_securities_funds", "股票和证券投资基金", 32, ["股票和证券投资基金"]),
    (
        "property_insurance_company",
        "财产险公司",
        40,
        ["其中：财产险公司", "财产险公司"],
    ),
    ("personal_insurance_company", "人身险公司", 41, ["人身险公司"]),
    ("reinsurance_company", "再保险公司", 42, ["其中：再保险公司", "再保险公司"]),
    (
        "insurance_asset_management_company",
        "保险资产管理公司",
        43,
        [
            "其中：保险资产管理公司",
            "保险资产管理公司",
            "其中：资产管理公司",
            "资产管理公司",
        ],
    ),
]


def keys(metric: str, breakdowns: set[str]) -> set[tuple[str, str]]:
    return {(metric, "all")} | {(metric, breakdown) for breakdown in breakdowns}


INSURANCE_BREAKDOWNS = {
    "property_insurance",
    "personal_insurance",
    "life_insurance",
    "health_insurance",
    "accident_insurance",
}
TOP_INSURANCE_BREAKDOWNS = {"property_insurance", "personal_insurance"}
INVESTMENT_BREAKDOWNS = {"bank_deposits", "bonds", "stocks_and_securities_funds"}
LEGACY_ASSET_BREAKDOWNS = {"reinsurance_company", "insurance_asset_management_company"}
MODERN_ASSET_BREAKDOWNS = LEGACY_ASSET_BREAKDOWNS | {
    "property_insurance_company",
    "personal_insurance_company",
}

FULL_KEYS = (
    keys("original_premium_income", INSURANCE_BREAKDOWNS)
    | {("sum_insured", "all"), ("policy_count", "all")}
    | keys("claims_paid", INSURANCE_BREAKDOWNS)
    | {("operating_management_expense", "all")}
    | keys("funds_employed_balance", INVESTMENT_BREAKDOWNS)
    | keys("total_assets", LEGACY_ASSET_BREAKDOWNS)
    | {("net_assets", "all")}
)
REDUCED_2023_KEYS = (
    keys("original_premium_income", TOP_INSURANCE_BREAKDOWNS)
    | keys("claims_paid", TOP_INSURANCE_BREAKDOWNS)
    | keys("funds_employed_balance", INVESTMENT_BREAKDOWNS)
    | keys("total_assets", LEGACY_ASSET_BREAKDOWNS)
    | {("net_assets", "all")}
)
REDUCED_2024_KEYS = (
    keys("original_premium_income", TOP_INSURANCE_BREAKDOWNS)
    | keys("claims_paid", TOP_INSURANCE_BREAKDOWNS)
    | keys("total_assets", LEGACY_ASSET_BREAKDOWNS)
    | {("net_assets", "all")}
)
MODERN_KEYS = (
    keys("original_premium_income", TOP_INSURANCE_BREAKDOWNS)
    | keys("claims_paid", TOP_INSURANCE_BREAKDOWNS)
    | keys("total_assets", MODERN_ASSET_BREAKDOWNS)
    | {("net_assets", "all")}
)
EXPECTED_KEYS = {
    "S1_FULL_DETAILS": FULL_KEYS,
    "S2_2023_REDUCED": REDUCED_2023_KEYS,
    "S3_2024_NO_INVESTMENTS": REDUCED_2024_KEYS,
    "S4_2025_ASSET_BREAKDOWN": MODERN_KEYS,
    "S5_2026_ACCOUNTING": MODERN_KEYS,
}
EXPECTED_FACTS = {schema: len(expected) for schema, expected in EXPECTED_KEYS.items()}

METRIC_BY_ALIAS = {
    cleaner.label_key(alias): {
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
    cleaner.label_key(alias): {
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
        if not path.is_file() or path.name.startswith("~$"):
            continue
        match = TARGET_RE.match(path.name)
        if match and 1 <= int(match.group(2)) <= 12:
            found.append((path, int(match.group(1)), int(match.group(2))))
    return sorted(found, key=lambda item: (item[1], item[2], item[0].name))


def schema_version(facts: list[dict]) -> str:
    metric_codes = {row["metric_code"] for row in facts}
    breakdowns = {row["product_line"] for row in facts}
    claim_labels = {
        cleaner.label_key(row["metric_name_raw"])
        for row in facts
        if row["metric_code"] == "claims_paid" and row["product_line"] == "all"
    }
    if "赔款与给付支出" in claim_labels:
        return "S5_2026_ACCOUNTING"
    if {"property_insurance_company", "personal_insurance_company"} <= breakdowns:
        return "S4_2025_ASSET_BREAKDOWN"
    if "funds_employed_balance" not in metric_codes:
        return "S3_2024_NO_INVESTMENTS"
    if "sum_insured" not in metric_codes:
        return "S2_2023_REDUCED"
    return "S1_FULL_DETAILS"


def identity_check(facts: list[dict]) -> tuple[Decimal | None, Decimal | None]:
    by_key = {(row["metric_code"], row["product_line"]): row["value"] for row in facts}
    diffs = []
    for metric in ("original_premium_income", "claims_paid"):
        total = by_key.get((metric, "all"))
        top_parts = [
            by_key.get((metric, item))
            for item in ("property_insurance", "personal_insurance")
        ]
        if total is not None and all(value is not None for value in top_parts):
            diffs.append(abs(total - sum(top_parts, Decimal(0))))
        personal = by_key.get((metric, "personal_insurance"))
        personal_parts = [
            by_key.get((metric, item))
            for item in ("life_insurance", "health_insurance", "accident_insurance")
        ]
        if personal is not None and all(value is not None for value in personal_parts):
            diffs.append(abs(personal - sum(personal_parts, Decimal(0))))
    values = [row["value"] for row in facts if row["value"] is not None]
    tolerance = Decimal("2") if cleaner.precision_unit(values) == 1 else Decimal("0.02")
    return max(diffs, default=None), tolerance


base_parse_file = cleaner.parse_file


def parse_file(
    path: Path, year: int, month: int, input_dir: Path
) -> tuple[list[dict], dict]:
    facts, quality = base_parse_file(path, year, month, input_dir)
    for fact in facts:
        fact["entity_code"] = "CN_INSURANCE_INDUSTRY_TOTAL"
        fact["entity_name"] = "全国保险业汇总"
    return facts, quality


def self_check() -> None:
    assert TARGET_RE.match(
        "035_2025年9月保险业经营情况表_2025年9月保险业经营情况表.xlsx"
    )
    assert TARGET_RE.match(
        "005_2026年2月保险业经营情况表_2026年2月保险业经营情况表.xls"
    )
    assert (
        METRIC_BY_ALIAS[cleaner.label_key("赔款与给付支出")]["metric_code"]
        == "claims_paid"
    )
    assert (
        PRODUCT_BY_ALIAS[cleaner.label_key("其中：资产管理公司")]["product_line"]
        == "insurance_asset_management_company"
    )
    assert [
        len(FULL_KEYS),
        len(REDUCED_2023_KEYS),
        len(REDUCED_2024_KEYS),
        len(MODERN_KEYS),
    ] == [23, 14, 10, 12]
    sample = [
        {"metric_code": "claims_paid", "product_line": item, "value": value}
        for item, value in (
            ("all", Decimal("10")),
            ("property_insurance", Decimal("4")),
            ("personal_insurance", Decimal("6")),
        )
    ]
    assert identity_check(sample)[0] == 0


def configure_cleaner() -> None:
    cleaner.TARGET_RE = TARGET_RE
    cleaner.OUTPUT_NAMES = OUTPUT_NAMES
    cleaner.METRICS = METRICS
    cleaner.PRODUCT_LINES = PRODUCT_LINES
    cleaner.EXPECTED_FACTS = EXPECTED_FACTS
    cleaner.EXPECTED_KEYS = EXPECTED_KEYS
    cleaner.METRIC_BY_ALIAS = METRIC_BY_ALIAS
    cleaner.PRODUCT_BY_ALIAS = PRODUCT_BY_ALIAS
    cleaner.schema_version = schema_version
    cleaner.identity_check = identity_check
    cleaner.parse_file = parse_file
    cleaner.self_check = self_check


def run(input_dir: Path, output_dir: Path, check_only: bool = False) -> None:
    configure_cleaner()
    cleaner.run(input_dir, output_dir, check_only)


def main() -> None:
    parser = argparse.ArgumentParser(description="将保险业经营情况表清洗为可追溯长表")
    parser.add_argument("--input-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "output_insurance_industry_clean",
    )
    parser.add_argument(
        "--check-only", action="store_true", help="全量解析和校验，不写结果文件"
    )
    args = parser.parse_args()
    run(args.input_dir.resolve(), args.output_dir.resolve(), args.check_only)


if __name__ == "__main__":
    main()
