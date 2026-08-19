# Docling0816 RAG 构建、索引、检索与评测报告

本文档记录当前基于docling0816的 RAG 全流程结果，包括 Docling JSON 全量读取、normalize、chunk、DuckDB/Qdrant 入库、混合检索、QA 选择题评测与阶段性优化结论。

更新时间：2026-08-19  
当前推荐版本：v7 target strict  
当前推荐评测目录：`D:\金融科技大赛\Code\data\processed\docling0816\p0_full\eval\qa_hybrid_word_pdf_200_v7_target_strict`

## 1. 数据输入与处理范围

### 1.1 输入数据

| 类型 | 路径/说明 |
| --- | --- |
| Docling 解析结果 | `D:\金融科技大赛\Code\data\docling0816` |
| converted-docx profile | `converted-docx\v0.03\001` |
| native-docx profile | `native-docx\v0.04\001` |
| pdf profile | `pdf\v0.05\001` |
| QA 数据 | `D:\金融科技大赛\Code\data\QA数据.xlsx` |
| API 配置 | `D:\金融科技大赛\Code\.env` |

当前处理对象是 Docling 已完成解析的 Word/PDF 资料。Excel 不在本轮 chunk 和 QA 评测范围内。

### 1.2 P0 输出根目录

`D:\金融科技大赛\Code\data\processed\docling0816\p0_full`

主要产物：

| 文件 | 内容 |
| --- | --- |
| `raw_manifest.jsonl` | 三个 profile 扫描出的 Docling JSON 清单 |
| `normalized_documents.jsonl` | 文档级元数据，含 `doc_id`、`source_profile`、`source_path` 等 |
| `normalized_elements.jsonl` | 规范化后的标题、正文、列表、表格、图片、公式等元素 |
| `normalized_tables.jsonl` | 表格结构记录 |
| `table_cells.jsonl` | 表格单元格记录 |
| `figures.jsonl` | 图片元素、caption、description、annotations 等 |
| `formulas.jsonl` | 公式元素、formula label、公式文本、上下文等 |
| `chunks.jsonl` | 主检索 chunk，含 text/table/figure/formula |
| `parent_chunks.jsonl` | parent chunk，用于上下文扩展和引用溯源 |
| `exclusions.jsonl` | 被排除或隔离的元素记录 |
| `build_report.json` | P0 构建统计报告 |

## 2. P0 Normalize + Chunk 结果

入口脚本：

```powershell
& 'C:\Users\86198\anaconda3\envs\pytorch39\python.exe' `
  'D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p0.py' `
  --input-root 'D:\金融科技大赛\Code\data\docling0816' `
  --output-root 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full'
```

当前状态：已全量完成。

### 2.1 处理逻辑

1. 扫描 `converted-docx`、`native-docx`、`pdf` 三个 profile。
2. 将不同 profile 的 Docling JSON 统一为文档、元素、表格、图片、公式结构。
3. 对正文按章节路径、页码、token 长度进行切块。
4. 对表格生成 `table_summary` 和 `table_rows` chunk。
5. 对图片生成 `figure` chunk，利用 `pictures[].meta.description.text`、`annotations`、caption 等信息。
6. 对公式生成 `formula` chunk，保留 formula label、公式文本和上下文。
7. 保留 provenance、page、section、parent 信息，便于引用和评测。

### 2.2 P0 统计

| 指标 | 数值 |
| --- | ---: |
| 文档数 | 111 |
| converted-docx | 32 |
| native-docx | 34 |
| pdf | 45 |
| 元素数 | 21082 |
| ready 元素 | 16287 |
| quarantine 元素 | 123 |
| exclude 元素 | 4672 |
| chunk 数 | 9577 |
| parent chunk 数 | 6962 |
| 表格数 | 414 |
| 表格单元格 | 20535 |
| 图片数 | 632 |
| 公式数 | 256 |
| exclusion 数 | 4795 |

chunk 类型分布：

| chunk 类型 | 数量 |
| --- | ---: |
| text | 7858 |
| table_rows | 540 |
| table_summary | 414 |
| figure | 509 |
| formula | 256 |

chunk token 分布：

| 指标 | 数值 |
| --- | ---: |
| P50 | 134 |
| P90 | 411 |
| P99 | 580 |

注意点：

- `pdf 370` 存在已知版面问题，标记为 `known_pdf370_layout_issue`、`disable_numeric_qa_until_reparsed`。涉及精确数值问答时应谨慎。
- figure/formula chunk 用于可召回和可引用，完整对象仍保存在 `figures.jsonl`、`formulas.jsonl`。
- 超长 chunk 主要集中在少数复杂正文和复杂表格行，当前仍可进入检索，但后续可针对超长表格行继续细分。

## 3. DuckDB 与 Qdrant 入库结果

入口脚本：

```powershell
& 'C:\Users\86198\anaconda3\envs\pytorch39\python.exe' `
  'D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p2_index.py' `
  --build-root 'D:\金融科技大赛\Code\data\processed\docling0816\p0_full' `
  --embedding-backend api `
  --collection rag_chunks_docling0816
