"""识别监管工作簿用途，并定位工作表中的连续内容表区。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field

from trusted_rag.domain.common import ContractModel, NonEmptyStr

WorkbookRole = Literal["fact_table", "dictionary", "template", "reference"]


class WorkbookClassification(ContractModel):
    """工作簿级业务路由结果。"""

    schema_version: Literal["workbook_classification.v1"] = "workbook_classification.v1"
    dataset_id: NonEmptyStr
    role: WorkbookRole
    rule_id: NonEmptyStr
    year: int | None = Field(default=None, ge=1900, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)


class TableRegionCandidate(ContractModel):
    """工作表中一个连续非空表区的候选位置。"""

    sheet_name: NonEmptyStr
    start_row: int = Field(ge=1)
    end_row: int = Field(ge=1)
    start_column: int = Field(ge=1)
    end_column: int = Field(ge=1)
    header_row: int = Field(ge=1)
    nonempty_cell_count: int = Field(ge=1)


_MONTHLY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "regional_premium_monthly",
        re.compile(r"^\d+_(20\d{2})年0?(\d{1,2})月全国各地区原保险保费收入情况表"),
    ),
    (
        "life_insurance_monthly",
        re.compile(r"^\d+_(20\d{2})年0?(\d{1,2})月人身险公司经营情况表_", re.I),
    ),
    (
        "property_insurance_monthly",
        re.compile(r"^\d+_(20\d{2})年0?(\d{1,2})月财产(?:保险|险)公司经营情况表_", re.I),
    ),
    (
        "insurance_industry_monthly",
        re.compile(r"^\d+_(20\d{2})年0?(\d{1,2})月保险业经营情况表_", re.I),
    ),
)

_REGULATORY_RULES: tuple[tuple[str, str], ...] = (
    ("bank_balance_monthly", "银行业总资产、总负债（月度）"),
    ("inclusive_small_business_loans", "普惠型小微企业贷款情况"),
    ("inclusive_agricultural_loans", "普惠型涉农贷款情况"),
    ("affordable_housing_loans", "保障性安居工程贷款情况"),
    ("commercial_bank_indicators_by_type", "商业银行主要指标分机构类情况表"),
    ("commercial_bank_regulatory_indicators", "商业银行主要监管指标情况表"),
    ("bank_balance_quarterly", "总资产、总负债（季度）"),
)


def classify_workbook(path: Path) -> WorkbookClassification:
    """依据 ZRY 已验证的文件名规则识别工作簿用途。

    :param path: XLS 或 XLSX 文件路径。
    :return: 工作簿级业务分类。
    :raises ValueError: 格式不支持、期间无效或没有匹配规则时抛出。
    """
    if path.suffix.lower() not in {".xls", ".xlsx"}:
        raise ValueError("只支持 XLS 或 XLSX 工作簿。")
    for dataset_id, pattern in _MONTHLY_RULES:
        match = pattern.search(path.name)
        if match:
            month = int(match.group(2))
            if not 1 <= month <= 12:
                raise ValueError("工作簿文件名中的月份无效。")
            return WorkbookClassification(
                dataset_id=dataset_id,
                role="fact_table",
                rule_id=f"filename:{dataset_id}",
                year=int(match.group(1)),
                month=month,
            )

    compact_name = re.sub(r"\s+", "", path.name)
    year_match = re.match(r"^\d+_(20\d{2})年", compact_name)
    if year_match:
        year = int(year_match.group(1))
        if any(token in compact_name for token in ("监管统计信息发布日程", "机构范围", "指标解释")):
            return WorkbookClassification(
                dataset_id="regulatory_release_schedule",
                role="dictionary",
                rule_id="filename:regulatory_release_schedule",
                year=year,
            )
        for dataset_id, token in _REGULATORY_RULES:
            if token in compact_name:
                return WorkbookClassification(
                    dataset_id=dataset_id,
                    role="fact_table",
                    rule_id=f"filename:{dataset_id}",
                    year=year,
                )
        if re.search(r"[一二三四1234]季度保险(?:业|公司)资金运用情况表", compact_name):
            return WorkbookClassification(
                dataset_id="insurance_funds_investment",
                role="fact_table",
                rule_id="filename:insurance_funds_investment",
                year=year,
            )
        if "偿付能力" in compact_name and any(token in compact_name for token in ("状况表", "情况表")):
            return WorkbookClassification(
                dataset_id="insurance_solvency",
                role="fact_table",
                rule_id="filename:insurance_solvency",
                year=year,
            )

    file_id = path.name.partition("_")[0]
    template_file_ids = {
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
    if file_id in template_file_ids:
        return WorkbookClassification(
            dataset_id="excel_template_library",
            role="template",
            rule_id="file_id:template_library",
        )
    return WorkbookClassification(
        dataset_id="excel_reference_documents",
        role="reference",
        rule_id="fallback:reference_document",
    )


def detect_table_regions(
    sheet_name: str,
    rows: Sequence[Sequence[object]],
) -> list[TableRegionCandidate]:
    """按连续非空行定位可供专用解析器进一步识别的表区。

    :param sheet_name: 原始工作表名称。
    :param rows: 按行排列的原始单元格值。
    :return: 按起始行排序的连续表区候选；全空工作表返回空列表。
    """
    regions: list[TableRegionCandidate] = []
    active: list[tuple[int, list[int]]] = []

    def flush() -> None:
        if not active:
            return
        columns = [column for _, row_columns in active for column in row_columns]
        regions.append(
            TableRegionCandidate(
                sheet_name=sheet_name,
                start_row=active[0][0],
                end_row=active[-1][0],
                start_column=min(columns),
                end_column=max(columns),
                header_row=active[0][0],
                nonempty_cell_count=len(columns),
            )
        )
        active.clear()

    for row_number, row in enumerate(rows, start=1):
        nonempty_columns = [
            column_number
            for column_number, value in enumerate(row, start=1)
            if value is not None and str(value).strip()
        ]
        if nonempty_columns:
            active.append((row_number, nonempty_columns))
        else:
            flush()
    flush()
    return regions
