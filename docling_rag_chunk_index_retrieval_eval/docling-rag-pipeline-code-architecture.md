# Docling0816 RAG Pipeline 代码架构与运行说明

本文档说明当前最新一版 `docling0816` RAG 系统的代码结构、Pipeline 顺序、关键输入输出、运行命令、评测方式和注意事项。

更新时间：2026-08-19  
当前推荐链路：v7 target strict  
当前构建根目录：`D:\金融科技大赛\Code\data\processed\docling0816\p0_full`

## 1. 总体 Pipeline

```text
Docling0816 JSON
  -> P0 扫描 / Normalize / Chunk
  -> P2 DuckDB + Qdrant 入库
  -> P3 Hybrid Retrieval
  -> QA Eval / 错题重跑 / 延迟抽样 / Report
```

设计目标：

1. 支持 `converted-docx`、`native-docx`、`pdf` 三个 profile 全量读取。
2. 保留 Docling provenance，能回到原文件、页码、表格、图片、公式和 parent chunk。
3. 将正文、表格、图片、公式分别生成可检索 chunk。
4. Qdrant 负责 dense + sparse/BM25 检索。
5. DuckDB 负责 documents/elements/chunks/tables/table_cells/table_facts/numeric_values 结构化查询。
6. QA 评测通过 retrieval 获取证据，再由 LLM 基于证据选择 A/B/C/D。
7. 支持缓存、错题重跑、错误归因和现场演示延迟抽样。

## 2. 代码目录结构

```text
D:\金融科技大赛\Code\rag_doc_ingestion
├── scripts
│   ├── run_docling_p0.py
│   ├── run_docling_p1_eval.py
│   ├── run_docling_p2_index.py
│   ├── run_docling_p3_eval_qa.py
│   ├── run_docling_p3_latency_sample.py
│   ├── restore_formula_evidence.py
│   └── run_ingestion.py
└── src
    └── rag_doc_ingestion
        └── docling_p0
            ├── __init__.py
            ├── pipeline.py
            ├── normalize.py
            ├── chunking.py
            ├── index_build.py
            ├── retrieval.py
            ├── eval_qa.py
            ├── latency_sample.py
            ├── eval.py
            ├── indexes.py
            └── utils.py
```

## 3. 阶段 P0：扫描、Normalize、Chunk

入口脚本：

`D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p0.py`

调用模块：

`rag_doc_ingestion.docling_p0.pipeline.main`

推荐命令：

```powershell
& 'C:\Users\86198\anaconda3\envs\pytorch39\python.exe' `
  'D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p0.py' `
  --input-root 'D:\金融科技大赛\Code\data\docling0816' `
  --output-root 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full'
```

### 3.1 `pipeline.py`

职责：

1. 扫描 `docling0816` 下三个 profile。
2. 建立 `raw_manifest.jsonl`。
3. 调用 `normalize.py` 将 Docling JSON 转成统一结构。
4. 调用 `chunking.py` 生成 text/table/figure/formula chunks。
5. 写出 P0 全部 JSONL 产物和 `build_report.json`。

注意：

- 当前默认使用 `docling0816`，这是已完成图片和公式处理后的版本。
- 全量构建会处理 111 个文档，不建议调试时频繁重跑。
- 调试可先改用 sample 或直接复用已有 `p0_full`。

### 3.2 `normalize.py`

职责：

1. 将三种 profile 的 Docling JSON 统一为文档、元素、表格、图片、公式结构。
2. 支持标题、正文、列表、页眉页脚、caption、table、figure、formula 等元素类型。
3. 支持图片字段：`pictures`、`meta.description`、`annotations`、caption。
4. 支持公式字段：formula label、公式文本、上下文、provenance。
5. 标记元素质量：`ready`、`quarantine`、`exclude`。

注意：

- 不把页面截图 `page_*.png` 当作独立 figure。
- `quality_status=exclude` 的元素不进入主检索。
- `pdf 370` 有已知版面风险，涉及数值 QA 时谨慎使用。

### 3.3 `chunking.py`

职责：

1. 正文按章节路径、页码、token 长度切块。
2. 表格生成 `table_summary` 和 `table_rows`。
3. 图片生成 `figure` chunk。
4. 公式生成 `formula` chunk。
5. 生成 `parent_chunks.jsonl`，用于上下文扩展和引用。

当前 chunk 类型：

| 类型 | 数量 |
| --- | ---: |
| text | 7858 |
| table_rows | 540 |
| table_summary | 414 |
| figure | 509 |
| formula | 256 |

注意：

- 表格既以 chunk 进入 Qdrant，也以 Cell/Fact 进入 DuckDB。
- figure/formula chunk 用于召回，完整对象仍保存在 `figures.jsonl`、`formulas.jsonl`。
- parent chunk 不直接替代主 chunk，它主要用于扩大上下文和解释来源。

### 3.4 P0 产物与统计

输出根目录：

`D:\金融科技大赛\Code\data\processed\docling0816\p0_full`

| 产物 | 内容 |
| --- | --- |
| `raw_manifest.jsonl` | 原始 Docling JSON 清单 |
| `normalized_documents.jsonl` | 文档元数据 |
| `normalized_elements.jsonl` | 元素级数据 |
| `normalized_tables.jsonl` | 表格结构 |
| `table_cells.jsonl` | 表格单元格 |
| `figures.jsonl` | 图片对象 |
| `formulas.jsonl` | 公式对象 |
| `chunks.jsonl` | 检索主 chunk |
| `parent_chunks.jsonl` | parent chunk |
| `exclusions.jsonl` | 排除和隔离记录 |
| `build_report.json` | 构建统计 |

最新统计：111 个文档、21082 个元素、9577 个 chunks、414 个表格、632 张图片、256 个公式、20535 个表格单元格。

## 4. 阶段 P2：DuckDB 与 Qdrant 入库

入口脚本：

`D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p2_index.py`

调用模块：

`rag_doc_ingestion.docling_p0.index_build.main`

推荐命令：

```powershell
& 'C:\Users\86198\anaconda3\envs\pytorch39\python.exe' `
  'D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p2_index.py' `
  --build-root 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full' `
  --embedding-backend api `
  --collection rag_chunks_docling0816
```

