# 查询规划、检索融合与可信回答

## 1. 最终采用方案

在线问答复用统一 `QueryPlan`、`ChunkRecord`、`EvidenceUnit` 和 `AnswerRecord` 契约，执行链路如下：

1. 查询规划以确定性中文规则为主，识别法规、文号、机构、指标、期间、单位和数值意图。
2. 规则置信度低于 `0.72` 时才调用 `qwen3.7-plus` 消解歧义；模型只能返回受 Pydantic 严格校验的计划字段，不能生成或执行 SQL。
3. 文档查询按配置选择 `dense_only`、`bm25_only` 或 `dense_bm25`。Dense 使用 DashScope `text-embedding-v3` 1024 维向量，BM25 使用固定快照词表与 Jieba 分词。
4. 双路候选使用 `k=60` 的 RRF 融合去重，再由 `qwen3-rerank` 精排；精排失败时明确记录降级原因并保持 RRF 顺序。
5. 数值查询仅通过 DuckDB 白名单字段和参数化 SQL 执行，支持查询、合计、平均、极值、计数、差额、比率和趋势，保留原始事实及单元格位置。
6. 混合问题合并文档证据与结构化事实。回答前后执行证据充分性、冲突、数字、规范强度和引用 ID 校验；未通过时可信拒答。
7. 每次请求按 `trace_id` 记录查询计划、逐通道候选、RRF/精排结果、模型、请求 ID、Token、延迟、引用和最终状态，不记录凭证或宿主机绝对路径。

版本化配置位于 [`configs/retrieval/v0.01.yaml`](../configs/retrieval/v0.01.yaml)，单次命令入口位于 [`scripts/query/ask.py`](../scripts/query/ask.py)。

## 2. 代表性快照验收结果

验收使用知识库快照 `index-snapshot-v0.01-002`，运行标识为 `stage7-validation-v0.01-006`。

| 查询类型 | 最终状态 | Dense 候选 | BM25 候选 | 精排后候选 | 结构化事实 | 引用 |
|---|---:|---:|---:|---:|---:|---:|
| 法规文档 | answered | 20 | 19 | 8 | 0 | 2 |
| 精确数值 | answered | 0 | 0 | 0 | 1 | 1 |
| 文档与数值混合 | refused | 20 | 19 | 8 | 1 | 0 |

法规文档问题成功返回受引用约束的摘要；精确数值问题回答 `11.94%` 并定位到原工作表单元格。混合问题已获得数值事实，但代表性文档快照不含对应监管门槛，因此系统拒绝补充无证据结论。

详细结果见 [`results.json`](../data_runtime/query_runs/stage7-validation-v0.01-006/results.json)，摘要报告见 [`report.md`](../data_runtime/query_runs/stage7-validation-v0.01-006/report.md)。

## 3. 质量检查

- `main/tests/unit`：80 项通过。
- 查询规划、精排与回答门禁专项测试：12 项通过。
- Ruff：通过。
- Mypy 严格检查：通过。
- Qdrant 当前 Alias：可访问，Collection 状态为 `green`。
- 审计敏感信息扫描：未发现 API Key 字段、环境变量密钥名称或宿主机绝对路径。