```

当前状态：已全量完成。Dense vector 使用 `.env` 中配置的真实 embedding API，Sparse vector 使用 jieba + 监管词典 + BM25 权重。

### 3.1 DuckDB

路径：

`D:\金融科技大赛\Code\data\processed\docling0816\p0_full\indexes\duckdb\rag_tables.duckdb`

入库表：

| 表 | 内容 | 数量 |
| --- | --- | ---: |
| `documents` | 文档级元数据 | 111 |
| `elements` | 规范化元素 | 21082 |
| `chunks` | chunk 元数据和文本 | 9577 |
| `tables` | 表格结构 | 414 |
| `table_cells` | 单元格级内容 | 20535 |
| `table_facts` | 行头、列头、值抽取后的弱结构化事实 | 13376 |
| `numeric_values` | 可解析数值字段 | 7825 |

DuckDB 主要用于表格事实、单元格定位、数字/日期/比例/金额等结构化检索和后续计算。制度事实类问答可以不强依赖 DuckDB，表格取数题应开启 DuckDB evidence。

### 3.2 Qdrant

本地 Qdrant path：

`D:\金融科技大赛\Code\data\processed\docling0816\p0_full\indexes\qdrant_local`

collection：`rag_chunks_docling0816`

| 指标 | 数值 |
| --- | ---: |
| Qdrant points | 9577 |
| dense backend | api |
| dense model | text-embedding-v3 |
| dense dim | 1024 |
| dense cached | 7538 |
| dense created | 2039 |
| dense batch size | 8 |
| dense concurrency | 4 |
| sparse vocab size | 80000 |
| avg sparse terms | 95.18 |
| max sparse terms | 501 |
| sparse tokenizer | jieba + regulatory_terms + fallback.v1 |

关键路径：

| 文件/目录 | 说明 |
| --- | --- |
| `indexes\embedding_cache\api_embeddings.jsonl` | chunk embedding cache |
| `indexes\embedding_cache\query_api_embeddings.jsonl` | query embedding cache |
| `indexes\bm25s\jieba_sparse_vocab.json` | sparse/BM25 词表 |
| `indexes\index_build_report.json` | 入库统计报告 |

## 4. 当前检索链路

核心代码：

`D:\金融科技大赛\Code\rag_doc_ingestion\src\rag_doc_ingestion\docling_p0\retrieval.py`

当前 v7 推荐链路：

```text
User Query
  -> query 路由与目标资料识别
  -> 全局 dense 检索：Qdrant dense topK，query = 问题 + 目标资料名
  -> 全局 sparse/BM25 检索：Qdrant sparse topK，query = 问题 + 目标资料名
  -> 条件 DuckDB 检索：TableFact/Cell/Numeric evidence
  -> source_hint 专路：目标资料内检索，query = 问题 + A/B/C/D
  -> RRF 去重融合
  -> source_title/file_label/source_type/profile boost
  -> rerank 精排
  -> evidence topK 输出给 LLM
