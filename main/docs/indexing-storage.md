# Dense、BM25 与统一存储

## 最终配置

- Dense：DashScope `text-embedding-v3`，1024 维，单批 10，超时 60 秒，最多重试 3 次。
- BM25：Jieba＋监管术语词典＋中文二元回退词，`k1=1.2`、`b=0.75`。
- 向量库：Docker Qdrant `v1.19.0`，命名向量 `dense` 与 `bm25`，BM25 使用 Qdrant IDF Modifier。
- 事实库：不可变 DuckDB 文件，事实以 `fact_id` 去重并建立指标、期间和来源索引。
- 发布方式：先创建不可变 Collection 和 DuckDB，再执行冒烟检索和一致性校验，最后原子切换 Alias。

Embedding 审计只记录输入 SHA-256、请求标识、数量、Token、可配置估算费用、延迟和错误，不保存原文或 API Key。

## 统一语料

正式语料快照为 `data_runtime/corpus_runs/unified-corpus-v0.01-003`：

- 原文件别名：500
- 规范文档：498
- 唯一检索分块：8,953
- 证据：101,975
- 父分块：1,390
- `chunks.jsonl` SHA-256：`4bc2c92985291d38251c28f6a08685ae16d73bc36e5f673a2567424bf86a9b40`

重复内容只索引一次，所有原文件名保留在 `source_aliases.jsonl`，用于文件名过滤和来源展示。

## 代表性快照结果

快照 `index-snapshot-v0.01-002` 覆盖转换 DOCX、原生 DOCX、PDF、Excel 各 5 个分块：

- DashScope 请求：2 次，均成功，每批 10 条；Token 分别为 2,452 和 2,729。
- Qdrant Points：20。
- DuckDB 事实：24,961。
- Dense＋BM25 冒烟检索：通过。
- 当前 Alias：`trusted_rag_documents_current` → `trusted_rag_documents_v0_01_002`。
- Ruff、mypy：通过；索引测试：6/6 通过。

配置位于 `configs/indexing/representative_v0.01.yaml`，快照清单位于 `data_runtime/index_runs/index-snapshot-v0.01-002/snapshot.json`。
