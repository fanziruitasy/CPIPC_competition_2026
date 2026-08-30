"""把 Docling JSON 标准化为统一文档元素和可回指证据。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trusted_rag.domain.common import (
    ArtifactReferences,
    BoundingBox,
    LineageMetadata,
    ModelGeneratedContent,
    QualityMetadata,
    SourceLocation,
)
from trusted_rag.domain.enums import (
    ElementType,
    EvidenceProvenance,
    EvidenceType,
    ModelContentStatus,
    QualityStatus,
    SourceProfile,
)
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import (
    DocumentNormalizationResult,
    DocumentRecord,
    ElementRecord,
    EvidenceUnit,
    SourceDocument,
)

_CLAUSE_PATTERN = re.compile(r"^(第[一二三四五六七八九十百千万零〇两\d]+条|[一二三四五六七八九十]+、)")
_UNUSABLE_MODEL_TEXT = ("无法可靠识别", "无法识别", "无有效内容", "没有可确认的信息")


def standardize_docling_json(
    path: Path,
    *,
    source: SourceDocument,
    source_profile: SourceProfile,
    parsing_run_id: str,
    parser_version: str,
    artifact_base_uri: str,
    created_at: datetime | None = None,
) -> DocumentNormalizationResult:
    """从 Docling JSON 构造统一文档、元素与原子证据。

    :param path: 已通过质量检查的 Docling JSON。
    :param source: 对应不可变来源登记。
    :param source_profile: 原生 DOCX、转换型 DOCX 或 PDF。
    :param parsing_run_id: 本次解析运行标识。
    :param parser_version: Docling 版本。
    :param artifact_base_uri: 相对运行根的单文档产物目录。
    :param created_at: 可注入血缘时间。
    :return: 统一文档、顺序元素和证据。
    :raises ValueError: JSON 不是有效 DoclingDocument 或没有可用元素时抛出。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    from docling_core.types.doc.document import DoclingDocument

    docling_document = DoclingDocument.model_validate(payload)
    timestamp = created_at or datetime.now(UTC)
    document_id = stable_id("document", source.source_id, parser_version)
    lineage = LineageMetadata(
        run_id=parsing_run_id,
        producer="docling_standardizer",
        producer_version="0.01",
        input_ids=[source.source_id],
        created_at=timestamp,
    )
    document = DocumentRecord(
        document_id=document_id,
        source_id=source.source_id,
        knowledge_base_id=source.knowledge_base_id,
        title=Path(source.original_file_name).stem,
        source_profile=source_profile,
        parser_name="docling",
        parser_version=parser_version,
        parsing_config_version="0.01",
        artifacts=ArtifactReferences(
            structured_json_uri=f"{artifact_base_uri}/{source.source_id}.json",
            markdown_uri=f"{artifact_base_uri}/{source.source_id}.md",
            html_uri=f"{artifact_base_uri}/{source.source_id}.html",
            asset_directory_uri=f"{artifact_base_uri}/{source.source_id}_artifacts",
        ),
        lineage=lineage,
    )
    elements: list[ElementRecord] = []
    evidence: list[EvidenceUnit] = []
    heading_path: list[str] = []
    ordered_items = [
        item.model_dump(mode="json", serialize_as_any=True)
        for item, _level in docling_document.iterate_items()
    ]
    for item in ordered_items:
        reference = str(item.get("self_ref", ""))
        element_type, display_text = _element_content(item, reference)
        if element_type is None or (not display_text and element_type is not ElementType.PICTURE):
            continue
        level = int(item.get("level", 1) or 1)
        if element_type is ElementType.HEADING:
            heading_path = heading_path[: max(0, level - 1)]
            heading_path.append(display_text)
        location = _location(item)
        element_id = stable_id("element", document_id, reference)
        quality, model_content = _model_quality(item, element_type, reference, timestamp)
        element = ElementRecord(
            element_id=element_id,
            document_id=document_id,
            source_id=source.source_id,
            element_index=len(elements),
            element_type=element_type,
            display_text=display_text,
            heading_path=list(heading_path),
            clause=_clause(display_text),
            location=location,
            quality=quality,
            model_generated_content=model_content,
            lineage=lineage,
        )
        elements.append(element)
        evidence.append(_evidence_for_element(element, timestamp))
    if not elements:
        raise ValueError("Docling JSON 没有可标准化元素。")
    return DocumentNormalizationResult(document=document, elements=elements, evidence=evidence)