```

### 4.1 最终运行开关

| 模块 | 最终运行是否开启 | 说明 |
| --- | --- | --- |
| Dense 检索 | 开启 | 语义召回主路 |
| Sparse/BM25 检索 | 开启 | 文号、机构、标题、监管术语效果好 |
| DuckDB | 条件开启 | 查询涉及表格、行列、金额、比例、日期、数量时启用 |
| source_hint 专路 | 开启 | QA 题干常明确指定资料名，应强利用 |
| RRF | 开启 | 多路召回去重融合 |
| Reranker | 开启 | 使用 `.env` 中 rerank API，失败则保留融合排序 |
| query embedding cache | 开启 | 降低重复评测成本和网络抖动 |
| LLM cache | 开启 | 降低重复判题成本 |

### 4.2 v7 检索修正

1. 全局 query 不再默认拼接 A/B/C/D 选项。
   - 错误做法：`全局 query = 问题 + A/B/C/D`。
   - 当前做法：`全局 query = 问题 + 目标资料名`。
   - 原因：干扰选项可能来自其他文档，会污染全局 dense/sparse 召回。

2. 选项只进入目标资料内检索。
   - 当前做法：`目标文档内 query = 问题 + A/B/C/D`。
   - 目的：在目标文档范围内提高选项相关证据召回，不扩大到全库污染。

3. evidence 排序增加软约束。
   - 目标资料标题命中 boost。
   - file_label/doc_title 命中 boost。
   - source_type/profile 命中 boost。
   - 非目标资料不硬过滤，但降权或排后，避免错杀同源补充证据。

## 5. QA 选择题评测链路

核心代码：

`D:\金融科技大赛\Code\rag_doc_ingestion\src\rag_doc_ingestion\docling_p0\eval_qa.py`

入口脚本：

`D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p3_eval_qa.py`

### 5.1 当前评测逻辑

```text
QA row
  -> 解析题号、source_type、题型、题干、A/B/C/D、标准答案
  -> 调用 HybridRetriever 获取 top evidence
  -> 构造两阶段 LLM prompt
  -> 第一阶段：逐选项判断 support/refute/unknown，并要求 direct chunk_id
  -> 第二阶段：输出最终 A/B/C/D 或 INSUFFICIENT
  -> 若资料库内题 evidence 命中但 INSUFFICIENT，可触发 force-choice 二次判题
  -> 输出每题 JSONL：预测、标准、是否正确、证据、召回来源、失败类型
  -> 汇总准确率、证据命中率、分类指标
