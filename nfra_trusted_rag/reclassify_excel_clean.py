from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = ROOT / "data"
DEFAULT_OUTPUT_DIR = ROOT / "output_excel_reclassified_clean"


FACT_DATASETS = {
    "insurance_industry_monthly": {
        "source": ROOT / "output_insurance_industry_clean" / "insurance_industry_facts.parquet",
        "existing_category": "insurance_industry",
        "domain_code": "insurance",
        "domain_name": "保险业",
        "topic_code": "insurance_industry_operations",
        "topic_name": "保险业经营",
        "frequency": "month",
    },
    "life_insurance_monthly": {
        "source": ROOT / "output_life_insurance_clean" / "life_insurance_facts.parquet",
        "existing_category": "life_insurance",
        "domain_code": "insurance",
        "domain_name": "保险业",
        "topic_code": "life_insurance_operations",
        "topic_name": "人身险经营",
        "frequency": "month",
    },
    "property_insurance_monthly": {
        "source": ROOT / "output_property_insurance_clean" / "property_insurance_facts.parquet",
        "existing_category": "property_insurance",
        "domain_code": "insurance",
        "domain_name": "保险业",
        "topic_code": "property_insurance_operations",
        "topic_name": "财产险经营",
        "frequency": "month",
    },
    "regional_premium_monthly": {
        "source": ROOT / "output_region_premium_clean" / "region_premium_facts.parquet",
        "existing_category": "region_premium",
        "domain_code": "insurance",
        "domain_name": "保险业",
        "topic_code": "regional_premium",
        "topic_name": "地区原保险保费",
        "frequency": "month",
    },
}


REGULATORY_DATASETS = {
    "bank_balance_monthly": ("banking", "银行业", "bank_balance_sheet", "银行资产负债", "month"),
    "bank_balance_quarterly": ("banking", "银行业", "bank_balance_sheet", "银行资产负债", "quarter"),
    "commercial_bank_regulatory_indicators": (
        "banking",
        "银行业",
        "commercial_bank_regulatory_indicators",
        "商业银行主要监管指标",
        "quarter",
    ),
    "commercial_bank_indicators_by_type": (
        "banking",
        "银行业",
        "commercial_bank_indicators_by_type",
        "商业银行分机构类指标",
        "quarter",
    ),
    "inclusive_small_business_loans": (
        "banking",
        "银行业",
        "inclusive_small_business_loans",
        "普惠型小微企业贷款",
        "quarter",
    ),
    "inclusive_agricultural_loans": (
        "banking",
        "银行业",
        "inclusive_agricultural_loans",
        "普惠型涉农贷款",
        "quarter",
    ),
    "affordable_housing_loans": (
        "banking",
        "银行业",
        "affordable_housing_loans",
        "保障性安居工程贷款",
        "quarter",
    ),
    "insurance_funds_investment": (
        "insurance",
        "保险业",
        "insurance_funds_investment",
        "保险资金运用",
        "quarter",
    ),
    "insurance_solvency": (
        "insurance",
        "保险业",
        "insurance_solvency",
        "保险公司偿付能力",
        "quarter",
    ),
}


