"""定义统一数据契约使用的稳定枚举。"""

from enum import StrEnum


class SourceFormat(StrEnum):
    """知识源文件格式。"""

    DOC = "doc"
    DOCX = "docx"
    PDF = "pdf"
    XLS = "xls"
    XLSX = "xlsx"


class SourceKind(StrEnum):
    """文件进入系统时的来源类型。"""

    ORIGINAL = "original"
    UPLOADED = "uploaded"
    CONVERTED = "converted"


class SourceProfile(StrEnum):
    """专用预处理链路。"""

    NATIVE_DOCX = "native_docx"
    CONVERTED_DOCX = "converted_docx"
    PDF = "pdf"
    EXCEL = "excel"


class ElementType(StrEnum):
    """标准化文档元素类型。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_CELL = "table_cell"
    PICTURE = "picture"
    FORMULA = "formula"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    KEY_VALUE = "key_value"
    SHEET_TEXT = "sheet_text"


class ChunkContentType(StrEnum):
    """检索分块内容类型。"""

    TEXT = "text"
    TABLE_SUMMARY = "table_summary"
    TABLE_ROWS = "table_rows"
    PICTURE = "picture"
    FORMULA = "formula"
    SHEET_TEXT = "sheet_text"


class EvidenceType(StrEnum):
    """最小可引用证据类型。"""

    TEXT = "text"
    TABLE = "table"
    PICTURE = "picture"
    FORMULA = "formula"
    CELL = "cell"
    CALCULATION = "calculation"


class EvidenceProvenance(StrEnum):
    """证据内容的生成来源。"""

    ORIGINAL = "original"
    MODEL_GENERATED = "model_generated"
    DETERMINISTIC_CALCULATION = "deterministic_calculation"


class QualityStatus(StrEnum):
    """记录质量门禁状态。"""

    PASSED = "passed"
    WARNING = "warning"
    REQUIRES_REVIEW = "requires_review"
    FAILED = "failed"


class ModelContentStatus(StrEnum):
    """模型生成内容的质量校验状态。"""

    PENDING = "pending"
    PASSED = "passed"
    REJECTED = "rejected"
    FAILED = "failed"


class ValueType(StrEnum):
    """表格事实值类型。"""

    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    PERCENTAGE = "percentage"
    DATE = "date"
    BOOLEAN = "boolean"
    BLANK = "blank"


class FormulaCacheStatus(StrEnum):
    """公式缓存值状态。"""

    NOT_FORMULA = "not_formula"
    CACHED = "cached"
    MISSING = "missing"
    RECALCULATED = "recalculated"


class QueryRoute(StrEnum):
    """查询执行路径。"""

    DOCUMENT = "document"
    STRUCTURED = "structured"
    MIXED = "mixed"
    REJECT = "reject"


class QueryIntent(StrEnum):
    """统一查询意图。"""

    GENERAL_LOOKUP = "general_lookup"
    REGULATION_LOOKUP = "regulation_lookup"
    REGULATION_COMPARISON = "regulation_comparison"
    CLAUSE_LOOKUP = "clause_lookup"
    STATISTIC_LOOKUP = "statistic_lookup"
    STATISTIC_COMPARISON = "statistic_comparison"
    STATISTIC_CALCULATION = "statistic_calculation"


class StructuredOperationType(StrEnum):
    """允许 DuckDB 执行的结构化操作。"""

    LOOKUP = "lookup"
    SUM = "sum"
    AVERAGE = "average"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    COUNT = "count"
    DIFFERENCE = "difference"
    RATIO = "ratio"
    TREND = "trend"


class RetrievalProfile(StrEnum):
    """可评测的检索召回组合。"""

    DENSE_ONLY = "dense_only"
    BM25_ONLY = "bm25_only"
    DENSE_BM25 = "dense_bm25"


class AnswerStatus(StrEnum):
    """可信回答终态。"""

    ANSWERED = "answered"
    REFUSED = "refused"
    CLARIFICATION_REQUIRED = "clarification_required"
    FAILED = "failed"


class JobStatus(StrEnum):
    """异步入库任务状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileProcessingStatus(StrEnum):
    """入库任务内单个文件的处理状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IngestionStage(StrEnum):
    """文件入库链路的可观测阶段。"""

    QUEUED = "queued"
    VALIDATING = "validating"
    CONVERTING = "converting"
    PARSING = "parsing"
    NORMALIZING = "normalizing"
    QUALITY_CHECKING = "quality_checking"
    INDEXING = "indexing"
    COMPLETED = "completed"