```

### 5.2 正式评测推荐命令

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

### 5.3 重要评测开关

| 开关 | 当前建议 | 说明 |
| --- | --- | --- |
| `--no-lexical-final` | 正式评测开启 | API 失败时标记 `API_FAILED`，不使用词面兜底 |
| `--force-choice-on-insufficient` | 资料库内选择题可开启 | 证据命中时强制从 A/B/C/D 中选一个 |
| `--only-wrong-from` | 错题重跑时开启 | 只重跑上一轮 `is_correct=false` 的题 |
| LLM cache | 开启 | 避免重复 API 调用 |
| query embedding cache | 开启 | 避免重复 embedding 调用 |

不要混淆两类评测：

| 评测类型 | 推荐设置 |
| --- | --- |
| 资料库内选择题 | 目标是选择 A/B/C/D，可以开启 force-choice-on-insufficient |
| 资料库外拒答题 | 目标是拒答或澄清，必须关闭 force-choice-on-insufficient |

`lexical_fallback` 只适合临时不中断流程，不适合作为正式准确率依据。它的含义是：LLM API 失败或输出不可解析时，用题干、选项、证据的关键词重合度临时猜一个选项。正式评测建议关闭。

## 6. 最新 v7 全量评测结果

输出目录：

`D:\金融科技大赛\Code\data\processed\docling0816\p0_full\eval\qa_hybrid_word_pdf_200_v7_target_strict`

主要输出：

| 文件 | 内容 |
| --- | --- |
| `qa_eval_report.json` | 200 题汇总指标 |
| `qa_eval_details.jsonl` | 每题预测、标准答案、证据、召回来源、失败类型 |
| `qa_eval_error_analysis.md` | 非正确题归因报告 |

### 6.1 总体指标

| 指标 | 数值 |
| --- | ---: |
| 总题数 | 200 |
| 正确数 | 191 |
| 准确率 | 95.5% |
| 证据命中数 | 200 |
| 证据命中率 | 100.0% |
| 二阶段强制选择触发 | 37 |
| LLM cache 本轮写入 | 231 |
| lexical final | 关闭 |

按来源类型：

| 来源 | 题数 | 正确数 | 准确率 | 证据命中率 |
| --- | ---: | ---: | ---: | ---: |
| word | 100 | 93 | 93.0% | 100.0% |
| pdf | 100 | 98 | 98.0% | 100.0% |

按题型：

| 题型 | 题数 | 准确率 | 证据命中率 |
| --- | ---: | ---: | ---: |
| 单事实检索 | 134 | 94.78% | 100.0% |
| 多事实检索 | 66 | 96.97% | 100.0% |

### 6.2 错误归因

| 失败类型 | 数量 | 说明 |
| --- | ---: | --- |
| `api_failed` | 6 | 证据已召回，但 LLM/API 三次重试后仍失败 |
| `llm_choice_error_after_recall` | 3 | 证据已召回，但 LLM 最终选错 |
| `evidence_not_recalled` | 0 | 未发现证据召回失败 |
| `no_retrieval_hits` | 0 | 未发现无召回结果 |

非正确题列表：

| 题号 | 来源 | 题型 | 预测 | 标准 | 归因 |
| --- | --- | --- | --- | --- | --- |
| Q112 | word | 多事实检索 | API_FAILED | B | API 失败 |
| Q114 | word | 单事实检索 | API_FAILED | D | API 失败 |
| Q128 | word | 单事实检索 | API_FAILED | B | API 失败 |
| Q162 | word | 单事实检索 | API_FAILED | D | API 失败 |
| Q168 | word | 单事实检索 | C | B | LLM 证据召回后选错 |
| Q176 | word | 单事实检索 | C | B | LLM 证据召回后选错 |
| Q192 | word | 单事实检索 | C | B | LLM 证据召回后选错 |
| Q282 | pdf | 单事实检索 | API_FAILED | D | API 失败 |
| Q284 | pdf | 多事实检索 | API_FAILED | B | API 失败 |

结论：当前主要瓶颈不是召回覆盖，而是 API 稳定性和少量相似选项的 LLM 判断。若排除 6 道 API 失败，非 API 口径为 `191 / 194 = 98.45%`。

### 6.3 与历史结果对比

| 版本 | 运行方式 | 正确数 | 准确率 | 证据命中率 | 主要问题 |
| --- | --- | ---: | ---: | ---: | --- |
| v6 初次全量 | 旧 prompt，保留 lexical fallback | 189/200 | 94.5% | 100.0% | 11 道证据召回后 LLM 选错 |
| v6 错题重跑合并 | 初跑 + 只重跑 11 道错题 | 198/200 | 99.0% | 100.0% | 剩 Q167、Q176 |
| v7 target strict | 全量重跑，关闭 lexical final，目标资料强约束 | 191/200 | 95.5% | 100.0% | 6 道 API 失败，3 道 LLM 选错 |

说明：v6 合并结果是“初次全量 + 错题重跑”的合并口径，不是一次性全量运行口径。v7 是一次性全量重跑，并且关闭 lexical fallback，因此会真实暴露 API/网络失败。

## 7. 延迟抽样结果

脚本：

`D:\金融科技大赛\Code\rag_doc_ingestion\scripts\run_docling_p3_latency_sample.py`

输出目录：

`D:\金融科技大赛\Code\data\processed\docling0816\p0_full\eval\latency_sample10`

10 题随机抽样结果：

| 指标 | 数值 |
| --- | ---: |
| 样本数 | 10 |
| 正确数 | 10 |
| 准确率 | 100.0% |
| API 失败 | 0 |
| 二阶段触发 | 2 |
| 端到端平均耗时 | 50.16 秒 |
| 端到端 P50 | 43.91 秒 |
| 端到端 P90 | 78.42 秒 |
| 端到端最大 | 78.80 秒 |
| 检索平均耗时 | 6.01 秒 |
| 第一阶段 LLM 平均耗时 | 36.24 秒 |
| 第二阶段 LLM 平均耗时 | 39.55 秒 |

现场演示注意：当前严格评测 prompt 偏长，单题耗时主要在 LLM。演示可以使用缓存、减少 top evidence、关闭或按需触发第二阶段、预热常见问题。

## 8. 赛题指标对照

赛题建议指标与当前状态：

| 指标 | 要求 | 当前状态 |
| --- | ---: | ---: |
| 制度事实类问题准确率 | 不低于 85% | word/pdf 200 题 95.5% |
| 表格取数类问题准确率 | 不低于 80% | 当前 200 题未覆盖 Excel，后续单独测 |
| 证据引用命中率 | 不低于 90% | 100.0% |
| 关键数字、日期、机构名称、文号错误率 | 不高于 5% | 需在结构化答案阶段单独统计 |
| 资料库外或依据不足拒答/澄清率 | 不低于 80% | 需单独构造资料库外问题集 |

## 9. 后续优化建议

1. 先重跑 `api_failed` 题。
   - 这些题证据已经命中，主要是网络/API 问题。
   - 建议继续使用 `--only-wrong-from` + `--no-lexical-final`。

2. 对 Q168、Q176、Q192 做选项级 evidence pack。
   - 每个选项单独在目标资料内检索。
   - 每个选项必须绑定直接支持或反驳的 `chunk_id`。
   - 最终答案只能从 direct evidence 完整支持的选项中选择。

3. 表格题单独增强 DuckDB evaluator。
   - 检查 `table_facts` 是否定位正确行列。
   - 检查 `numeric_values` 是否解析单位、日期、比例、金额。
   - 将 DuckDB 查询结果以结构化证据单独传给 LLM。

4. 构造资料库外拒答集。
   - 必须关闭 force-choice-on-insufficient。
   - 单独评测拒答/澄清率。

5. 演示链路单独优化延迟。
   - 预热 embedding cache 与 LLM cache。
   - 控制 evidence topK。
   - 只在低置信时触发第二阶段。
