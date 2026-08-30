# 统一数据契约 v1

本文档定义 Word、PDF、Excel 在知识入库、检索、精确取数和可信回答中的最终数据格式。三份来源代码通过适配器转换为本契约，后续模块不直接读取旧字段。

## 1. 契约关系

```text
SourceDocument
  └── DocumentRecord
      ├── ElementRecord
      ├── ChunkRecord ──> EvidenceUnit
      └── TableFact ────> EvidenceUnit

QueryPlan ──> EvidenceUnit[] ──> AnswerRecord
```

统一规则：

- JSON 字段使用 `snake_case`，中文以 UTF-8 原样保存。
- 稳定标识由业务内容生成，不包含 `run_id`；同一输入重建后标识保持一致。
- `run_id` 只记录一次处理运行，放在 `lineage` 中。
- 文件路径只保存相对路径或相对产物 URI，不保存 `E:\...` 等宿主机绝对路径。
- Word/PDF/Excel 使用同一证据结构；不适用的位置字段为 `null`。
- DOCX 没有可靠页码时使用章节、条款和 Docling 元素位置，不伪造页码。
- 银行统计数值的 `raw_value` 和 `normalized_value` 均为字符串，避免浮点精度损失。
- 图片或公式的大模型描述只有通过质量校验后才能进入检索文本，并始终引用原始元素。

## 2. 核心对象字段

### 2.1 SourceDocument

| 字段 | 含义 |
|---|---|
| `source_id` | 由知识库和文件 SHA-256 生成的稳定来源标识 |
| `knowledge_base_id` | 所属知识库 |
| `original_file_name` | 用户可见的原始中文文件名 |
| `normalized_file_name` | 处理使用的规范化文件名，例如 `385.docx` |
| `source_format` | `doc`、`docx`、`pdf`、`xls` 或 `xlsx` |
| `source_kind` | `original`、`uploaded` 或 `converted` |
| `source_sha256` | 原文件完整性摘要 |
| `relative_path` | 数据根目录内的相对路径 |
| `converted_from_source_id` | 转换文件对应的原始 DOC/XLS 来源 |
| `lineage` | 运行、生产模块、版本、输入和时间 |

### 2.2 DocumentRecord 与 ElementRecord

| 对象 | 关键字段 |
|---|---|
| `DocumentRecord` | 文档、来源、解析 Profile、解析器版本、配置版本、JSON/Markdown/HTML 产物、监管元数据和质量状态 |
| `ElementRecord` | 元素顺序、标题/段落/表格/图片/公式类型、原文、章节、条款、Docling 引用、页码/单元格和模型描述 |

### 2.3 ChunkRecord

| 字段 | 含义 |
|---|---|
| `display_text` | 展示给用户和回答模型的正文 |
| `embedding_text` | 标题、章节、条款和正文组成的 Dense 文本 |
| `bm25_text` | 用于 Jieba 和 BM25 的检索文本 |
| `source_element_ids` | 形成该 Chunk 的原始元素 |
| `evidence_ids` | 可直接引用的最小证据 |
| `locations` | 页码、章节、条款、Docling 引用或 Sheet/单元格 |
| `indexing.dense` | `text-embedding-v3`、1024 维、中文 |
| `indexing.bm25` | Jieba、`k1=1.2`、`b=0.75`、词典版本 |
| `model_generated_content_used` | 是否使用了已通过校验的图片/公式描述 |

### 2.4 TableFact

| 字段 | 含义 |
|---|---|
| `metric_code`、`metric_name` | 指标编码和名称 |
| `entity_code`、`entity_name` | 机构、地区或统计对象 |
| `period_start`、`period_end` | 统计期间 |
| `period_basis` | 时点、累计、季度等期间口径 |
| `raw_value` | 单元格原始显示值 |
| `normalized_value` | 可由 Decimal 精确解析的字符串 |
| `unit`、`scale` | 单位和倍率 |
| `formula` | 原始公式 |
| `formula_cache_status` | `not_formula`、`cached`、`missing` 或 `recalculated` |
| `location` | Sheet/单元格，或文档表格的页码/表格位置 |

### 2.5 EvidenceUnit、QueryPlan 与 AnswerRecord

| 对象 | 关键字段 |
|---|---|
| `EvidenceUnit` | 证据类型、摘录、原值、单位、来源属性、可回指位置和质量 |
| `QueryPlan` | 原问题、规范问题、查询路由、意图、检索开关、过滤条件和白名单结构化操作 |
| `AnswerRecord` | 回答状态、答案、公开引用、拒答原因、检索摘要、模型用量和追踪标识 |

