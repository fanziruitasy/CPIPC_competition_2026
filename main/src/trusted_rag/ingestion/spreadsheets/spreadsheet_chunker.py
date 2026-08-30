"""把 Excel 事实按工作表和指标组织为可检索说明分块。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from trusted_rag.domain.common import QualityMetadata
from trusted_rag.domain.enums import ChunkContentType
from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import (
    ChunkingResult,
    ChunkRecord,
    ChunkRelations,
    DocumentRecord,
    ElementRecord,
    EvidenceUnit,
    RetrievalMetadata,
    TableFact,
)


def chunk_spreadsheet_facts(
    document: DocumentRecord,
    elements: Sequence[ElementRecord],
    facts: Sequence[TableFact],
    evidence: Sequence[EvidenceUnit],
    *,
    max_facts_per_chunk: int = 20,
) -> ChunkingResult:
    """生成保留精确事实引用但不替代 DuckDB 取数的说明分块。

    :param document: 电子表格文件级记录。
    :param elements: 由抽取器生成的工作表元素。
    :param facts: 已标准化的表格事实。
    :param evidence: 与事实对应的单元格证据。
    :param max_facts_per_chunk: 每个检索分块最多包含的事实数。
    :return: Excel 说明分块和原始单元格证据。
    :raises ValueError: 分块大小无效或事实缺少工作表元素时抛出。
    """
    if max_facts_per_chunk < 1:
        raise ValueError("max_facts_per_chunk 必须大于零。")
    first_element_by_sheet = {
        element.location.sheet_name: element
        for element in elements
        if element.location.sheet_name
    }
    grouped: dict[str, list[TableFact]] = defaultdict(list)
    for fact in facts:
        grouped[fact.location.sheet_name or "工作表"].append(fact)

    chunks: list[ChunkRecord] = []
    for sheet_name in sorted(grouped):
        element = first_element_by_sheet.get(sheet_name)
        if element is None:
            raise ValueError(f"事实缺少对应工作表元素：{sheet_name}")
        ordered = grouped[sheet_name]
        for offset in range(0, len(ordered), max_facts_per_chunk):
            batch = ordered[offset : offset + max_facts_per_chunk]
            lines = [_fact_line(fact) for fact in batch]
            display_text = f"工作表：{sheet_name}\n" + "\n".join(lines)
            chunk_id = stable_id(
                "chunk",
                document.document_id,
                sheet_name,
                [fact.fact_id for fact in batch],
            )
            chunks.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    content_sha256=canonical_sha256(display_text),
                    document_id=document.document_id,
                    source_id=document.source_id,
                    chunk_index=len(chunks),
                    content_type=ChunkContentType.TABLE_ROWS,
                    display_text=display_text,
                    embedding_text=display_text,
                    bm25_text=display_text,
                    heading_path=[sheet_name],
                    token_count=max(1, len(display_text) // 2),
                    source_element_ids=[element.element_id],
                    evidence_ids=[fact.evidence_id for fact in batch],
                    locations=[fact.location for fact in batch],
                    retrieval=RetrievalMetadata(
                        indicator_names=list(
                            dict.fromkeys(fact.metric_name for fact in batch if fact.metric_name)
                        ),
                        organization_names=list(
                            dict.fromkeys(fact.entity_name for fact in batch if fact.entity_name)
                        ),
                        periods=list(
                            dict.fromkeys(
                                fact.period_end.isoformat()
                                for fact in batch
                                if fact.period_end
                            )
                        ),
                        units=list(dict.fromkeys(fact.unit for fact in batch if fact.unit)),
                    ),
                    relations=ChunkRelations(table_fact_ids=[fact.fact_id for fact in batch]),
                    quality=_combined_quality(batch),
                    lineage=document.lineage,
                )
            )
    row_evidence: dict[tuple[str, str], EvidenceUnit] = {
        (
            item.location.sheet_name,
            item.location.cell_range,
        ): item
        for item in evidence
        if item.location.sheet_name and item.location.cell_range
    }
    elements_by_sheet: dict[str, list[ElementRecord]] = defaultdict(list)
    for element in elements:
        elements_by_sheet[element.location.sheet_name or "工作表"].append(element)
    for sheet_name in sorted(elements_by_sheet):
        ordered_elements = sorted(
            elements_by_sheet[sheet_name],
            key=lambda element: element.element_index,
        )
        for offset in range(0, len(ordered_elements), max_facts_per_chunk):
            row_batch = ordered_elements[offset : offset + max_facts_per_chunk]
            lines = [
                f"{element.location.cell_range}｜{element.display_text}"
                for element in row_batch
            ]
            display_text = f"工作表：{sheet_name}\n" + "\n".join(lines)
            batch_evidence = [
                row_evidence[
                    (
                        element.location.sheet_name or "工作表",
                        element.location.cell_range or "A1:A1",
                    )
                ]
                for element in row_batch
                if (
                    element.location.sheet_name or "工作表",
                    element.location.cell_range or "A1:A1",
                )
                in row_evidence
            ]
            chunks.append(
                ChunkRecord(
                    chunk_id=stable_id(
                        "chunk",
                        document.document_id,
                        sheet_name,
                        "rows",
                        [element.element_id for element in row_batch],
                    ),
                    content_sha256=canonical_sha256(display_text),
                    document_id=document.document_id,
                    source_id=document.source_id,
                    chunk_index=len(chunks),
                    content_type=ChunkContentType.SHEET_TEXT,
                    display_text=display_text,
                    embedding_text=display_text,
                    bm25_text=display_text,
                    heading_path=[sheet_name],
                    token_count=max(1, len(display_text) // 2),
                    source_element_ids=[element.element_id for element in row_batch],
                    evidence_ids=[item.evidence_id for item in batch_evidence],
                    locations=[element.location for element in row_batch],
                    quality=QualityMetadata(),
                    lineage=document.lineage,
                )
            )
    return ChunkingResult(chunks=chunks, evidence=list(evidence))


def _fact_line(fact: TableFact) -> str:
    labels = [label for label in (fact.entity_name, fact.metric_name) if label]
    period = fact.period_end.isoformat() if fact.period_end else "期间未知"
    value = fact.raw_value if fact.raw_value is not None else "缺失"
    cell = fact.location.cell_range or "单元格未知"
    return f"{period}｜{'｜'.join(labels)}｜{value}{fact.unit or ''}｜{cell}"


def _combined_quality(facts: Sequence[TableFact]) -> QualityMetadata:
    reviews = list(
        dict.fromkeys(
            reason
            for fact in facts
            for reason in fact.quality.review_reasons
        )
    )
    flags = list(dict.fromkeys(flag for fact in facts for flag in fact.quality.flags))
    return QualityMetadata(
        status=max((fact.quality.status for fact in facts), key=lambda status: _QUALITY_ORDER[status.value]),
        flags=flags,
        requires_manual_review=bool(reviews),
        review_reasons=reviews,
    )


_QUALITY_ORDER = {
    "passed": 0,
    "warning": 1,
    "requires_review": 2,
    "failed": 3,
}
