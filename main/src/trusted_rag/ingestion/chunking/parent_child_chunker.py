"""按 Docling 元素边界构建可回溯的父子分块。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from trusted_rag.domain.common import LineageMetadata, QualityMetadata, SourceLocation
from trusted_rag.domain.enums import ChunkContentType, ElementType, QualityStatus
from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import (
    ChunkingResult,
    ChunkRecord,
    ChunkRelations,
    DocumentNormalizationResult,
    ElementRecord,
    EvidenceUnit,
    ParentChunkRecord,
)

_TOKEN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?|[^\s]")


@dataclass(frozen=True)
class ParentChildChunkingConfig:
    """确定性父子分块参数。"""

    version: str = "0.01"
    child_target_tokens: int = 550
    child_hard_max_tokens: int = 800
    parent_max_tokens: int = 2500

    def validate(self) -> None:
        """校验分块阈值。

        :return: 无。
        :raises ValueError: 阈值不是正数或顺序错误时抛出。
        """
        if not 0 < self.child_target_tokens <= self.child_hard_max_tokens <= self.parent_max_tokens:
            raise ValueError("分块阈值必须为正数且单调递增。")


def estimate_tokens(text: str) -> int:
    """确定性估算中文、英文和数字混合文本 token 数。

    :param text: 待估算文本。
    :return: 至少为一的近似 token 数。
    """
    return max(1, len(_TOKEN_PATTERN.findall(text)))


def build_parent_child_chunks(
    normalized: DocumentNormalizationResult,
    *,
    config: ParentChildChunkingConfig | None = None,
) -> ChunkingResult:
    """按标题路径和元素边界生成父子分块及相邻关系。

    :param normalized: Docling JSON 标准化结果。
    :param config: 可选版本化分块参数。
    :return: 父分块、检索子分块和原子证据。
    """
    active = config or ParentChildChunkingConfig()
    active.validate()
    evidence_by_element = dict(zip(
        (element.element_id for element in normalized.elements),
        normalized.evidence,
        strict=True,
    ))
    groups: list[list[ElementRecord]] = []
    current: list[ElementRecord] = []
    current_tokens = 0
    current_heading: list[str] | None = None
    for element in normalized.elements:
        tokens = estimate_tokens(element.display_text)
        boundary = current and (
            element.heading_path != current_heading
            or current_tokens + tokens > active.child_target_tokens
            or element.element_type in {ElementType.TABLE, ElementType.PICTURE, ElementType.FORMULA}
        )
        if boundary:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(element)
        current_tokens += tokens
        current_heading = element.heading_path
        if current_tokens >= active.child_hard_max_tokens or element.element_type in {
            ElementType.TABLE,
            ElementType.PICTURE,
            ElementType.FORMULA,
        }:
            groups.append(current)
            current = []
            current_tokens = 0
            current_heading = None
    if current:
        groups.append(current)

    raw_chunks: list[ChunkRecord] = []
    for index, elements in enumerate(groups):
        display = "\n".join(item.display_text for item in elements).strip()
        if not display:
            continue
        evidence = [evidence_by_element[item.element_id] for item in elements]
        heading = elements[0].heading_path
        content_type = _content_type(elements)
        parent_id = stable_id("parent_chunk", normalized.document.document_id, "/".join(heading) or "root")
        quality = _combined_quality(elements)
        embedding_text = "\n".join(
            value for value in [normalized.document.title, " > ".join(heading), display] if value
        )
        chunk_id = stable_id("chunk", normalized.document.document_id, index, canonical_sha256(display))
        raw_chunks.append(
            ChunkRecord(
                chunk_id=chunk_id,
                content_sha256=canonical_sha256(display),
                document_id=normalized.document.document_id,
                source_id=normalized.document.source_id,
                parent_chunk_id=parent_id,
                chunk_index=index,
                content_type=content_type,
                display_text=display,
                embedding_text=embedding_text,
                bm25_text=display,
                heading_path=heading,
                clause=next((item.clause for item in elements if item.clause), None),
                token_count=estimate_tokens(display),
                source_element_ids=[item.element_id for item in elements],
                evidence_ids=[item.evidence_id for item in evidence],
                locations=_unique_locations(item.location for item in elements),
                quality=quality,
                lineage=_chunk_lineage(normalized, active),
            )
        )
    chunks = [
        chunk.model_copy(
            update={
                "relations": ChunkRelations(
                    previous_chunk_id=raw_chunks[index - 1].chunk_id if index else None,
                    next_chunk_id=raw_chunks[index + 1].chunk_id if index + 1 < len(raw_chunks) else None,
                )
            }
        )
        for index, chunk in enumerate(raw_chunks)
    ]
    parents = _build_parents(chunks, normalized.evidence, normalized)
    return ChunkingResult(chunks=chunks, evidence=normalized.evidence, parents=parents)


def _content_type(elements: list[ElementRecord]) -> ChunkContentType:
    if len(elements) == 1:
        mapping = {
            ElementType.TABLE: ChunkContentType.TABLE_ROWS,
            ElementType.PICTURE: ChunkContentType.PICTURE,
            ElementType.FORMULA: ChunkContentType.FORMULA,
        }
        return mapping.get(elements[0].element_type, ChunkContentType.TEXT)
    return ChunkContentType.TEXT


def _combined_quality(elements: list[ElementRecord]) -> QualityMetadata:
    review = any(item.quality.requires_manual_review for item in elements)
    return QualityMetadata(
        status=QualityStatus.REQUIRES_REVIEW if review else QualityStatus.PASSED,
        flags=sorted({flag for item in elements for flag in item.quality.flags}),
        requires_manual_review=review,
        review_reasons=sorted({reason for item in elements for reason in item.quality.review_reasons}),
    )


def _chunk_lineage(
    normalized: DocumentNormalizationResult,
    config: ParentChildChunkingConfig,
) -> LineageMetadata:
    return LineageMetadata(
        run_id=normalized.document.lineage.run_id,
        producer="parent_child_chunker",
        producer_version=config.version,
        input_ids=[normalized.document.document_id],
        created_at=normalized.document.lineage.created_at,
    )


def _build_parents(
    chunks: list[ChunkRecord],
    evidence: list[EvidenceUnit],
    normalized: DocumentNormalizationResult,
) -> list[ParentChunkRecord]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    grouped: dict[str, list[ChunkRecord]] = {}
    for chunk in chunks:
        if chunk.parent_chunk_id is not None:
            grouped.setdefault(chunk.parent_chunk_id, []).append(chunk)
    parents: list[ParentChunkRecord] = []
    for parent_id, children in grouped.items():
        evidence_ids = list(dict.fromkeys(value for child in children for value in child.evidence_ids))
        parents.append(
            ParentChunkRecord(
                parent_chunk_id=parent_id,
                document_id=normalized.document.document_id,
                source_id=normalized.document.source_id,
                heading_path=children[0].heading_path,
                display_text="\n".join(child.display_text for child in children),
                child_chunk_ids=[child.chunk_id for child in children],
                source_element_ids=list(
                    dict.fromkeys(value for child in children for value in child.source_element_ids)
                ),
                evidence_ids=evidence_ids,
                locations=_unique_locations(evidence_by_id[value].location for value in evidence_ids),
                lineage=_chunk_lineage(normalized, ParentChildChunkingConfig()),
            )
        )
    return parents


def _unique_locations(values: Iterable[SourceLocation]) -> list[SourceLocation]:
    """按序列化内容稳定去重来源位置。

    :param values: ``SourceLocation`` 可迭代对象。
    :return: 保持首次出现顺序的位置列表。
    """
    result: list[SourceLocation] = []
    seen: set[str] = set()
    for value in values:
        key = value.model_dump_json()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result
