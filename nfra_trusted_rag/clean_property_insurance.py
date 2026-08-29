from __future__ import annotations

import argparse
import re
from decimal import Decimal
from pathlib import Path

import clean_life_insurance as cleaner


TARGET_RE = re.compile(
    r"^\d+_(20\d{2})年0?(\d{1,2})月财产(?:保险|险)公司经营情况表_.*\.(?:xls|xlsx)$",
    re.I,
)
OUTPUT_NAMES = {
    "facts": "property_insurance_facts.parquet",
    "mapping": "metric_mapping.csv",
    "quality": "quality_report.csv",
}

# code, canonical name, unit, period basis, order, source aliases
METRICS = [
    ("original_premium_income", "原保险保费收入", "CNY_100M", "YTD", 10, ["原保险保费收入"]),
    (
        "claims_paid",
        "赔款支出",
        "CNY_100M",
        "YTD",
        20,
        ["原保险赔款支出", "赔款支出", "赔款与给付支出"],
    ),
    ("sum_insured", "保险金额", "CNY_100M", "YTD", 30, ["保险金额"]),
    ("policy_count", "保单件数", "TEN_THOUSAND_POLICIES", "YTD", 40, ["保单件数"]),
    ("total_assets", "总资产", "CNY_100M", "POINT_IN_TIME", 50, ["资产总额", "总资产"]),
]

# code, canonical name, order, source aliases
PRODUCT_LINES = [
    ("enterprise_property", "企业财产保险", 10, ["其中：企业财产保险", "企业财产保险"]),
    ("household_property", "家庭财产保险", 20, ["其中：家庭财产保险", "家庭财产保险"]),
    ("motor_vehicle", "机动车辆保险", 30, ["其中：机动车辆保险", "机动车辆保险"]),
    ("engineering", "工程保险", 40, ["其中：工程保险", "工程保险"]),
    ("liability", "责任险", 50, ["其中：责任保险", "其中：责任险", "责任保险", "责任险"]),
    ("guarantee", "保证保险", 60, ["其中：保证保险", "保证保险"]),
    ("agriculture", "农业保险", 70, ["其中：农业保险", "农业保险"]),
    ("health", "健康险", 80, ["其中：健康险", "健康险"]),
    ("accident", "意外险", 90, ["其中：意外险", "意外险"]),
    ("cargo", "货运险", 100, ["其中：货运险", "货运险", "其中：货物运输保险", "货物运输保险"]),
]

LEGACY_PREMIUM_PRODUCTS = {
    "enterprise_property",
    "household_property",
    "motor_vehicle",
    "engineering",
    "liability",
    "guarantee",
    "agriculture",
    "health",
    "accident",
}
LEGACY_SUM_INSURED_PRODUCTS = {"motor_vehicle", "liability", "agriculture", "health", "accident"}
LEGACY_POLICY_PRODUCTS = {"motor_vehicle", "liability", "cargo", "guarantee", "health", "accident"}
REDUCED_PREMIUM_PRODUCTS = {"motor_vehicle", "liability", "agriculture", "health", "accident"}
REDUCED_SUM_INSURED_PRODUCTS = {"motor_vehicle", "liability", "agriculture"}
REDUCED_POLICY_PRODUCTS = {"motor_vehicle", "liability"}


def keys(metric: str, products: set[str]) -> set[tuple[str, str]]:
    return {(metric, "all")} | {(metric, product) for product in products}


LEGACY_KEYS = (
    keys("original_premium_income", LEGACY_PREMIUM_PRODUCTS)
    | {("claims_paid", "all")}
    | keys("sum_insured", LEGACY_SUM_INSURED_PRODUCTS)
    | keys("policy_count", LEGACY_POLICY_PRODUCTS)
    | {("total_assets", "all")}
)
REDUCED_KEYS = (
    keys("original_premium_income", REDUCED_PREMIUM_PRODUCTS)
    | {("claims_paid", "all")}
    | keys("sum_insured", REDUCED_SUM_INSURED_PRODUCTS)
    | keys("policy_count", REDUCED_POLICY_PRODUCTS)
    | {("total_assets", "all")}
)
NO_POLICY_KEYS = REDUCED_KEYS - keys("policy_count", REDUCED_POLICY_PRODUCTS)