## 3. 完整 Chunk 示例

```json
{
  "schema_version": "chunk.v1",
  "chunk_id": "chunk_3315dc62d2c94abf8a5a4c06",
  "content_sha256": "8cfd18b9ce24e3200b0e5225909b3f6f654db3b37957cd92637378555c61519c",
  "document_id": "document_5f944195f1e044aa47cd1c4f",
  "source_id": "source_c5b115546c7d60cfe33d49d5",
  "parent_chunk_id": null,
  "chunk_index": 0,
  "content_type": "text",
  "display_text": "商业银行应当建立风险管理制度。",
  "embedding_text": "商业银行风险管理办法\n第二章 > 风险管理\n第十二条\n商业银行应当建立风险管理制度。",
  "bm25_text": "商业银行风险管理办法\n第二章 > 风险管理\n第十二条\n商业银行应当建立风险管理制度。",
  "heading_path": ["第二章", "风险管理"],
  "clause": "第十二条",
  "token_count": 18,
  "source_element_ids": ["element_320422e44bf255c5aa429fe0"],
  "evidence_ids": ["evidence_43e5ed13b582fb6b265bb673"],
  "locations": [
    {
      "page_number": null,
      "bounding_box": null,
      "section_path": ["第二章", "风险管理"],
      "clause": "第十二条",
      "docling_ref": "#/texts/12",
      "element_ref": "element_320422e44bf255c5aa429fe0",
      "sheet_name": null,
      "cell_range": null,
      "table_id": null,
      "artifact_uri": null
    }
  ],
  "retrieval": {
    "keywords": [],
    "normative_terms": [],
    "indicator_names": [],
    "organization_names": [],
    "periods": [],
    "units": []
  },
  "relations": {
    "previous_chunk_id": null,
    "next_chunk_id": null,
    "referenced_document_ids": [],
    "table_fact_ids": []
  },
  "indexing": {
    "dense": {
      "provider": "dashscope",
      "model": "text-embedding-v3",
      "dimensions": 1024,
      "language": "zh-CN"
    },
    "bm25": {
      "tokenizer": "jieba",
      "k1": 1.2,
      "b": 0.75,
      "lexicon_version": "v0.01"
    }
  },
  "quality": {
    "status": "passed",
    "flags": [],
    "requires_manual_review": false,
    "review_reasons": []
  },
  "model_generated_content": [],
  "model_generated_content_used": false,
  "lineage": {
    "run_id": "document-ingestion-v0.01-20260830T120000Z-a1b2c3d4",
    "producer": "document-chunker",
    "producer_version": "0.1.0",
    "input_ids": ["element_320422e44bf255c5aa429fe0"],
    "created_at": "2026-08-30T12:00:00Z"
  }
}
```

## 4. 完整表格事实与证据示例