### 4.1 `index_build.py`

职责分两块：

1. 构建 DuckDB。
   - `documents`
   - `elements`
   - `chunks`
   - `tables`
   - `table_cells`
   - `table_facts`
   - `numeric_values`

2. 构建 Qdrant。
   - dense vector
   - sparse/BM25 vector
   - metadata payload

最新入库统计：

| 模块 | 指标 | 数量/配置 |
| --- | --- | ---: |
| DuckDB | documents | 111 |
| DuckDB | elements | 21082 |
| DuckDB | chunks | 9577 |
| DuckDB | tables | 414 |
| DuckDB | table_cells | 20535 |
| DuckDB | table_facts | 13376 |
| DuckDB | numeric_values | 7825 |
| Qdrant | collection | `rag_chunks_docling0816` |
| Qdrant | points | 9577 |
| Dense | model | `text-embedding-v3` |
| Dense | dim | 1024 |
| Sparse | vocab size | 80000 |
| Sparse | tokenizer | jieba + regulatory_terms + fallback.v1 |

注意：

- Dense embedding 优先调用 `.env` 中配置的真实 embedding API。
- embedding cache 位于 `indexes\embedding_cache\api_embeddings.jsonl`。
- Sparse/BM25 依赖 jieba 和监管词典，词表位于 `indexes\bm25s\jieba_sparse_vocab.json`。
- 如果同伴使用 Qdrant server，而不是本地 path，需要把 `QdrantClient(path=...)` 改为 `QdrantClient(url=...)`。

## 5. 阶段 P3：Hybrid Retrieval

核心模块：

`D:\金融科技大赛\Code\rag_doc_ingestion\src\rag_doc_ingestion\docling_p0\retrieval.py`

### 5.1 `retrieval.py` 职责

1. 解析 query，识别目标资料 `source_hint`。
2. 生成 query embedding，并使用 query cache。
3. 执行 Qdrant dense 检索。
4. 执行 Qdrant sparse/BM25 检索。
5. 按需执行 DuckDB 表格事实检索。
6. 执行 source_hint 目标资料专路检索。
7. 使用 RRF 融合多路候选。
8. 按目标资料、file_label、source_type/profile 做软 boost。
9. 调用 rerank API 精排。
10. 输出 top evidence 给 QA 或问答模块。

### 5.2 当前 v7 检索策略

```text
全局检索 query = 问题 + 目标资料名
目标文档内 query = 问题 + A/B/C/D
```

原因：选择题干扰项可能来自其他文档，如果把选项拼进全局 query，会把非目标资料中的相似定义召回并污染排序。

召回路径：

| 路径 | 是否开启 | 用途 |
| --- | --- | --- |
| Dense Qdrant | 开启 | 语义召回 |
| Sparse/BM25 Qdrant | 开启 | 标题、文号、机构、监管术语召回 |
| DuckDB TableFact | 条件开启 | 表格事实、数值、行列定位 |
| source_hint 专路 | 开启 | 目标资料内精准召回 |
| RRF | 开启 | 多路去重融合 |
| Rerank | 开启 | 最终证据排序 |

注意：