EXPECTED_KEYS = {
    "S1_LEGACY_LABELS": LEGACY_KEYS,
    "S2_MODERN_LABELS": LEGACY_KEYS,
    "S3_REDUCED_BREAKDOWNS": REDUCED_KEYS,
    "S4_NO_POLICY_COUNT": NO_POLICY_KEYS,
    "S5_2026_ACCOUNTING": NO_POLICY_KEYS,
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


def schema_version(facts: list[dict]) -> str:
    release_id = facts[0]["release_id"] if facts else ""
    raw_labels = {cleaner.label_key(row["metric_name_raw"]) for row in facts if row["product_line"] == "all"}
    product_lines = {row["product_line"] for row in facts}
    metric_codes = {row["metric_code"] for row in facts}
    if release_id >= "2026-01":
        return "S5_2026_ACCOUNTING"
    if "policy_count" not in metric_codes:
        return "S4_NO_POLICY_COUNT"
    if not {"enterprise_property", "household_property", "engineering"} & product_lines:
        return "S3_REDUCED_BREAKDOWNS"
    if "原保险赔款支出" in raw_labels or "资产总额" in raw_labels:
        return "S1_LEGACY_LABELS"
    return "S2_MODERN_LABELS"


def identity_check(facts: list[dict]) -> tuple[Decimal | None, Decimal | None]:
    totals = {
        row["metric_code"]: row["value"]
        for row in facts
        if row["product_line"] == "all" and row["value"] is not None
    }
    excesses = [
        row["value"] - totals[row["metric_code"]]
        for row in facts
        if row["product_line"] != "all"
        and row["value"] is not None
        and row["metric_code"] in totals
        and row["value"] > totals[row["metric_code"]]
    ]
    values = [row["value"] for row in facts if row["value"] is not None]
    tolerance = Decimal("2") if cleaner.precision_unit(values) == 1 else Decimal("0.02")
    return max(excesses, default=Decimal(0)), tolerance


base_parse_file = cleaner.parse_file


def parse_file(path: Path, year: int, month: int, input_dir: Path) -> tuple[list[dict], dict]:
    facts, quality = base_parse_file(path, year, month, input_dir)
    for fact in facts:
        fact["entity_code"] = "CN_PROPERTY_INSURANCE_INDUSTRY_TOTAL"
        fact["entity_name"] = "全国财产险行业汇总"
    if (year, month) == (2026, 1):
        quality["detail"] = quality["detail"].replace("赔付指标及会计基础自本期发生变化", "会计基础自本期发生变化")
    return facts, quality


def self_check() -> None:
    assert TARGET_RE.match("034_2025年9月财产保险公司经营情况表_2025年9月财产险公司经营情况表.xlsx")
    assert TARGET_RE.match("004_2026年2月财产险公司经营情况表_2026年2月财产险公司经营情况表.xls")
    assert METRIC_BY_ALIAS[cleaner.label_key("原保险赔款支出")]["metric_code"] == "claims_paid"
    assert PRODUCT_BY_ALIAS[cleaner.label_key("其中：责任保险")]["product_line"] == "liability"
    assert len(LEGACY_KEYS) == 25
    assert len(REDUCED_KEYS) == 15
    assert len(NO_POLICY_KEYS) == 12


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


def main() -> None:
    parser = argparse.ArgumentParser(description="将财产险公司经营情况表清洗为可追溯长表")
    parser.add_argument("--input-dir", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "output_property_insurance_clean")
    parser.add_argument("--check-only", action="store_true", help="全量解析和校验，不写结果文件")
    args = parser.parse_args()
    configure_cleaner()
    cleaner.run(args.input_dir.resolve(), args.output_dir.resolve(), args.check_only)


if __name__ == "__main__":
    main()
