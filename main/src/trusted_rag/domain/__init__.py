"""统一领域契约、校验规则与端口协议。"""

from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import (
    ChunkRecord,
    DocumentRecord,
    ElementRecord,
    EvidenceUnit,
    SourceDocument,
    TableFact,
)
from trusted_rag.domain.query import AnswerRecord, QueryPlan

__all__ = [
    "AnswerRecord",
    "ChunkRecord",
    "DocumentRecord",
    "ElementRecord",
    "EvidenceUnit",
    "QueryPlan",
    "SourceDocument",
    "TableFact",
    "canonical_sha256",
    "stable_id",
]
