"""定义解析、检索、存储和回答模块之间的稳定端口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, model_validator

from trusted_rag.domain.common import ContractModel, NonEmptyStr, StableId
from trusted_rag.domain.enums import (
    FileProcessingStatus,
    IngestionStage,
    JobStatus,
    RetrievalProfile,
)
from trusted_rag.domain.knowledge import (
    ChunkingResult,
    ChunkRecord,
    DocumentParseResult,
    DocumentRecord,
    ElementRecord,
    EvidenceUnit,
    SourceDocument,
    SpreadsheetExtractionResult,
    TableFact,
)
from trusted_rag.domain.query import QueryFilters, QueryPlan


class DenseVector(ContractModel):
    """一条 1024 维 Dense 向量及模型请求信息。"""

    values: list[float] = Field(min_length=1024, max_length=1024)
    model_name: NonEmptyStr = "text-embedding-v3"
    request_id: str | None = None


class SparseVector(ContractModel):
    """一条用于 BM25 检索的稀疏词项表示。"""

    indices: list[Annotated[int, Field(ge=0)]]
    values: list[float]

    @model_validator(mode="after")
    def validate_sparse_shape(self) -> SparseVector:
        """校验稀疏向量索引和值一一对应且索引唯一。

        :return: 通过结构约束的当前稀疏向量。
        :raises ValueError: 长度不同或索引重复时抛出。
        """
        if len(self.indices) != len(self.values):
            raise ValueError("稀疏向量 indices 和 values 长度必须一致。")
        if len(set(self.indices)) != len(self.indices):
            raise ValueError("稀疏向量 indices 不得重复。")
        return self


class IndexRecord(ContractModel):
    """向量存储端口接收的完整索引记录。"""

    chunk: ChunkRecord
    dense_vector: DenseVector
    bm25_vector: SparseVector
    payload: dict[str, Any] = Field(default_factory=dict)


class SnapshotReference(ContractModel):
    """Qdrant 和 DuckDB 共同使用的不可变知识库快照标识。"""

    knowledge_base_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    qdrant_collection: NonEmptyStr
    duckdb_uri: NonEmptyStr


class SearchRequest(ContractModel):
    """向量存储端口执行一次可审计检索所需的信息。"""

    query: NonEmptyStr
    profile: RetrievalProfile
    dense_vector: DenseVector | None = None
    bm25_vector: SparseVector | None = None
    filters: QueryFilters = Field(default_factory=QueryFilters)
    top_k: Annotated[int, Field(ge=1, le=200)] = 40

    @model_validator(mode="after")
    def validate_profile_vectors(self) -> SearchRequest:
        """校验检索剖面所需向量均已提供。

        :return: 通过剖面约束的当前检索请求。
        :raises ValueError: 剖面缺少对应 Dense 或 BM25 向量时抛出。
        """
        if self.profile in {RetrievalProfile.DENSE_ONLY, RetrievalProfile.DENSE_BM25} and self.dense_vector is None:
            raise ValueError("当前检索剖面必须提供 dense_vector。")
        if self.profile in {RetrievalProfile.BM25_ONLY, RetrievalProfile.DENSE_BM25} and self.bm25_vector is None:
            raise ValueError("当前检索剖面必须提供 bm25_vector。")
        return self


class SearchHit(ContractModel):
    """存储检索、融合或精排阶段返回的统一候选。"""

    chunk_id: StableId
    score: float
    rank: Annotated[int, Field(ge=1)]
    channel: RetrievalProfile | str
    chunk: ChunkRecord


class AnswerDraft(ContractModel):
    """回答模型生成但尚未通过引用校验的草稿。"""

    answer_text: str
    cited_evidence_ids: list[StableId] = Field(default_factory=list)
    refusal_reason: str | None = None
    model_name: NonEmptyStr
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    latency_ms: Annotated[int, Field(ge=0)] = 0
    request_id: str | None = None


class IngestionFileRecord(ContractModel):
    """异步入库任务中单个文件的可公开处理状态。"""

    source_id: StableId
    original_file_name: NonEmptyStr
    status: FileProcessingStatus = FileProcessingStatus.QUEUED
    stage: IngestionStage = IngestionStage.QUEUED
    progress_percent: Annotated[int, Field(ge=0, le=100)] = 0
    public_error_code: str | None = None
    public_error: str | None = None
    updated_at: AwareDatetime


class IngestionJobRecord(ContractModel):
    """异步入库任务及其逐文件状态的持久化记录。"""

    job_id: NonEmptyStr
    knowledge_base_id: NonEmptyStr
    idempotency_key: NonEmptyStr
    status: JobStatus
    source_ids: list[StableId] = Field(default_factory=list)
    files: list[IngestionFileRecord] = Field(default_factory=list)
    progress_percent: Annotated[int, Field(ge=0, le=100)] = 0
    public_error: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AuditRecord(ContractModel):
    """查询、模型调用和索引操作使用的结构化审计记录。"""

    audit_id: NonEmptyStr
    trace_id: NonEmptyStr
    event_type: NonEmptyStr
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: AwareDatetime


@runtime_checkable
class DocumentParser(Protocol):
    """将 Word 或 PDF 来源转换为统一文档和元素。"""

    def parse(self, source: SourceDocument, *, run_id: str) -> DocumentParseResult:
        """解析一份 Word 或 PDF 来源。

        :param source: 已登记的来源文件。
        :param run_id: 当前解析运行标识。
        :return: 标准文档和元素。
        """
        ...


@runtime_checkable
class SpreadsheetExtractor(Protocol):
    """将 Excel 来源转换为标准元素、事实和证据。"""

    def extract(self, source: SourceDocument, *, run_id: str) -> SpreadsheetExtractionResult:
        """抽取一份 Excel 来源。

        :param source: 已登记的电子表格来源。
        :param run_id: 当前抽取运行标识。
        :return: 标准文档、元素、表格事实和证据。
        """
        ...


@runtime_checkable
class Chunker(Protocol):
    """把标准文档结构转换为父子检索分块。"""

    def chunk(
        self,
        document: DocumentRecord,
        elements: Sequence[ElementRecord],
        facts: Sequence[TableFact] = (),
    ) -> ChunkingResult:
        """生成分块及其最小引用证据。

        :param document: 文件级标准记录。
        :param elements: 已按文档顺序排列的标准元素。
        :param facts: 可选表格事实。
        :return: 检索分块和证据。
        """
        ...


@runtime_checkable
class DenseEmbedder(Protocol):
    """隐藏 DashScope Dense Embedding 调用细节。"""

    def embed(self, texts: Sequence[str]) -> list[DenseVector]:
        """按输入顺序生成 Dense 向量。

        :param texts: 待向量化文本。
        :return: 与输入顺序和数量一致的 Dense 向量。
        """
        ...


@runtime_checkable
class LexicalIndexer(Protocol):
    """隐藏 Jieba、术语词典和 BM25 编码细节。"""

    def encode(self, texts: Sequence[str]) -> list[SparseVector]:
        """按输入顺序生成 BM25 稀疏表示。

        :param texts: 待分词和编码的 BM25 文本。
        :return: 与输入顺序和数量一致的稀疏表示。
        """
        ...


@runtime_checkable
class VectorStore(Protocol):
    """在不可变快照中保存和检索文档索引。"""

    def create_snapshot(self, snapshot: SnapshotReference) -> None:
        """创建尚未对外服务的空索引快照。

        :param snapshot: 新知识库快照信息。
        :return: 无。
        """
        ...

    def upsert(self, snapshot: SnapshotReference, records: Sequence[IndexRecord]) -> None:
        """向指定快照写入索引记录。

        :param snapshot: 目标知识库快照。
        :param records: 已生成 Dense 和 BM25 表示的分块。
        :return: 无。
        """
        ...

    def search(self, snapshot: SnapshotReference, request: SearchRequest) -> list[SearchHit]:
        """在指定快照执行配置要求的检索。

        :param snapshot: 只读查询快照。
        :param request: 查询向量、过滤条件和 Top-K。
        :return: 保留通道、分数和排名的候选。
        """
        ...

    def activate(self, snapshot: SnapshotReference, *, alias: str) -> None:
        """把稳定别名原子切换到已验收快照。

        :param snapshot: 已通过质量和冒烟检查的快照。
        :param alias: 对外访问的稳定别名。
        :return: 无。
        """
        ...


@runtime_checkable
class FactStore(Protocol):
    """保存表格事实并执行受控 QueryPlan。"""

    def replace_snapshot(self, snapshot: SnapshotReference, facts: Sequence[TableFact]) -> None:
        """为不可变快照写入完整事实集。

        :param snapshot: 目标知识库快照。
        :param facts: 已通过质量门禁的表格事实。
        :return: 无。
        """
        ...

    def query(self, snapshot: SnapshotReference, plan: QueryPlan) -> list[EvidenceUnit]:
        """执行白名单约束的结构化查询。

        :param snapshot: 只读查询快照。
        :param plan: 已通过模式校验的查询计划。
        :return: 携带单元格或表格定位的证据。
        """
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    """保存和读取版本化 JSON、Markdown 与其他文件产物。"""

    def write_json(self, relative_uri: str, payload: Any) -> str:
        """以 UTF-8 中文原样写入 JSON 产物。

        :param relative_uri: 运行目录内的相对 URI。
        :param payload: 可序列化的模型或普通数据。
        :return: 最终相对 URI。
        """
        ...

    def read_json(self, relative_uri: str) -> Any:
        """读取一份结构化 JSON 产物。

        :param relative_uri: 运行目录内的相对 URI。
        :return: 反序列化后的普通数据。
        """
        ...


@runtime_checkable
class QueryPlanner(Protocol):
    """把自然语言问题转换为受控查询计划。"""

    def plan(self, question: str, *, knowledge_base_id: str, trace_id: str) -> QueryPlan:
        """生成并校验查询计划。

        :param question: 用户原始问题。
        :param knowledge_base_id: 查询目标知识库。
        :param trace_id: 当前请求追踪标识。
        :return: 规则优先且可审计的查询计划。
        """
        ...


@runtime_checkable
class Reranker(Protocol):
    """根据问题对融合候选进行相关性精排。"""

    def rerank(self, question: str, hits: Sequence[SearchHit], *, top_k: int) -> list[SearchHit]:
        """返回限定数量的精排候选。

        :param question: 用户问题。
        :param hits: RRF 融合后的候选。
        :param top_k: 需要返回的候选数量。
        :return: 按相关性重新排序的候选。
        """
        ...


@runtime_checkable
class AnswerGenerator(Protocol):
    """只根据编号证据生成待校验回答草稿。"""

    def generate(self, question: str, evidence: Sequence[EvidenceUnit]) -> AnswerDraft:
        """生成回答文本及其引用标识。

        :param question: 用户问题。
        :param evidence: 已通过证据门禁的上下文。
        :return: 尚待引用与数字校验的回答草稿。
        """
        ...


@runtime_checkable
class IngestionJobRepository(Protocol):
    """持久化异步入库任务的当前状态。"""

    def save(self, job: IngestionJobRecord) -> None:
        """新增或更新一条任务状态。

        :param job: 最新任务记录。
        :return: 无。
        """
        ...

    def get(self, job_id: str) -> IngestionJobRecord | None:
        """按任务标识读取当前状态。

        :param job_id: 入库任务标识。
        :return: 任务记录；不存在时返回空值。
        """
        ...


@runtime_checkable
class AuditRepository(Protocol):
    """追加保存问答、模型和索引审计事件。"""

    def append(self, record: AuditRecord) -> None:
        """追加一条不可变审计记录。

        :param record: 结构化审计事件。
        :return: 无。
        """
        ...


ArtifactPayload = ContractModel | Mapping[str, Any] | Sequence[Any]
