"""把专用 Excel 清洗结果映射为统一表格事实和单元格证据。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trusted_rag.domain.common import (
    ArtifactReferences,
    LineageMetadata,
    QualityMetadata,
    SourceLocation,
)
from trusted_rag.domain.enums import (
    ElementType,
    EvidenceType,
    FormulaCacheStatus,
    QualityStatus,
    SourceFormat,
    SourceProfile,
    ValueType,
)
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import (
    DocumentRecord,
    ElementRecord,
    EvidenceUnit,
    SourceDocument,
    SpreadsheetExtractionResult,
    TableFact,
)
from trusted_rag.ingestion.spreadsheets.classification import (
    WorkbookClassification,
    classify_workbook,
)
from trusted_rag.ingestion.spreadsheets.cleaners import (
    excel_documents,
    insurance_industry,
    life_insurance,
    property_insurance,
    region_premium,
    regulatory_tables,
)


class UnifiedSpreadsheetExtractor:
    """复用专用清洗算法并输出稳定的统一电子表格契约。"""

    def __init__(self, source_root: Path, *, extractor_version: str = "0.01") -> None:
        """初始化统一电子表格抽取器。

        :param source_root: `SourceDocument.relative_path` 所在的只读根目录。
        :param extractor_version: 当前适配器版本。
        :return: 无。
        """
        self.source_root = source_root.resolve()
        self.extractor_version = extractor_version

    def extract(
        self,
        source: SourceDocument,
        *,
        run_id: str,
    ) -> SpreadsheetExtractionResult:
        """抽取一份 XLSX 的事实与证据并映射统一契约。

        :param source: 原生或由 XLS 转换得到的 XLSX 来源。
        :param run_id: 当前知识构建运行标识。
        :return: 文档记录、事实和一一对应的单元格证据。
        :raises ValueError: 来源格式错误、路径逃逸或专用解析器不支持时抛出。
        """
        if source.source_format is not SourceFormat.XLSX:
            raise ValueError("统一电子表格抽取器只接受 XLSX；XLS 必须先转换。")
        source_path = (self.source_root / source.relative_path).resolve()
        try:
            source_path.relative_to(self.source_root)
        except ValueError as exc:
            raise ValueError("XLSX 来源路径逃逸了来源根目录。") from exc
        if not source_path.is_file():
            raise FileNotFoundError(f"XLSX 来源不存在：{source.relative_path}")

        classification = classify_workbook(Path(source.original_file_name))
        raw_facts = self._extract_raw_facts(source_path, source, classification)
        created_at = datetime.now(UTC)
        document_id = stable_id("document", source.source_id, "spreadsheet")
        lineage = LineageMetadata(
            run_id=run_id,
            producer="unified_spreadsheet_extractor",
            producer_version=self.extractor_version,
            input_ids=[source.source_id],
            created_at=created_at,
        )
        raw_cells, raw_rows, _, row_quality = excel_documents.parse_file(
            source_path,
            self.source_root,
        )
        elements, row_evidence = _map_rows(
            raw_rows,
            source=source,
            document_id=document_id,
            lineage=lineage,
        )
        facts: list[TableFact] = []
        evidence: list[EvidenceUnit] = list(row_evidence)
        for raw in raw_facts:
            fact, item_evidence = _map_fact(
                raw,
                source=source,
                document_id=document_id,
                dataset_id=classification.dataset_id,
                lineage=lineage,
            )
            facts.append(fact)
            evidence.append(item_evidence)

        del raw_cells

        review_reasons = sorted(
            {
                reason
                for fact in facts
                for reason in fact.quality.review_reasons
            }
        )
        if row_quality["formula_cache_missing_count"]:
            review_reasons.append(
                f"{row_quality['formula_cache_missing_count']} 个公式缺少缓存值"
            )
        document_quality = QualityMetadata(
            status=QualityStatus.REQUIRES_REVIEW if review_reasons else QualityStatus.PASSED,
            flags=sorted({flag for fact in facts for flag in fact.quality.flags}),
            requires_manual_review=bool(review_reasons),
            review_reasons=review_reasons,
        )
        document = DocumentRecord(
            document_id=document_id,
            source_id=source.source_id,
            knowledge_base_id=source.knowledge_base_id,
            title=Path(source.original_file_name).stem,
            source_profile=SourceProfile.EXCEL,
            parser_name="zry_specialized_excel_cleaners",
            parser_version=self.extractor_version,
            parsing_config_version="v0.01",
            artifacts=ArtifactReferences(
                structured_json_uri=f"normalized/{source.source_id}/spreadsheet.json",
            ),
            quality=document_quality,
            lineage=lineage,
        )
        return SpreadsheetExtractionResult(
            document=document,
            elements=elements,
            facts=facts,
            evidence=evidence,
        )

    def _extract_raw_facts(
        self,
        source_path: Path,
        source: SourceDocument,
        classification: WorkbookClassification,
    ) -> list[dict[str, Any]]:
        dataset_id = classification.dataset_id
        year = classification.year
        month = classification.month
        if dataset_id == "regional_premium_monthly" and year and month:
            facts, _ = region_premium.parse_file(
                source_path,
                year,
                month,
                self.source_root,
                source_file_label=source.original_file_name,
            )
            return facts
        if dataset_id == "life_insurance_monthly" and year and month:
            _configure_life_cleaner()
            facts, _ = life_insurance.parse_file(source_path, year, month, self.source_root)
            return facts
        if dataset_id == "property_insurance_monthly" and year and month:
            property_insurance.configure_cleaner()
            facts, _ = property_insurance.parse_file(source_path, year, month, self.source_root)
            return facts
        if dataset_id == "insurance_industry_monthly" and year and month:
            insurance_industry.configure_cleaner()
            facts, _ = insurance_industry.parse_file(source_path, year, month, self.source_root)
            return facts
        if dataset_id == "insurance_funds_investment" and year:
            return regulatory_tables.parse_funds(
                source_path,
                self.source_root,
                dataset_id,
                year,
                source.original_file_name,
            )
        if dataset_id in regulatory_tables.PARSERS and year:
            parser = cast(
                Callable[[Path, Path, str, int], list[dict[str, Any]]],
                regulatory_tables.PARSERS[dataset_id],
            )
            return parser(
                source_path,
                self.source_root,
                dataset_id,
                year,
            )
        if classification.role in {"dictionary", "template", "reference"}:
            return []
        raise ValueError(f"没有可用的专用事实解析器：{dataset_id}")


def _map_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    source: SourceDocument,
    document_id: str,
    lineage: LineageMetadata,
) -> tuple[list[ElementRecord], list[EvidenceUnit]]:
    elements: list[ElementRecord] = []
    evidence: list[EvidenceUnit] = []
    for index, row in enumerate(rows):
        sheet_name = str(row.get("source_sheet") or "工作表")
        cell_range = str(row.get("source_range") or "A1:A1")
        display_text = str(row.get("row_text") or "").strip()
        if not display_text:
            continue
        element_id = stable_id("element", source.source_id, sheet_name, cell_range)
        evidence_id = stable_id("evidence", element_id, "row")
        location = SourceLocation(sheet_name=sheet_name, cell_range=cell_range)
        elements.append(
            ElementRecord(
                element_id=element_id,
                document_id=document_id,
                source_id=source.source_id,
                element_index=index,
                element_type=ElementType.SHEET_TEXT,
                display_text=display_text,
                location=location,
                lineage=lineage,
            )
        )
        evidence.append(
            EvidenceUnit(
                evidence_id=evidence_id,
                source_id=source.source_id,
                document_id=document_id,
                evidence_type=EvidenceType.CELL,
                excerpt=display_text,
                location=location,
                lineage=lineage,
            )
        )
    return elements, evidence


def write_facts_parquet(
    facts: Sequence[TableFact],
    output_path: Path,
) -> Path:
    """原子写入可由 DuckDB 直接读取的统一事实 Parquet。

    :param facts: 已通过统一契约校验的表格事实。
    :param output_path: 目标 Parquet 文件；已存在时原子替换。
    :return: 写入完成的绝对文件路径。
    """
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    rows = [_fact_parquet_row(fact) for fact in facts]
    table = pa.Table.from_pylist(rows, schema=_FACT_PARQUET_SCHEMA)
    try:
        pq.write_table(table, temporary, compression="zstd")
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output_path


def _map_fact(
    raw: Mapping[str, Any],
    *,
    source: SourceDocument,
    document_id: str,
    dataset_id: str,
    lineage: LineageMetadata,
) -> tuple[TableFact, EvidenceUnit]:
    sheet_name = str(raw.get("source_sheet") or "工作表")
    source_cell = str(raw.get("source_cell") or "A1")
    metric_code = _optional_text(raw.get("metric_code"))
    metric_name = _optional_text(raw.get("metric_name") or raw.get("metric_name_raw"))
    entity_code = _optional_text(raw.get("entity_code") or raw.get("region_code"))
    entity_name = _optional_text(raw.get("entity_name") or raw.get("region_name"))
    product_name = _optional_text(raw.get("product_line_name"))
    if product_name and product_name != "合计":
        entity_name = f"{entity_name or ''}/{product_name}".strip("/")
        entity_code = f"{entity_code or ''}:{raw.get('product_line') or product_name}".strip(":")
    period_start = _optional_date(raw.get("period_start"))
    period_end = _optional_date(raw.get("period_end"))
    raw_value = _raw_value(raw)
    normalized_source = raw.get("value")
    if normalized_source is None:
        normalized_source = raw.get("value_decimal")
    normalized_value = _normalized_value(normalized_source)
    unit = _optional_text(raw.get("unit") or raw.get("unit_raw"))
    formula = _optional_text(raw.get("formula_raw"))
    formula_cache_status = (
        FormulaCacheStatus.MISSING
        if formula and normalized_value is None
        else FormulaCacheStatus.CACHED
        if formula
        else FormulaCacheStatus.NOT_FORMULA
    )
    flags = [
        flag
        for flag in str(raw.get("quality_flags") or "").split("|")
        if flag
    ]
    if formula_cache_status is FormulaCacheStatus.MISSING and "FORMULA_CACHE_MISSING" not in flags:
        flags.append("FORMULA_CACHE_MISSING")
    review_reasons = ["公式缺少缓存值"] if formula_cache_status is FormulaCacheStatus.MISSING else []
    quality = QualityMetadata(
        status=QualityStatus.REQUIRES_REVIEW if review_reasons else QualityStatus.PASSED,
        flags=flags,
        requires_manual_review=bool(review_reasons),
        review_reasons=review_reasons,
    )
    table_id = stable_id("table", source.source_id, dataset_id, sheet_name)
    fact_id = stable_id(
        "fact",
        source.source_id,
        sheet_name,
        source_cell,
        metric_code,
        entity_code,
        period_end,
    )
    evidence_id = stable_id("evidence", fact_id, "cell")
    location = SourceLocation(
        sheet_name=sheet_name,
        cell_range=source_cell,
        table_id=table_id,
    )
    value_type = _value_type(normalized_value, unit)
    fact = TableFact(
        fact_id=fact_id,
        source_id=source.source_id,
        document_id=document_id,
        table_id=table_id,
        metric_code=metric_code,
        metric_name=metric_name,
        entity_code=entity_code,
        entity_name=entity_name,
        period_start=period_start,
        period_end=period_end,
        period_basis=_optional_text(raw.get("period_basis")),
        raw_value=raw_value,
        normalized_value=normalized_value,
        value_type=value_type,
        unit=unit,
        scale=_optional_text(raw.get("unit_raw")),
        statistical_scope=_optional_text(
            raw.get("statistical_scope_version") or raw.get("scope") or raw.get("scope_version")
        ),
        accounting_basis=_optional_text(raw.get("accounting_basis_version")),
        is_formula=formula is not None,
        formula=formula,
        formula_cache_status=formula_cache_status,
        evidence_id=evidence_id,
        location=location,
        quality=quality,
        lineage=lineage,
    )
    excerpt_parts = [part for part in (metric_name, entity_name, _period_text(period_end)) if part]
    value_text = raw_value if raw_value is not None else "缺少缓存值"
    excerpt = "；".join(excerpt_parts) + f"：{value_text}{unit or ''}"
    evidence = EvidenceUnit(
        evidence_id=evidence_id,
        source_id=source.source_id,
        document_id=document_id,
        evidence_type=EvidenceType.CELL,
        excerpt=excerpt,
        source_value=raw_value,
        unit=unit,
        location=location,
        quality=quality,
        lineage=lineage,
    )
    return fact, evidence


def _fact_parquet_row(fact: TableFact) -> dict[str, Any]:
    return {
        "schema_version": fact.schema_version,
        "fact_id": fact.fact_id,
        "source_id": fact.source_id,
        "document_id": fact.document_id,
        "table_id": fact.table_id,
        "metric_code": fact.metric_code,
        "metric_name": fact.metric_name,
        "entity_code": fact.entity_code,
        "entity_name": fact.entity_name,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "period_basis": fact.period_basis,
        "raw_value": fact.raw_value,
        "normalized_value": fact.normalized_value,
        "value_type": fact.value_type.value,
        "unit": fact.unit,
        "scale": fact.scale,
        "statistical_scope": fact.statistical_scope,
        "accounting_basis": fact.accounting_basis,
        "is_formula": fact.is_formula,
        "formula": fact.formula,
        "formula_cache_status": fact.formula_cache_status.value,
        "evidence_id": fact.evidence_id,
        "sheet_name": fact.location.sheet_name,
        "cell_range": fact.location.cell_range,
        "quality_status": fact.quality.status.value,
        "quality_flags": fact.quality.flags,
        "requires_manual_review": fact.quality.requires_manual_review,
        "review_reasons": fact.quality.review_reasons,
        "run_id": fact.lineage.run_id,
        "producer": fact.lineage.producer,
        "producer_version": fact.lineage.producer_version,
        "created_at": fact.lineage.created_at,
    }


def _raw_value(raw: Mapping[str, Any]) -> str | None:
    for key in ("value_raw", "value_display", "value"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _normalized_value(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return format(Decimal(str(value)), "f")


def _optional_text(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def _optional_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _period_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _value_type(normalized_value: str | None, unit: str | None) -> ValueType:
    if normalized_value is None:
        return ValueType.BLANK
    if unit and (unit == "%" or "percent" in unit.lower()):
        return ValueType.PERCENTAGE
    number = Decimal(normalized_value)
    return ValueType.INTEGER if number == number.to_integral_value() else ValueType.DECIMAL


def _configure_life_cleaner() -> None:
    for name, value in _LIFE_CONFIGURATION.items():
        setattr(life_insurance, name, value)


_LIFE_CONFIGURATION: dict[str, Any] = {
    "TARGET_RE": life_insurance.TARGET_RE,
    "OUTPUT_NAMES": life_insurance.OUTPUT_NAMES,
    "METRICS": life_insurance.METRICS,
    "PRODUCT_LINES": life_insurance.PRODUCT_LINES,
    "EXPECTED_FACTS": life_insurance.EXPECTED_FACTS,
    "EXPECTED_KEYS": life_insurance.EXPECTED_KEYS,
    "METRIC_BY_ALIAS": life_insurance.METRIC_BY_ALIAS,
    "PRODUCT_BY_ALIAS": life_insurance.PRODUCT_BY_ALIAS,
    "schema_version": life_insurance.schema_version,
    "identity_check": life_insurance.identity_check,
    "parse_file": life_insurance.parse_file,
    "self_check": life_insurance.self_check,
}


_FACT_PARQUET_SCHEMA = pa.schema(
    [
        ("schema_version", pa.string()),
        ("fact_id", pa.string()),
        ("source_id", pa.string()),
        ("document_id", pa.string()),
        ("table_id", pa.string()),
        ("metric_code", pa.string()),
        ("metric_name", pa.string()),
        ("entity_code", pa.string()),
        ("entity_name", pa.string()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("period_basis", pa.string()),
        ("raw_value", pa.string()),
        ("normalized_value", pa.string()),
        ("value_type", pa.string()),
        ("unit", pa.string()),
        ("scale", pa.string()),
        ("statistical_scope", pa.string()),
        ("accounting_basis", pa.string()),
        ("is_formula", pa.bool_()),
        ("formula", pa.string()),
        ("formula_cache_status", pa.string()),
        ("evidence_id", pa.string()),
        ("sheet_name", pa.string()),
        ("cell_range", pa.string()),
        ("quality_status", pa.string()),
        ("quality_flags", pa.list_(pa.string())),
        ("requires_manual_review", pa.bool_()),
        ("review_reasons", pa.list_(pa.string())),
        ("run_id", pa.string()),
        ("producer", pa.string()),
        ("producer_version", pa.string()),
        ("created_at", pa.timestamp("us", tz="UTC")),
    ]
)