- source_hint 是软约束，不是硬过滤，避免误删同源辅助证据。
- v7 已增加 source_type/profile boost，PDF 题优先 PDF profile，Word 题优先 doc/docx profile。
- Rerank API 失败时不会中断检索，会使用融合排序结果继续。

## 6. 阶段 P3：QA Eval

核心模块：

`D:\金融科技大赛\Code\rag_doc_ingestion\src\rag_doc_ingestion\docling_p0\eval_qa.py`

入口脚本：

`D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p3_eval_qa.py`

### 6.1 `eval_qa.py` 职责

1. 读取 `QA数据.xlsx`。
2. 过滤 source types，例如只跑 `word pdf`。
3. 调用 `HybridRetriever.retrieve()` 获取 evidence。
4. 构造两阶段 LLM 判题 prompt。
5. 要求 LLM 对每个选项给出 `support/refute/unknown` 和直接证据 chunk_id。
6. 输出最终 A/B/C/D。
7. 记录每题预测、标准答案、是否正确、证据命中、召回来源、失败类型。
8. 生成 `qa_eval_details.jsonl` 和 `qa_eval_report.json`。
9. 支持 `--only-wrong-from` 只重跑错题。
10. 支持 `--no-lexical-final` 关闭词面兜底。

### 6.2 正式评测推荐命令

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:EMBEDDING_TIMEOUT_SECONDS='30'
$env:EMBEDDING_MAX_RETRIES='3'
$env:RERANK_TIMEOUT_SECONDS='30'
$env:RERANK_MAX_RETRIES='2'
$env:LLM_TIMEOUT_SECONDS='90'
$env:LLM_MAX_RETRIES='3'
$env:LLM_RETRY_BACKOFF_SECONDS='5'

& 'C:\Users\86198\anaconda3\envs\pytorch39\python.exe' `
  'D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p3_eval_qa.py' `
  --qa-path 'D:\金融科技大赛\Code\data\QA数据.xlsx' `
  --build-root 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full' `
  --env-path 'D:\金融科技大赛\Code\.env' `
  --collection rag_chunks_docling0816 `
  --source-types word pdf `
  --limit 200 `
  --out-dir 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full\eval\qa_hybrid_word_pdf_200_v7_target_strict' `
  --no-lexical-final
```

### 6.3 错题重跑命令

```powershell
& 'C:\Users\86198\anaconda3\envs\pytorch39\python.exe' `
  'D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p3_eval_qa.py' `
  --qa-path 'D:\金融科技大赛\Code\data\QA数据.xlsx' `
  --build-root 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full' `
  --env-path 'D:\金融科技大赛\Code\.env' `
  --collection rag_chunks_docling0816 `
  --source-types word pdf `
  --only-wrong-from 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full\eval\qa_hybrid_word_pdf_200_v7_target_strict\qa_eval_details.jsonl' `
  --out-dir 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full\eval\qa_hybrid_word_pdf_200_v7_retry_wrong' `
  --no-lexical-final
```

### 6.4 失败类型

| failure_type | 含义 |
| --- | --- |
| `correct` | 预测选项等于标准答案 |
| `api_failed` | LLM/API 多次重试失败，未给出正式选项 |
| `llm_choice_error_after_recall` | 证据已召回，但 LLM 选错 |
| `evidence_not_recalled` | 标准答案相关证据未被召回 |
| `no_retrieval_hits` | 没有检索结果 |
| `llm_over_refusal_or_low_confidence` | 证据存在但模型过度拒答或低置信 |

`lexical_fallback` 说明：当 LLM API 失败或输出不可解析时，用题干、选项、证据的词面重合度临时猜 A/B/C/D。它只适合流程不中断，不适合正式准确率统计。当前 v7 正式评测已用 `--no-lexical-final` 关闭。

## 7. 阶段 P3：延迟抽样

核心模块：

`D:\金融科技大赛\Code\rag_doc_ingestion\src\rag_doc_ingestion\docling_p0\latency_sample.py`

入口脚本：

`D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p3_latency_sample.py`

职责：

1. 从 word/pdf QA 池中随机抽取指定数量题目。
2. 对每题记录 retrieval、第一阶段 LLM、第二阶段 LLM、端到端耗时。
3. 输出每题明细和汇总报告。
4. 支持 `--fast-demo-mode`，用于现场演示延迟优化。

推荐命令：

```powershell
& 'C:\Users\86198\anaconda3\envs\pytorch39\python.exe' `
  'D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p3_latency_sample.py' `
  --qa-path 'D:\金融科技大赛\Code\data\QA数据.xlsx' `
  --build-root 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full' `
  --env-path 'D:\金融科技大赛\Code\.env' `
  --collection rag_chunks_docling0816 `
  --source-types word pdf `
  --sample-size 10 `
  --seed 20260818 `
  --out-dir 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full\eval\latency_sample10'
```

