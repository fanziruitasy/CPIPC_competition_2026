"""定义前端 API 的请求、状态和统一错误契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from trusted_rag.domain.common import ContractModel
from trusted_rag.domain.enums import RetrievalProfile
from trusted_rag.domain.query import AnswerRecord


class ChatRequest(ContractModel):
    """非流式和 SSE Chat 共用的请求。"""

    question: Annotated[str, Field(min_length=1, max_length=5000)]
    knowledge_base_id: str = "nfra-regulations"
    user_id: str | None = None
    session_id: str | None = None
    retrieval_profile: RetrievalProfile | None = None


class ChatResponse(ContractModel):
    """前端可直接展示的 Chat 响应。"""

    trace_id: str
    answer: AnswerRecord


class ApiError(ContractModel):
    """不暴露内部异常和路径的统一错误。"""

    code: str
    message: str
    trace_id: str | None = None


class HealthResponse(ContractModel):
    """存活或就绪检查响应。"""

    status: Literal["ok", "not_ready"]
    checks: dict[str, bool] = Field(default_factory=dict)


class KnowledgeBaseStatus(ContractModel):
    """前端知识库概览和当前索引快照状态。"""

    knowledge_base_id: str
    snapshot_id: str
    collection_alias: str
    source_file_count: int
    unique_document_count: int
    corpus_chunk_count: int
    indexed_chunk_count: int
    fact_count: int
    status: Literal["ready", "not_ready"]
