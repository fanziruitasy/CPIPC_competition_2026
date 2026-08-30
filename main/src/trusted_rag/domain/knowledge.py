"""定义来源文件、文档元素、分块、表格事实和证据契约。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from trusted_rag.domain.common import (
    ArtifactReferences,
    ContractModel,
    LineageMetadata,
    ModelGeneratedContent,
    NonEmptyStr,
    QualityMetadata,
    Sha256,
    SourceLocation,
    StableId,
)
from trusted_rag.domain.enums import (
    ChunkContentType,
    ElementType,
    EvidenceProvenance,
    EvidenceType,
    FormulaCacheStatus,
    ModelContentStatus,
    SourceFormat,
    SourceKind,
    SourceProfile,
    ValueType,
)


class SourceDocument(ContractModel):
    """进入知识构建流程的不可变来源文件登记记录。"""

    schema_version: Literal["source_document.v1"] = "source_document.v1"
    source_id: StableId
    knowledge_base_id: NonEmptyStr
    original_file_name: NonEmptyStr
    normalized_file_name: NonEmptyStr
    source_format: SourceFormat
    source_kind: SourceKind
    source_sha256: Sha256
    file_size_bytes: Annotated[int, Field(ge=0)]
    relative_path: NonEmptyStr
    source_url: str | None = None
    converted_from_source_id: StableId | None = None
    lineage: LineageMetadata

    @field_validator("relative_path")
    @classmethod
    def require_relative_source_path(cls, value: str) -> str:
        """禁止在来源契约中保存宿主机绝对路径。

        :param value: 来源文件相对路径。
        :return: 通过校验的原值。
        :raises ValueError: 路径是 Windows 或 POSIX 绝对路径时抛出。
        """
        if value.startswith(("/", "\\")) or (len(value) >= 3 and value[1] == ":" and value[2] in "\\/"):
            raise ValueError("relative_path 必须是相对路径。")
        return value

    @model_validator(mode="after")
    def validate_conversion_origin(self) -> SourceDocument:
        """校验转换文件必须指向原始来源。

        :return: 通过来源关系校验的当前记录。
        :raises ValueError: 转换文件没有原始来源或普通文件错误提供来源时抛出。
        """
        if self.source_kind is SourceKind.CONVERTED and self.converted_from_source_id is None:
            raise ValueError("转换文件必须提供 converted_from_source_id。")
        if self.source_kind is not SourceKind.CONVERTED and self.converted_from_source_id is not None:
            raise ValueError("只有转换文件可以提供 converted_from_source_id。")
        return self


class RegulatoryMetadata(ContractModel):
    """监管文件身份、时效和业务分类元数据。"""

    issuer: str | None = None
    document_number: str | None = None
    document_type: str | None = None
    publish_date: date | None = None
    effective_date: date | None = None
    expiry_date: date | None = None
    effective_status: Literal["effective", "expired", "repealed", "unknown"] = "unknown"
    regulatory_topics: list[str] = Field(default_factory=list)
    business_domains: list[str] = Field(default_factory=list)
    applicable_entities: list[str] = Field(default_factory=list)


class DocumentRecord(ContractModel):
    """一份来源文件完成专用解析后的文件级记录。"""

    schema_version: Literal["document.v1"] = "document.v1"
    document_id: StableId
    source_id: StableId
    knowledge_base_id: NonEmptyStr
    title: NonEmptyStr
    source_profile: SourceProfile
    parser_name: NonEmptyStr
    parser_version: NonEmptyStr
    parsing_config_version: NonEmptyStr
    artifacts: ArtifactReferences
    regulatory: RegulatoryMetadata = Field(default_factory=RegulatoryMetadata)
    quality: QualityMetadata = Field(default_factory=QualityMetadata)
    lineage: LineageMetadata


class ElementRecord(ContractModel):
    """由专用解析结果标准化得到的最小文档元素。"""

    schema_version: Literal["element.v1"] = "element.v1"
    element_id: StableId
    document_id: StableId
    source_id: StableId
    element_index: Annotated[int, Field(ge=0)]
    element_type: ElementType
    display_text: str = ""
    heading_path: list[str] = Field(default_factory=list)
    clause: str | None = None
    parent_element_id: StableId | None = None
    location: SourceLocation
    quality: QualityMetadata = Field(default_factory=QualityMetadata)
    model_generated_content: ModelGeneratedContent | None = None
    lineage: LineageMetadata


class RetrievalMetadata(ContractModel):
    """查询改写和元数据过滤使用的中文检索信息。"""

    keywords: list[str] = Field(default_factory=list)
    normative_terms: list[str] = Field(default_factory=list)
    indicator_names: list[str] = Field(default_factory=list)
    organization_names: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)


class ChunkRelations(ContractModel):
    """父子分块、相邻分块和表格事实关系。"""

    previous_chunk_id: StableId | None = None
    next_chunk_id: StableId | None = None
    referenced_document_ids: list[StableId] = Field(default_factory=list)
    table_fact_ids: list[StableId] = Field(default_factory=list)


class DenseIndexMetadata(ContractModel):
    """Dense 向量生成配置。"""

    provider: Literal["dashscope"] = "dashscope"
    model: Literal["text-embedding-v3"] = "text-embedding-v3"
    dimensions: Literal[1024] = 1024
    language: Literal["zh-CN"] = "zh-CN"


class Bm25IndexMetadata(ContractModel):
    """本地中文 BM25 表示配置。"""

    tokenizer: Literal["jieba"] = "jieba"
    k1: Annotated[float, Field(gt=0)] = 1.2
    b: Annotated[float, Field(ge=0, le=1)] = 0.75
    lexicon_version: NonEmptyStr = "v0.01"


class IndexingMetadata(ContractModel):
    """最终采用的 Dense 与 BM25 双路索引配置。"""

    dense: DenseIndexMetadata = Field(default_factory=DenseIndexMetadata)
    bm25: Bm25IndexMetadata = Field(default_factory=Bm25IndexMetadata)


class ChunkRecord(ContractModel):
    """进入 Qdrant 并可恢复证据上下文的统一检索分块。"""

    schema_version: Literal["chunk.v1"] = "chunk.v1"
    chunk_id: StableId
    content_sha256: Sha256
    document_id: StableId
    source_id: StableId
    parent_chunk_id: StableId | None = None
    chunk_index: Annotated[int, Field(ge=0)]
    content_type: ChunkContentType
    display_text: NonEmptyStr
    embedding_text: NonEmptyStr
    bm25_text: NonEmptyStr
    heading_path: list[str] = Field(default_factory=list)
    clause: str | None = None
    token_count: Annotated[int, Field(ge=1)]
    source_element_ids: list[StableId] = Field(min_length=1)
    evidence_ids: list[StableId] = Field(min_length=1)
    locations: list[SourceLocation] = Field(min_length=1)
    retrieval: RetrievalMetadata = Field(default_factory=RetrievalMetadata)
    relations: ChunkRelations = Field(default_factory=ChunkRelations)
    indexing: IndexingMetadata = Field(default_factory=IndexingMetadata)
    quality: QualityMetadata = Field(default_factory=QualityMetadata)
    model_generated_content: list[ModelGeneratedContent] = Field(default_factory=list)
    model_generated_content_used: bool = False
    lineage: LineageMetadata

    @model_validator(mode="after")
    def validate_model_generated_content(self) -> ChunkRecord:
        """确保进入检索文本的模型描述均已通过质量校验。

        :return: 通过模型内容约束的当前分块。
        :raises ValueError: 使用了缺失或未通过校验的模型描述时抛出。
        """
        if not self.model_generated_content_used:
            return self
        if not self.model_generated_content:
            raise ValueError("使用模型生成内容时必须保留生成记录。")
        if any(item.validation_status is not ModelContentStatus.PASSED for item in self.model_generated_content):
            raise ValueError("只有通过质量校验的模型生成内容才能进入检索文本。")
        return self


class ParentChunkRecord(ContractModel):
    """用于恢复完整章节上下文且不直接进入向量召回的父分块。"""

    schema_version: Literal["parent_chunk.v1"] = "parent_chunk.v1"
    parent_chunk_id: StableId
    document_id: StableId
    source_id: StableId
    heading_path: list[str] = Field(default_factory=list)
    display_text: NonEmptyStr
    child_chunk_ids: list[StableId] = Field(min_length=1)
    source_element_ids: list[StableId] = Field(min_length=1)
    evidence_ids: list[StableId] = Field(min_length=1)
    locations: list[SourceLocation] = Field(min_length=1)
    lineage: LineageMetadata


class TableFact(ContractModel):
    """可在 DuckDB 中精确取数和确定性计算的表格事实。"""

    schema_version: Literal["table_fact.v1"] = "table_fact.v1"
    fact_id: StableId
    source_id: StableId
    document_id: StableId
    table_id: NonEmptyStr
    metric_code: str | None = None
    metric_name: str | None = None
    entity_code: str | None = None
    entity_name: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_basis: str | None = None
    raw_value: str | None = None
    normalized_value: str | None = None
    value_type: ValueType
    unit: str | None = None
    scale: str | None = None
    statistical_scope: str | None = None
    accounting_basis: str | None = None
    is_formula: bool = False
    formula: str | None = None
    formula_cache_status: FormulaCacheStatus = FormulaCacheStatus.NOT_FORMULA
    evidence_id: StableId
    location: SourceLocation
    quality: QualityMetadata = Field(default_factory=QualityMetadata)
    lineage: LineageMetadata

    @field_validator("normalized_value")
    @classmethod
    def validate_decimal_string(cls, value: str | None) -> str | None:
        """确保规范数值是可精确解析的十进制字符串。

        :param value: 规范化数值字符串或空值。
        :return: 通过校验的原值。
        :raises ValueError: 字符串不能解析为 Decimal 时抛出。
        """
        if value is None:
            return None
        try:
            Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("normalized_value 必须是十进制字符串。") from exc
        return value

    @model_validator(mode="after")
    def validate_formula_fields(self) -> TableFact:
        """校验公式、缓存状态和来源定位的一致性。

        :return: 通过约束的当前表格事实。
        :raises ValueError: 公式状态冲突或数值字段类型不一致时抛出。
        """
        numeric_types = {ValueType.INTEGER, ValueType.DECIMAL, ValueType.PERCENTAGE}
        if self.value_type in numeric_types and self.raw_value is not None and self.normalized_value is None:
            raise ValueError("数值型事实存在原始值时必须提供 normalized_value。")
        if self.is_formula:
            if not self.formula:
                raise ValueError("公式事实必须保留公式文本。")
            if self.formula_cache_status is FormulaCacheStatus.NOT_FORMULA:
                raise ValueError("公式事实不能使用 not_formula 缓存状态。")
        elif self.formula is not None or self.formula_cache_status is not FormulaCacheStatus.NOT_FORMULA:
            raise ValueError("非公式事实不得提供公式或公式缓存状态。")
        return self


class EvidenceUnit(ContractModel):
    """回答可以直接引用并能回到原文件位置的最小证据。"""

    schema_version: Literal["evidence.v1"] = "evidence.v1"
    evidence_id: StableId
    source_id: StableId
    document_id: StableId
    evidence_type: EvidenceType
    excerpt: NonEmptyStr
    source_value: str | None = None
    unit: str | None = None
    provenance: EvidenceProvenance = EvidenceProvenance.ORIGINAL
    location: SourceLocation
    quality: QualityMetadata = Field(default_factory=QualityMetadata)
    model_generated_content: ModelGeneratedContent | None = None
    lineage: LineageMetadata

    @model_validator(mode="after")
    def validate_provenance(self) -> EvidenceUnit:
        """校验证据来源与模型生成记录保持一致。

        :return: 通过来源约束的当前证据。
        :raises ValueError: 模型证据缺少记录或原始证据错误携带记录时抛出。
        """
        if self.provenance is EvidenceProvenance.MODEL_GENERATED:
            if self.model_generated_content is None:
                raise ValueError("模型生成证据必须保留模型生成记录。")
            if self.model_generated_content.validation_status is not ModelContentStatus.PASSED:
                raise ValueError("模型生成证据必须通过质量校验。")
        elif self.model_generated_content is not None:
            raise ValueError("只有模型生成证据可以携带 model_generated_content。")
        return self


class DocumentParseResult(ContractModel):
    """文档解析端口返回的完整标准化结果。"""

    document: DocumentRecord
    elements: list[ElementRecord]


class DocumentNormalizationResult(ContractModel):
    """Docling JSON 标准化产生的文档、元素和原子证据。"""

    document: DocumentRecord
    elements: list[ElementRecord]
    evidence: list[EvidenceUnit]


class SpreadsheetExtractionResult(ContractModel):
    """电子表格抽取端口返回的标准化结果。"""

    document: DocumentRecord
    elements: list[ElementRecord]
    facts: list[TableFact]
    evidence: list[EvidenceUnit]


class ChunkingResult(ContractModel):
    """分块端口返回的分块和对应证据。"""

    chunks: list[ChunkRecord]
    evidence: list[EvidenceUnit]
    parents: list[ParentChunkRecord] = Field(default_factory=list)