最新 10 题抽样结果：

| 指标 | 数值 |
| --- | ---: |
| 样本数 | 10 |
| 正确数 | 10 |
| API 失败 | 0 |
| 二阶段触发 | 2 |
| 端到端平均耗时 | 50.16 秒 |
| 端到端 P50 | 43.91 秒 |
| 端到端 P90 | 78.42 秒 |
| 检索平均耗时 | 6.01 秒 |
| 第一阶段 LLM 平均耗时 | 36.24 秒 |
| 第二阶段 LLM 平均耗时 | 39.55 秒 |

现场演示建议：

1. 提前预热 query embedding cache 和 LLM cache。
2. 演示时优先使用 `--fast-demo-mode` 或同等配置。
3. 降低 top evidence 数量，例如 top5 改 top3。
4. 第二阶段只在低置信或 INSUFFICIENT 时触发。
5. 网络不稳定时优先展示已缓存问题。

## 8. 最新运行结果摘要

当前最新正式评测目录：

`D:\金融科技大赛\Code\data\processed\docling0816\p0_full\eval\qa_hybrid_word_pdf_200_v7_target_strict`

| 指标 | 数值 |
| --- | ---: |
| 总题数 | 200 |
| 正确数 | 191 |
| 准确率 | 95.5% |
| 证据命中率 | 100.0% |
| Word 准确率 | 93.0% |
| PDF 准确率 | 98.0% |
| API 失败 | 6 |
| 证据召回后 LLM 选错 | 3 |
| 召回失败 | 0 |

非正确题：Q112、Q114、Q128、Q162、Q168、Q176、Q192、Q282、Q284。

其中：

- Q112、Q114、Q128、Q162、Q282、Q284 是 API/网络失败。
- Q168、Q176、Q192 是证据已召回但 LLM 选错。

## 9. 各代码文件作用速查

| 文件 | 作用 | 注意点 |
| --- | --- | --- |
| `pipeline.py` | P0 总入口，扫描、normalize、chunk、写报告 | 全量运行处理 111 文档，调试时避免频繁重跑 |
| `normalize.py` | 将 Docling JSON 统一成 documents/elements/tables/figures/formulas | 重点维护三 profile 字段兼容、图片和公式字段兼容 |
| `chunking.py` | 生成 text/table/figure/formula chunk 和 parent chunk | 表格 chunk 与 DuckDB 表结构要保持 ID 对齐 |
| `index_build.py` | 构建 DuckDB 和 Qdrant dense/sparse 索引 | 入库前确认 `.env`、Qdrant 本地路径、embedding cache |
| `retrieval.py` | query 路由、多路召回、RRF、boost、rerank | v7 中全局检索不拼选项，选项只进目标资料内检索 |
| `eval_qa.py` | 读取 QA、调用检索、LLM 判题、输出评测报告 | 正式评测建议 `--no-lexical-final` |
| `latency_sample.py` | 随机抽样记录每题回答耗时 | 用于现场演示前压测，不替代准确率评测 |
| `utils.py` | JSONL、文本清洗、token 估算、路径和 hash 工具 | P0/P2/P3 共用，改动会影响全链路 |
| `indexes.py` | embedding、hash、索引辅助能力 | 保持与 `index_build.py`、`retrieval.py` 的向量维度一致 |
| `eval.py` | 早期 P1 评估辅助 | 当前 QA 正式评测以 `eval_qa.py` 为准 |

## 10. 运行注意事项

1. `.env` 必须包含 embedding、rerank、LLM 所需 API 配置。
2. 评测依赖网络 API，网络波动会造成 `api_failed`，这类错误应与真实召回/判题错误分开看。
3. 正式准确率评测建议关闭 lexical final，避免 API 失败被词面猜测掩盖。
4. 资料库内选择题可以 force-choice，资料库外拒答题必须关闭 force-choice。
5. 表格取数题需要单独检查 DuckDB `table_facts`、`numeric_values` 和原始表格 chunk。
6. 若迁移到别的机器，注意 Python 环境、依赖包、Qdrant 存储路径、`.env` 和缓存路径。
7. 若只改 prompt 或 eval 逻辑，不需要重建 P0/P2；若改 chunk schema 或 embedding 文本，需要重跑 P0/P2。
8. 若只修复 API 失败题，使用 `--only-wrong-from`，不要一上来重跑全量。

## 11. 后续迭代优先级

1. 对 Q168、Q176、Q192 做选项级 evidence pack。
2. 对 API 失败题单独重跑并合并报告。
3. 增加资料库外拒答评测集。
4. 增强 DuckDB 表格取数 evaluator。
5. 做面向现场演示的轻量 query 接口：`query -> retrieve -> rerank -> answer -> citations`。
