"""校验 Docling 结构化产物并生成可追溯的解析质量报告。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from trusted_rag.domain.common import ContractModel, NonEmptyStr, Sha256
from trusted_rag.domain.enums import QualityStatus, SourceProfile
from trusted_rag.infrastructure.artifacts import sha256_file


class ParsedArtifactFile(ContractModel):
    """单个解析产物的相对地址和完整性摘要。"""

    relative_uri: NonEmptyStr
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: Sha256


class DoclingQualityReport(ContractModel):
    """一份 DoclingDocument 的结构完整性与人工复核提示。"""

    schema_version: Literal["docling_quality.v1"] = "docling_quality.v1"
    document_name: NonEmptyStr
    source_profile: SourceProfile
    docling_schema_version: NonEmptyStr
    text_count: Annotated[int, Field(ge=0)]
    table_count: Annotated[int, Field(ge=0)]
    picture_count: Annotated[int, Field(ge=0)]
    formula_count: Annotated[int, Field(ge=0)]
    page_count: Annotated[int, Field(ge=0)]
    described_picture_count: Annotated[int, Field(ge=0)]
    empty_text_count: Annotated[int, Field(ge=0)]
    unresolved_body_references: list[str] = Field(default_factory=list)
    quality_status: QualityStatus
    quality_flags: list[str] = Field(default_factory=list)
    requires_manual_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class DoclingParseArtifacts(ContractModel):
    """Docling 解析阶段发布的全部产物及质量报告。"""

    schema_version: Literal["docling_parse_artifacts.v1"] = "docling_parse_artifacts.v1"
    source_id: NonEmptyStr
    document_name: NonEmptyStr
    source_profile: SourceProfile
    parser_name: Literal["docling"] = "docling"
    parser_version: NonEmptyStr
    files: list[ParsedArtifactFile] = Field(min_length=3)
    quality: DoclingQualityReport


def inspect_docling_json(path: Path, *, source_profile: SourceProfile) -> DoclingQualityReport:
    """检查 Docling JSON 是否可恢复、引用是否有效并统计关键元素。

    :param path: Docling 导出的 UTF-8 JSON 文件。
    :param source_profile: 原生 DOCX 或转换型 DOCX 来源类型。
    :return: 结构化质量报告。
    :raises FileNotFoundError: JSON 文件不存在时抛出。
    :raises ValueError: 文件不是有效 DoclingDocument 或正文为空时抛出。
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_name") != "DoclingDocument":
        raise ValueError("JSON 不是 DoclingDocument。")
    name = str(payload.get("name", "")).strip()
    version = str(payload.get("version", "")).strip()
    if not name or not version:
        raise ValueError("DoclingDocument 缺少 name 或 version。")

    # 重新构造模型可发现 JSON 中仅靠字典检查无法识别的模式错误。
    from docling_core.types.doc.document import DoclingDocument

    DoclingDocument.model_validate(payload)
    texts = _object_list(payload, "texts")
    tables = _object_list(payload, "tables")
    pictures = _object_list(payload, "pictures")
    pages = payload.get("pages", {})
    page_count = len(pages) if isinstance(pages, (dict, list)) else 0
    formula_count = sum(item.get("label") == "formula" for item in texts)
    empty_text_count = sum(not str(item.get("text", "")).strip() for item in texts)
    described_picture_count = sum(_has_model_description(item) for item in pictures)
    body = payload.get("body")
    body_refs = _collect_references(body)
    unresolved = sorted(reference for reference in body_refs if not _reference_exists(payload, reference))

    flags: list[str] = []
    review_reasons: list[str] = []
    if empty_text_count:
        flags.append("empty_text_elements")
    if unresolved:
        flags.append("unresolved_body_references")
        review_reasons.append("正文树包含无法解析的 Docling 引用。")
    if pictures and described_picture_count < len(pictures):
        flags.append("pictures_without_model_description")
        review_reasons.append("存在尚未获得可靠模型描述的图片，分块前需执行描述校验或回溯。")
    if not texts and not tables and not pictures:
        raise ValueError("DoclingDocument 不包含可用正文元素。")
    status = QualityStatus.REQUIRES_REVIEW if review_reasons else QualityStatus.PASSED
    return DoclingQualityReport(
        document_name=name,
        source_profile=source_profile,
        docling_schema_version=version,
        text_count=len(texts),
        table_count=len(tables),
        picture_count=len(pictures),
        formula_count=formula_count,
        page_count=page_count,
        described_picture_count=described_picture_count,
        empty_text_count=empty_text_count,
        unresolved_body_references=unresolved,
        quality_status=status,
        quality_flags=flags,
        requires_manual_review=bool(review_reasons),
        review_reasons=review_reasons,
    )


def inventory_artifacts(root: Path, *, relative_to: Path) -> list[ParsedArtifactFile]:
    """为解析目录中的全部文件生成稳定排序的哈希清单。

    :param root: 单文档解析产物目录。
    :param relative_to: 所有 URI 的相对基准目录。
    :return: 按相对 URI 排序的文件记录。
    :raises ValueError: 产物目录逃逸相对基准时抛出。
    """
    resolved_base = relative_to.resolve()
    records: list[ParsedArtifactFile] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file():
            continue
        uri = path.resolve().relative_to(resolved_base).as_posix()
        records.append(
            ParsedArtifactFile(relative_uri=uri, size_bytes=path.stat().st_size, sha256=sha256_file(path))
        )
    return records


def _object_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"DoclingDocument 字段 {key} 必须是数组。")
    return [item for item in value if isinstance(item, dict)]


def _has_model_description(picture: dict[str, Any]) -> bool:
    annotations = picture.get("annotations", [])
    if not isinstance(annotations, list):
        return False
    return any(
        isinstance(annotation, dict)
        and annotation.get("kind") == "description"
        and bool(str(annotation.get("text", "")).strip())
        for annotation in annotations
    )


def _collect_references(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            references.add(reference)
        for child in value.values():
            references.update(_collect_references(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_collect_references(child))
    return references


def _reference_exists(payload: dict[str, Any], reference: str) -> bool:
    if not reference.startswith("#/"):
        return False
    current: Any = payload
    for part in reference[2:].split("/"):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False
    return True