```json
{
  "table_fact": {
    "schema_version": "table_fact.v1",
    "fact_id": "fact_111111111111111111111111",
    "source_id": "source_222222222222222222222222",
    "document_id": "document_333333333333333333333333",
    "table_id": "assets_table",
    "metric_code": "total_assets",
    "metric_name": "资产总额",
    "entity_code": "CN_BANKING_TOTAL",
    "entity_name": "银行业金融机构",
    "period_start": "2026-06-30",
    "period_end": "2026-06-30",
    "period_basis": "point_in_time",
    "raw_value": "12345.678900",
    "normalized_value": "12345.678900",
    "value_type": "decimal",
    "unit": "亿元",
    "scale": "1",
    "statistical_scope": "银行业金融机构合计",
    "accounting_basis": null,
    "is_formula": false,
    "formula": null,
    "formula_cache_status": "not_formula",
    "evidence_id": "evidence_444444444444444444444444",
    "location": {
      "page_number": null,
      "bounding_box": null,
      "section_path": [],
      "clause": null,
      "docling_ref": null,
      "element_ref": null,
      "sheet_name": "资产负债表",
      "cell_range": "B3",
      "table_id": "assets_table",
      "artifact_uri": null
    },
    "quality": {
      "status": "passed",
      "flags": [],
      "requires_manual_review": false,
      "review_reasons": []
    },
    "lineage": {
      "run_id": "spreadsheet-ingestion-v0.01-20260830T120000Z-a1b2c3d4",
      "producer": "spreadsheet-extractor",
      "producer_version": "0.1.0",
      "input_ids": ["source_222222222222222222222222"],
      "created_at": "2026-08-30T12:00:00Z"
    }
  },
  "evidence": {
    "schema_version": "evidence.v1",
    "evidence_id": "evidence_444444444444444444444444",
    "source_id": "source_222222222222222222222222",
    "document_id": "document_333333333333333333333333",
    "evidence_type": "cell",
    "excerpt": "资产总额：12345.678900亿元",
    "source_value": "12345.678900",
    "unit": "亿元",
    "provenance": "original",
    "location": {
      "page_number": null,
      "bounding_box": null,
      "section_path": [],
      "clause": null,
      "docling_ref": null,
      "element_ref": null,
      "sheet_name": "资产负债表",
      "cell_range": "B3",
      "table_id": "assets_table",
      "artifact_uri": null
    },
    "quality": {
      "status": "passed",
      "flags": [],
      "requires_manual_review": false,
      "review_reasons": []
    },
    "model_generated_content": null,
    "lineage": {
      "run_id": "spreadsheet-ingestion-v0.01-20260830T120000Z-a1b2c3d4",
      "producer": "spreadsheet-extractor",
      "producer_version": "0.1.0",
      "input_ids": ["fact_111111111111111111111111"],
      "created_at": "2026-08-30T12:00:00Z"
    }
  }
}
```

## 5. 完整前端引用和回答示例

```json
{
  "schema_version": "answer.v1",
  "answer_id": "answer_920835a48f272fc5499312bd",
  "trace_id": "trace_test",
  "query_plan_id": "query_plan_db0fc046772b38f179ea469a",
  "knowledge_base_id": "banking-regulations",
  "user_id": null,
  "session_id": null,
  "question": "资本管理要求是什么？",
  "status": "answered",
  "answer_text": "商业银行应当建立风险管理制度。",
  "citations": [
    {
      "evidence_id": "evidence_43e5ed13b582fb6b265bb673",
      "source_id": "source_c5b115546c7d60cfe33d49d5",
      "original_file_name": "商业银行风险管理办法.docx",
      "source_format": "docx",
      "excerpt": "商业银行应当建立风险管理制度。",
      "location": {
        "page_number": null,
        "section_path": ["第二章", "风险管理"],
        "clause": "第十二条",
        "element_ref": "element_320422e44bf255c5aa429fe0",
        "sheet_name": null,
        "cell_range": null,
        "table_id": null
      }
    }
  ],
  "refusal_reason": null,
  "retrieval": {
    "profile": "dense_bm25",
    "dense_candidate_count": 40,
    "bm25_candidate_count": 40,
    "fused_candidate_count": 32,
    "reranked_candidate_count": 10,
    "structured_fact_count": 0
  },
  "usage": {
    "model_name": "qwen3.7-plus",
    "input_tokens": 1200,
    "output_tokens": 120,
    "estimated_cost_cny": "0.0123",
    "latency_ms": 1850
  },
  "created_at": "2026-08-30T12:00:00Z"
}
```

前端引用模型中没有 `relative_path`、`artifact_uri` 或任何宿主机绝对路径。

## 6. 稳定端口

`domain/ports.py` 定义以下端口，领域层不导入 Docling、Qdrant、DuckDB、DashScope 或 OpenAI SDK：

| 端口 | 职责 |
|---|---|
| `DocumentParser` | Word/PDF → 文档和元素 |
| `SpreadsheetExtractor` | Excel → 文档、元素、事实和证据 |
| `Chunker` | 文档结构 → 父子 Chunk 和证据 |
| `DenseEmbedder` | 文本 → 1024 维 Dense 向量 |
| `LexicalIndexer` | BM25 文本 → 稀疏词项表示 |
| `VectorStore` | 快照建库、写入、检索和 Alias 切换 |
| `FactStore` | 写入事实快照并执行受控 QueryPlan |
| `ArtifactStore` | 读写版本化 JSON 与人工检查产物 |
| `QueryPlanner` | 问题 → QueryPlan |
| `Reranker` | 融合候选 → 精排候选 |
| `AnswerGenerator` | 编号证据 → 待校验回答草稿 |
| `IngestionJobRepository` | 保存异步入库任务状态 |
| `AuditRepository` | 追加问答、模型和索引审计事件 |