DOCUMENT_RULES = [
    {
        "family": "property_insurer_county_reporting_template",
        "domain": ("insurance", "保险业"),
        "topic": ("county_insurance_statistics", "县域保险统计"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "财产保险公司县域机构统计报送",
        "all": ["财产保险公司县域机构原保险保费收入统计表"],
    },
    {
        "family": "life_insurer_county_reporting_template",
        "domain": ("insurance", "保险业"),
        "topic": ("county_insurance_statistics", "县域保险统计"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "人身保险公司县域机构统计报送",
        "all": ["人身保险公司县域机构原保险保费收入统计表"],
    },
    {
        "family": "nonlife_reserve_backtesting_template",
        "domain": ("insurance", "保险业"),
        "topic": ("nonlife_reserves", "非寿险准备金"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "非寿险准备金回溯分析",
        "all": ["年度准备金回溯结果汇总表"],
    },
    {
        "family": "compulsory_traffic_reserve_report_template",
        "domain": ("insurance", "保险业"),
        "topic": ("nonlife_reserves", "非寿险准备金"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "交强险专项准备金评估报告",
        "all": ["未到期责任准备金", "死亡伤残", "医疗费用"],
    },
    {
        "family": "reinsurer_nonlife_reserve_report_template",
        "domain": ("insurance", "保险业"),
        "topic": ("nonlife_reserves", "非寿险准备金"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "再保险公司非寿险准备金评估报告",
        "all": ["比例合约汇总", "终极保费"],
    },
    {
        "family": "insurer_nonlife_reserve_report_template",
        "domain": ("insurance", "保险业"),
        "topic": ("nonlife_reserves", "非寿险准备金"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "保险公司非寿险准备金评估报告",
        "all": ["险种名称", "未到期责任准备金"],
    },
    {
        "family": "reserve_report_signature_page",
        "domain": ("insurance", "保险业"),
        "topic": ("nonlife_reserves", "非寿险准备金"),
        "content_type": ("signature_page", "签字页"),
        "function": "准备金监管报表签章",
        "all": ["准备金评估报告监管报表签字页"],
    },
    {
        "family": "catastrophe_risk_capital_calculation_template",
        "domain": ("insurance", "保险业"),
        "topic": ("solvency_capital", "偿付能力与最低资本"),
        "content_type": ("calculation_template", "计算模板"),
        "function": "巨灾风险损失因子与最低资本计算",
        "all": ["巨灾风险损失因子表和最低资本计算模板"],
    },
    {
        "family": "insurance_group_risk_management_assessment",
        "domain": ("insurance", "保险业"),
        "topic": ("solvency_risk_management", "偿付能力风险管理"),
        "content_type": ("assessment_template", "评估表"),
        "function": "保险集团偿付能力风险管理能力评估",
        "all": ["保险集团偿付能力风险管理能力评估表"],
    },
    {
        "family": "insurer_risk_management_assessment",
        "domain": ("insurance", "保险业"),
        "topic": ("solvency_risk_management", "偿付能力风险管理"),
        "content_type": ("assessment_template", "评估表"),
        "function": "保险公司偿付能力风险管理能力评估",
        "all": ["保险公司偿付能力风险管理能力评估表"],
    },
    {
        "family": "insurer_quarterly_solvency_report_template",
        "domain": ("insurance", "保险业"),
        "topic": ("solvency_reporting", "偿付能力报告"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "保险公司偿付能力季度报告",
        "all": ["S01-偿付能力状况表", "S02-实际资本表"],
    },
    {
        "family": "insurance_group_solvency_report_template",
        "domain": ("insurance", "保险业"),
        "topic": ("solvency_reporting", "偿付能力报告"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "保险集团偿付能力报告",
        "all": ["保险控股型集团偿付能力状况表", "保险控股型集团实际资本表"],
    },
    {
        "family": "property_insurer_stress_test_template",
        "domain": ("insurance", "保险业"),
        "topic": ("solvency_stress_test", "偿付能力压力测试"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "财产保险公司偿付能力压力测试",
        "all": ["财产保险公司偿付能力压力测试报告Excel样表"],
    },
    {
        "family": "life_insurer_stress_test_template",
        "domain": ("insurance", "保险业"),
        "topic": ("solvency_stress_test", "偿付能力压力测试"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "人身保险公司偿付能力压力测试",
        "all": ["人身保险公司偿付能力压力测试报告Excel样表"],
    },
    {
        "family": "accident_insurance_business_reporting_template",
        "domain": ("insurance", "保险业"),
        "topic": ("accident_insurance_operations", "意外伤害保险经营"),
        "content_type": ("reporting_template", "报送模板"),
        "function": "意外伤害保险经营情况报送",
        "all": ["个人意外伤害保险业务年度经营数据"],
    },
    {
        "family": "small_business_regulatory_evaluation_rules",
        "domain": ("banking", "银行业"),
        "topic": ("small_business_regulation", "小微企业金融服务监管"),
        "content_type": ("rule_table", "规则与评分表"),
        "function": "小微企业金融服务监管评价",
        "all": ["银行业金融机构小微企业金融服务监管评价指标表"],
    },
    {
        "family": "money_broker_data_service_institution_list",
        "domain": ("financial_market", "金融市场"),
        "topic": ("money_broker_data_service", "货币经纪数据服务"),
        "content_type": ("reference_list", "参考名单"),
        "function": "可接受数据服务机构名录",
        "all": ["货币经纪公司可提供数据服务的机构名单"],
    },
]


QUALITY_SOURCES = {
    "region_premium": ROOT / "output_region_premium_clean" / "quality_report.csv",
    "life_insurance": ROOT / "output_life_insurance_clean" / "quality_report.csv",
    "property_insurance": ROOT / "output_property_insurance_clean" / "quality_report.csv",
    "insurance_industry": ROOT / "output_insurance_industry_clean" / "quality_report.csv",
    "regulatory_tables": ROOT / "output_regulatory_tables_clean" / "quality_report.csv",
}


def pipe(values: pd.Series) -> str:
    return "|".join(sorted({str(value) for value in values.dropna() if str(value).strip()}))


def add_classification_columns(df: pd.DataFrame, meta: dict) -> pd.DataFrame:
    result = df.copy()
    columns = [
        ("content_type", meta["content_type_code"]),
        ("domain_code", meta["domain_code"]),
        ("domain_name", meta["domain_name"]),
        ("topic_code", meta["topic_code"]),
        ("topic_name", meta["topic_name"]),
        ("dataset_family", meta["dataset_family"]),
        ("frequency", meta["frequency"]),
    ]
    for name, value in reversed(columns):
        if name not in result:
            result.insert(0, name, value)
    return result


def source_hash(frame: pd.DataFrame) -> str:
    hashes = frame["source_file_hash"].dropna().astype(str).unique()
    if len(hashes) != 1:
        raise ValueError(f"源文件哈希不唯一：{hashes.tolist()}")
    return hashes[0]


def staged_path(source_root: Path, path: Path) -> Path:
    """Resolve an intermediate-cleaning path under the selected staging root."""
    return source_root / path.relative_to(ROOT)


def read_quality(source_root: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for category, path in QUALITY_SOURCES.items():
        quality = pd.read_csv(staged_path(source_root, path))
        if "record_type" in quality:
            quality = quality[quality["record_type"].eq("FILE")]
        for record in quality.to_dict("records"):
            source = str(record.get("source_file", "")).replace("\\", "/")
            if source:
                rows[source] = {
                    "existing_category": category,
                    "quality_severity": record.get("severity", ""),
                    "quality_status": record.get("status", ""),
                    "quality_detail": record.get("detail", ""),
                }

    documents = pd.read_csv(source_root / "output_excel_documents_clean" / "quality_report.csv")
    documents = documents[documents["record_type"].eq("FILE")]
    for record in documents.to_dict("records"):
        source = str(record["source_file"]).replace("\\", "/")
        role = record.get("document_role", "")
        rows[source] = {
            "existing_category": "template_library" if role == "template_library" else "excel_documents",
            "quality_severity": record.get("severity", ""),
            "quality_status": record.get("status", ""),
            "quality_detail": record.get("detail", ""),
        }
    return rows


def source_profile(
    source_file: str,
    frame: pd.DataFrame,
    meta: dict,
    clean_output: str,
    quality: dict[str, dict],
) -> dict:
    source_rows = frame[frame["source_file"].eq(source_file)]
    dimension_column = "region_code" if "region_code" in source_rows else "entity_code" if "entity_code" in source_rows else None
    schema_columns = [
        column
        for column in ("schema_version", "scope_version", "statistical_scope_version", "accounting_basis_version")
        if column in source_rows
    ]
    result = {
        "source_file": source_file,
        "source_file_hash": source_hash(source_rows),
        "source_format": Path(source_file).suffix.lower().lstrip("."),
        "existing_category": meta["existing_category"],
        "content_type_code": meta["content_type_code"],
        "content_type_name": meta["content_type_name"],
        "domain_code": meta["domain_code"],
        "domain_name": meta["domain_name"],
        "topic_code": meta["topic_code"],
        "topic_name": meta["topic_name"],
        "dataset_family": meta["dataset_family"],
        "document_function": meta["document_function"],
        "frequency": meta["frequency"],
        "structured_level": "fact_table",
        "classification_basis": "解析后的事实表字段与实际指标内容",
        "evidence_summary": f"metrics={pipe(source_rows['metric_name'])}; sheets={pipe(source_rows['source_sheet'])}",
        "source_sheet_names": pipe(source_rows["source_sheet"]),
        "record_count": len(source_rows),
        "metric_count": source_rows["metric_code"].nunique(),
        "dimension_count": source_rows[dimension_column].nunique() if dimension_column else 0,
        "period_start": str(source_rows["period_start"].min()),
        "period_end": str(source_rows["period_end"].max()),
        "schema_versions": "|".join(f"{column}={pipe(source_rows[column])}" for column in schema_columns),
        "clean_output": clean_output,
        "default_qa_eligible": True,
    }
    result.update(quality[source_file])
    return result


def classify_document(text: str) -> tuple[dict, list[str]]:
    for rule in DOCUMENT_RULES:
        matched = [keyword for keyword in rule["all"] if keyword in text]
        if len(matched) == len(rule["all"]):
            return rule, matched
    raise ValueError("未匹配到基于实际内容的文档分类规则")


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False, compression="zstd")


def build(input_dir: Path, output_dir: Path, source_root: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"输出目录已存在且非空，拒绝覆盖：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    quality = read_quality(source_root)
    catalog: list[dict] = []
    output_inventory: list[dict] = []

    for family, config in FACT_DATASETS.items():
        source_frame = pd.read_parquet(staged_path(source_root, config["source"]))
        meta = {
            **config,
            "dataset_family": family,
            "content_type_code": "statistical_data",
            "content_type_name": "已发布统计数据",
            "document_function": "统计数据发布",
        }
        classified = add_classification_columns(source_frame, meta)
        relative_output = f"facts/{family}.parquet"
        write_parquet(classified, output_dir / relative_output)
        output_inventory.append({"output_file": relative_output, "row_count": len(classified), "source_file_count": classified["source_file"].nunique()})
        for source_file in sorted(source_frame["source_file"].unique()):
            catalog.append(source_profile(source_file, source_frame, meta, relative_output, quality))

    regulatory = pd.read_parquet(source_root / "output_regulatory_tables_clean" / "regulatory_facts.parquet")
    for family, group in regulatory.groupby("dataset_id", sort=True):
        if family not in REGULATORY_DATASETS:
            raise ValueError(f"未知监管事实数据集：{family}")
        domain_code, domain_name, topic_code, topic_name, frequency = REGULATORY_DATASETS[family]
        meta = {
            "dataset_family": family,
            "existing_category": "regulatory_tables",
            "domain_code": domain_code,
            "domain_name": domain_name,
            "topic_code": topic_code,
            "topic_name": topic_name,
            "frequency": frequency,
            "content_type_code": "statistical_data",
            "content_type_name": "已发布统计数据",
            "document_function": "监管统计数据发布",
        }
        classified = add_classification_columns(group, meta)
        relative_output = f"facts/{family}.parquet"
        write_parquet(classified, output_dir / relative_output)
        output_inventory.append({"output_file": relative_output, "row_count": len(classified), "source_file_count": classified["source_file"].nunique()})
        for source_file in sorted(group["source_file"].unique()):
            catalog.append(source_profile(source_file, group, meta, relative_output, quality))

    dictionary_sources = {
        "release_schedule": source_root / "output_regulatory_tables_clean" / "release_schedule.parquet",
        "metric_definitions": source_root / "output_regulatory_tables_clean" / "metric_definitions.parquet",
        "institution_scopes": source_root / "output_regulatory_tables_clean" / "institution_scopes.parquet",
    }
    dictionary_frames: dict[str, pd.DataFrame] = {}
    source_dictionary_tables: dict[str, set[str]] = defaultdict(set)
    for table_name, source_path in dictionary_sources.items():
        frame = pd.read_parquet(source_path)
        dictionary_frames[table_name] = frame
        for source_file in frame["source_file"].unique():
            source_dictionary_tables[source_file].add(table_name)
        classified = frame.copy()
        classified.insert(0, "dataset_family", table_name)
        classified.insert(0, "topic_name", "监管统计元数据")
        classified.insert(0, "topic_code", "regulatory_statistical_metadata")
        classified.insert(0, "domain_name", "银行业")
        classified.insert(0, "domain_code", "banking")
        classified.insert(0, "content_type", "metadata_dictionary")
        relative_output = f"dictionaries/{table_name}.parquet"
        write_parquet(classified, output_dir / relative_output)
        output_inventory.append({"output_file": relative_output, "row_count": len(classified), "source_file_count": classified["source_file"].nunique()})

    for source_file, tables in sorted(source_dictionary_tables.items()):
        family = "regulatory_schedule_and_dictionary" if "release_schedule" in tables else "regulatory_metric_scope_dictionary"
        frames = [frame[frame["source_file"].eq(source_file)] for name, frame in dictionary_frames.items() if name in tables]
        record_count = sum(len(frame) for frame in frames)
        sheets = sorted({str(value) for frame in frames for value in frame["source_sheet"].dropna().unique()})
        record = {
            "source_file": source_file,
            "source_file_hash": source_hash(pd.concat(frames, ignore_index=True)),
            "source_format": Path(source_file).suffix.lower().lstrip("."),
            "existing_category": "regulatory_tables",
            "content_type_code": "metadata_dictionary",
            "content_type_name": "监管元数据与字典",
            "domain_code": "banking",
            "domain_name": "银行业",
            "topic_code": "regulatory_statistical_metadata",
            "topic_name": "监管统计发布日程、指标解释和机构范围",
            "dataset_family": family,
            "document_function": "监管统计口径说明",
            "frequency": "annual",
            "structured_level": "dictionary_table",
            "classification_basis": "实际识别出的发布日程/指标解释/机构范围表头",
            "evidence_summary": "content_tables=" + "|".join(sorted(tables)),
            "source_sheet_names": "|".join(sheets),
            "record_count": record_count,
            "metric_count": 0,
            "dimension_count": 0,
            "period_start": "",
            "period_end": "",
            "schema_versions": "",
            "clean_output": "|".join(f"dictionaries/{name}.parquet" for name in sorted(tables)),
            "default_qa_eligible": True,
        }
        record.update(quality[source_file])
        catalog.append(record)

    document_quality = pd.read_csv(source_root / "output_excel_documents_clean" / "quality_report.csv")
    document_quality = document_quality[document_quality["record_type"].eq("FILE")]
    template_rows = pd.read_parquet(source_root / "output_excel_documents_clean" / "template_rows.parquet")
    workbook_rows = pd.read_parquet(source_root / "output_excel_documents_clean" / "workbook_rows.parquet")
    for quality_row in document_quality.to_dict("records"):
        source_file = quality_row["source_file"]
        role = quality_row["document_role"]
        source_rows = template_rows[template_rows["source_file"].eq(source_file)] if role == "template_library" else workbook_rows[workbook_rows["source_file"].eq(source_file)]
        if source_rows.empty:
            raise ValueError(f"通用文档没有抽取到行：{source_file}")
        sheet_names = sorted(source_rows["source_sheet"].dropna().astype(str).unique())
        sample_text = "\n".join(source_rows["row_text"].dropna().astype(str).head(300))
        content_text = "\n".join(sheet_names) + "\n" + sample_text
        try:
            rule, matched = classify_document(content_text)
        except ValueError as exc:
            raise ValueError(f"{source_file}: {exc}") from exc
        content_type_code, content_type_name = rule["content_type"]
        domain_code, domain_name = rule["domain"]
        topic_code, topic_name = rule["topic"]
        target_dir = "references" if content_type_code in {"rule_table", "reference_list"} else "templates"
        relative_output = f"{target_dir}/{rule['family']}_rows.parquet"
        classified = source_rows.copy()
        for name, value in reversed(
            [
                ("content_type", content_type_code),
                ("domain_code", domain_code),
                ("domain_name", domain_name),
                ("topic_code", topic_code),
                ("topic_name", topic_name),
                ("dataset_family", rule["family"]),
            ]
        ):
            classified.insert(0, name, value)
        write_parquet(classified, output_dir / relative_output)
        output_inventory.append({"output_file": relative_output, "row_count": len(classified), "source_file_count": 1})
        record = {
            "source_file": source_file,
            "source_file_hash": source_hash(source_rows),
            "source_format": Path(source_file).suffix.lower().lstrip("."),
            "existing_category": "template_library" if role == "template_library" else "excel_documents",
            "content_type_code": content_type_code,
            "content_type_name": content_type_name,
            "domain_code": domain_code,
            "domain_name": domain_name,
            "topic_code": topic_code,
            "topic_name": topic_name,
            "dataset_family": rule["family"],
            "document_function": rule["function"],
            "frequency": "template" if role == "template_library" else "not_applicable",
            "structured_level": "row_document",
            "classification_basis": "实际工作表名称与行文本关键词",
            "evidence_summary": "matched=" + "|".join(matched),
            "source_sheet_names": "|".join(sheet_names),
            "record_count": len(source_rows),
            "metric_count": 0,
            "dimension_count": 0,
            "period_start": "",
            "period_end": "",
            "schema_versions": "",
            "clean_output": relative_output,
            "default_qa_eligible": bool(quality_row["default_qa_eligible"]),
        }
        record.update(quality[source_file])
        catalog.append(record)

    catalog_frame = pd.DataFrame(catalog).sort_values("source_file").reset_index(drop=True)
    discovered = {
        path.name
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".xls", ".xlsx"} and not path.name.startswith("~$")
    }
    classified_sources = set(catalog_frame["source_file"])
    duplicates = catalog_frame["source_file"].duplicated().sum()
    missing = sorted(discovered - classified_sources)
    unexpected = sorted(classified_sources - discovered)
    if duplicates or missing or unexpected:
        raise ValueError(f"分类覆盖失败 duplicates={duplicates} missing={missing[:5]} unexpected={unexpected[:5]}")
    if len(catalog_frame) != 389:
        raise ValueError(f"源文件数量异常：{len(catalog_frame)}，预期 389")

    catalog_path = output_dir / "source_catalog.parquet"
    write_parquet(catalog_frame, catalog_path)
    family_summary = (
        catalog_frame.groupby(
            [
                "content_type_code",
                "content_type_name",
                "domain_code",
                "domain_name",
                "topic_code",
                "topic_name",
                "dataset_family",
                "document_function",
            ],
            dropna=False,
            sort=True,
        )
        .agg(source_file_count=("source_file", "nunique"), cleaned_record_count=("record_count", "sum"))
        .reset_index()
    )
    write_parquet(family_summary, output_dir / "family_summary.parquet")
    write_parquet(pd.DataFrame(output_inventory), output_dir / "output_inventory.parquet")

    summary = {
        "source_file_count": len(discovered),
        "classified_source_file_count": len(catalog_frame),
        "unclassified_source_file_count": len(missing),
        "duplicate_classification_count": int(duplicates),
        "content_type_counts": catalog_frame["content_type_name"].value_counts().sort_index().to_dict(),
        "domain_counts": catalog_frame["domain_name"].value_counts().sort_index().to_dict(),
        "dataset_family_count": int(catalog_frame["dataset_family"].nunique()),
        "dataset_family_counts": catalog_frame["dataset_family"].value_counts().sort_index().to_dict(),
        "quality_severity_counts": catalog_frame["quality_severity"].value_counts().sort_index().to_dict(),
        "output_file_count": len(output_inventory) + 5,
        "canonical_format": "parquet",
        "classification_basis": "structured fact content, detected dictionary tables, and worksheet/row text fingerprints",
    }
    with (output_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    with (output_dir / "classification_rules.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "principles": [
                    "原文件名仅作为源文件标识，不作为文档细分类的唯一依据",
                    "已结构化统计表依据解析后的 dataset_id、指标和维度内容分类",
                    "监管元数据依据实际识别出的表头类型分类",
                    "模板和参考文档依据工作表名称及行文本关键词分类",
                    "模板、参考资料和已发布统计事实分层保存",
                ],
                "document_rules": DOCUMENT_RULES,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="按实际内容对已清洗 Excel 进行细分类并生成新的清洗文件")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=ROOT,
        help="底层清洗器中间产物所在目录；一键流程会传入临时目录",
    )
    args = parser.parse_args()
    build(args.input_dir.resolve(), args.output_dir.resolve(), args.source_root.resolve())


if __name__ == "__main__":
    main()