def _body_items(document: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    visited: set[str] = set()

    def resolve(reference: str) -> dict[str, Any]:
        current: Any = document
        for token in reference[2:].split("/"):
            current = current[int(token)] if isinstance(current, list) else current[token]
        if not isinstance(current, dict):
            raise ValueError(f"Docling 引用不是对象：{reference}")
        return current

    def walk(node: Any) -> None:
        if isinstance(node, dict) and ("$ref" in node or "cref" in node):
            reference = str(node.get("$ref") or node.get("cref"))
            if reference in visited:
                return
            visited.add(reference)
            item = resolve(reference)
            if reference.startswith("#/groups/"):
                for child in item.get("children", []):
                    walk(child)
            else:
                items.append(item)
            return
        if isinstance(node, dict):
            for child in node.get("children", []):
                walk(child)

    walk(document.get("body", {}))
    return items


def _element_content(item: dict[str, Any], reference: str) -> tuple[ElementType | None, str]:
    label = str(item.get("label", ""))
    if reference.startswith("#/texts/"):
        mapping = {
            "section_header": ElementType.HEADING,
            "title": ElementType.HEADING,
            "list_item": ElementType.LIST_ITEM,
            "formula": ElementType.FORMULA,
            "caption": ElementType.CAPTION,
            "footnote": ElementType.FOOTNOTE,
        }
        return mapping.get(label, ElementType.PARAGRAPH), str(item.get("text", "")).strip()
    if reference.startswith("#/tables/"):
        rows = _table_rows(item)
        return ElementType.TABLE, "\n".join(rows)
    if reference.startswith("#/pictures/"):
        description = _picture_description(item)
        return ElementType.PICTURE, description or "[图片]"
    return None, ""


def _table_rows(item: dict[str, Any]) -> list[str]:
    data = item.get("data", {})
    rows: list[str] = []
    for row_index in range(int(data.get("num_rows", 0) or 0)):
        cells = sorted(
            (
                cell
                for cell in data.get("table_cells", [])
                if int(cell.get("start_row_offset_idx", -1)) <= row_index
                < int(cell.get("end_row_offset_idx", -1))
            ),
            key=lambda cell: int(cell.get("start_col_offset_idx", 0)),
        )
        text = " | ".join(str(cell.get("text", "")).strip() for cell in cells if str(cell.get("text", "")).strip())
        if text:
            rows.append(text)
    return rows


def _picture_description(item: dict[str, Any]) -> str | None:
    for annotation in item.get("annotations", []) or []:
        if annotation.get("kind") == "description":
            return str(annotation.get("text", "")).strip() or None
    return None


def _location(item: dict[str, Any]) -> SourceLocation:
    reference = str(item.get("self_ref", "")) or None
    image = item.get("image")
    artifact_uri = image.get("uri") if isinstance(image, dict) else None
    if isinstance(artifact_uri, str) and artifact_uri.startswith("data:"):
        artifact_uri = None
    provenance = item.get("prov", []) or []
    if provenance:
        first = provenance[0]
        bbox_raw = first.get("bbox")
        bbox = None
        if isinstance(bbox_raw, dict):
            bbox = BoundingBox(
                left=float(bbox_raw["l"]),
                top=min(float(bbox_raw["t"]), float(bbox_raw["b"])),
                right=float(bbox_raw["r"]),
                bottom=max(float(bbox_raw["t"]), float(bbox_raw["b"])),
            )
        return SourceLocation(
            page_number=int(first["page_no"]),
            bounding_box=bbox,
            docling_ref=reference,
            element_ref=reference,
            artifact_uri=artifact_uri,
        )
    return SourceLocation(docling_ref=reference, element_ref=reference, artifact_uri=artifact_uri)


def _model_quality(
    item: dict[str, Any],
    element_type: ElementType,
    reference: str,
    timestamp: datetime,
) -> tuple[QualityMetadata, ModelGeneratedContent | None]:
    if element_type is not ElementType.PICTURE:
        review = element_type is ElementType.FORMULA
        return (
            QualityMetadata(
                status=QualityStatus.REQUIRES_REVIEW if review else QualityStatus.PASSED,
                flags=["formula_requires_review"] if review else [],
                requires_manual_review=review,
                review_reasons=["公式转写需与原文核对。"] if review else [],
            ),
            None,
        )
    description = _picture_description(item)
    if not description:
        return QualityMetadata(
            status=QualityStatus.REQUIRES_REVIEW,
            flags=["picture_without_description"],
            requires_manual_review=True,
            review_reasons=["图片没有可用语义描述。"],
        ), None
    unusable = any(pattern in description for pattern in _UNUSABLE_MODEL_TEXT)
    status = ModelContentStatus.REJECTED if unusable else ModelContentStatus.PENDING
    model_content = ModelGeneratedContent(
        text=description,
        model_name="configured-vlm",
        validation_status=status,
        original_element_ref=reference,
        generated_at=timestamp,
    )
    return QualityMetadata(
        status=QualityStatus.REQUIRES_REVIEW,
        flags=["model_description_unusable" if unusable else "picture_description_pending_validation"],
        requires_manual_review=True,
        review_reasons=["模型图片描述尚未通过质量校验。"],
    ), model_content


def _clause(text: str) -> str | None:
    match = _CLAUSE_PATTERN.match(text)
    return match.group(1) if match else None


def _evidence_for_element(element: ElementRecord, timestamp: datetime) -> EvidenceUnit:
    type_map = {
        ElementType.TABLE: EvidenceType.TABLE,
        ElementType.PICTURE: EvidenceType.PICTURE,
        ElementType.FORMULA: EvidenceType.FORMULA,
    }
    return EvidenceUnit(
        evidence_id=stable_id("evidence", element.element_id, "original"),
        source_id=element.source_id,
        document_id=element.document_id,
        evidence_type=type_map.get(element.element_type, EvidenceType.TEXT),
        excerpt=element.display_text,
        provenance=EvidenceProvenance.ORIGINAL,
        location=element.location,
        quality=element.quality,
        lineage=element.lineage.model_copy(update={"created_at": timestamp}),
    )
