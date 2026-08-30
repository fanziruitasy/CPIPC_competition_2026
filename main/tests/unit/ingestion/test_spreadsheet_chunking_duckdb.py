"""验证 Excel 说明分块与 DuckDB 精确事实查询。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import openpyxl

from trusted_rag.domain.enums import QueryIntent, QueryRoute, StructuredOperationType
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import SpreadsheetExtractionResult
from trusted_rag.domain.ports import SnapshotReference
from trusted_rag.domain.query import QueryFilters, QueryPlan, StructuredOperation
from trusted_rag.ingestion.source_registry import SourceRegistrar
from trusted_rag.ingestion.spreadsheets.fact_extractor import UnifiedSpreadsheetExtractor
from trusted_rag.ingestion.spreadsheets.spreadsheet_chunker import chunk_spreadsheet_facts
from trusted_rag.storage.duckdb_fact_store import DuckDbFactStore


def test_spreadsheet_chunks_keep_fact_and_cell_relations(tmp_path: Path) -> None:
    """Excel 说明分块必须关联统一事实、证据和工作表单元格。"""
    result = _extract_sample(tmp_path)

    chunking = chunk_spreadsheet_facts(
        result.document,
        result.elements,
        result.facts,
        result.evidence,
        max_facts_per_chunk=3,
    )

    assert len(chunking.chunks) == 3
    first = chunking.chunks[0]
    assert first.heading_path == ["原保险保费"]
    assert first.relations.table_fact_ids == [fact.fact_id for fact in result.facts[:3]]
    assert first.evidence_ids == [fact.evidence_id for fact in result.facts[:3]]
    assert first.locations[0].cell_range == "B3"
    assert "原保险保费收入合计" in first.display_text
    assert first.retrieval.periods == ["2024-03-31"]


def test_duckdb_lookup_returns_exact_value_and_cell_evidence(tmp_path: Path) -> None:
    """参数化精确查询必须返回原始数值、单位和同一单元格证据。"""
    result = _extract_sample(tmp_path)
    snapshot = SnapshotReference(
        knowledge_base_id="kb_excel",
        snapshot_id="snapshot_001",
        qdrant_collection="trusted_rag_kb_excel_snapshot_001",
        duckdb_uri="kb_excel/snapshot_001.duckdb",
    )
    store = DuckDbFactStore(tmp_path / "databases")
    store.replace_snapshot(snapshot, result.facts)
    plan = QueryPlan(
        query_plan_id=stable_id("query_plan", "trace-001", "全国保费合计"),
        trace_id="trace-001",
        knowledge_base_id="kb_excel",
        original_query="2024年3月全国原保险保费收入合计是多少？",
        normalized_query="2024年3月全国原保险保费收入合计",
        semantic_queries=["全国 原保险保费收入合计 2024-03-31"],
        route=QueryRoute.STRUCTURED,
        intent=QueryIntent.STATISTIC_LOOKUP,
        structured_operations=[
            StructuredOperation(
                operation=StructuredOperationType.LOOKUP,
                metric="premium_total",
                entity="CN",
                periods=["2024-03-31"],
                unit="CNY_100M",
            )
        ],
        planner_mode="rules_only",
        planner_reasons=["命中明确指标、机构和期间"],
        rule_confidence=1.0,
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    evidence = store.query(snapshot, plan)

    assert len(evidence) == 1
    assert evidence[0].source_value == "100"
    assert evidence[0].unit == "CNY_100M"
    assert evidence[0].location.sheet_name == "原保险保费"
    assert evidence[0].location.cell_range == "B3"
    assert evidence[0].evidence_id == result.facts[0].evidence_id

    scoped_plan = plan.model_copy(
        update={
            "filters": QueryFilters(source_ids=[result.document.source_id]),
            "structured_operations": [
                plan.structured_operations[0].model_copy(update={"unit": "亿元"})
            ],
        }
    )
    assert len(store.query(snapshot, scoped_plan)) == 1

    missing_source_plan = scoped_plan.model_copy(
        update={
            "filters": QueryFilters(source_ids=["source_1234567890abcdef12345678"]),
        }
    )
    assert store.query(snapshot, missing_source_plan) == []


def _extract_sample(tmp_path: Path) -> SpreadsheetExtractionResult:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    workbook_path = source_root / "001_2024年3月全国各地区原保险保费收入情况表.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "原保险保费"
    sheet.append(["2024年3月全国各地区原保险保费收入情况表（单位：亿元）"])
    sheet.append(["地区", "合计", "财产保险", "寿险", "意外险", "健康险"])
    sheet.append(["全国", 100, 20, 50, 10, 20])
    workbook.save(workbook_path)
    workbook.close()
    source = SourceRegistrar(source_root).register(
        workbook_path,
        knowledge_base_id="kb_excel",
        run_id="registration-run",
    )
    return UnifiedSpreadsheetExtractor(source_root).extract(source, run_id="excel-run")
