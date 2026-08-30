# 前端 API

## 1. 服务定位

可信 RAG API 使用 FastAPI 提供知识库状态、异步入库登记、非流式问答和 SSE
流式问答。当前接口无需登录和角色鉴权，所有回答均返回 `trace_id`，并通过引用对象
关联原始文件、页码或文档结构位置。接口不会返回服务器绝对路径和模型密钥。

## 2. 启动与检查

在 `main` 目录执行：

```powershell
python .\scripts\api\run.py
python .\scripts\api\smoke.py
```

默认监听 `0.0.0.0:8000`。可通过 `RAG_API_HOST`、`RAG_API_PORT` 和
`RAG_API_WORKERS` 调整监听参数。OpenAPI 地址为 `/openapi.json`，交互文档地址为
`/docs`。

健康检查：

- `GET /health/live`：检查 API 进程存活。
- `GET /health/ready`：检查固定索引清单、Jieba BM25、DuckDB 和 Qdrant Alias。

## 3. 知识库状态

`GET /api/v1/knowledge-bases/{knowledge_base_id}` 返回当前语料和索引快照：

```json
{
  "knowledge_base_id": "nfra-regulations",
  "snapshot_id": "index-snapshot-v0.01-003",
  "collection_alias": "trusted_rag_documents_current",
  "source_file_count": 500,
  "unique_document_count": 498,
  "corpus_chunk_count": 8953,
  "indexed_chunk_count": 8953,
  "fact_count": 24961,
  "status": "ready"
}
```

`source_file_count` 包含输入别名记录，`unique_document_count` 是内容去重后的文档数，
`corpus_chunk_count` 是统一语料分块数，`indexed_chunk_count` 是当前 Qdrant 快照实际点数。

## 4. 文件入库任务

`POST /api/v1/knowledge-bases/{knowledge_base_id}/ingestion-jobs` 使用
`multipart/form-data` 的重复 `files` 字段上传 DOC、DOCX、PDF、XLS 或 XLSX。建议通过
`Idempotency-Key` 请求头传入前端业务幂等键。接口立即返回持久化任务，不阻塞等待解析：

```json
{
  "job": {
    "job_id": "ingestion_job_...",
    "status": "queued",
    "files": []
  },
  "reused_existing_job": false
}
```

`GET /api/v1/ingestion-jobs/{job_id}` 查询任务与逐文件状态。同一输入、配置和代码版本
会复用已有任务。扩展名或文件签名不合法时返回统一 `422` 错误。

## 5. 非流式问答

`POST /api/v1/chat` 请求示例：

```json
{
  "question": "某项监管要求是什么？",
  "knowledge_base_id": "nfra-regulations",
  "user_id": "user-001",
  "session_id": "session-001",
  "retrieval_profile": "dense_bm25"
}
```

`retrieval_profile` 可省略，或取 `dense_only`、`bm25_only`、`dense_bm25`。响应包含
答案状态、回答正文、可信拒答原因、检索摘要、引用、模型用量和追踪标识。前端应按
`answer.status` 区分 `answered` 与 `refused`，不得把拒答原因拼接成事实答案。

可选请求头 `X-Trace-ID` 用于前后端联调；未提供时由服务生成。

## 6. SSE 流式问答

`POST /api/v1/chat/stream` 使用与非流式问答相同的 JSON 请求，响应类型为
`text/event-stream`。事件顺序为：

1. `status`：开始处理及 `trace_id`。
2. `answer_delta`：存在回答正文时发送。
3. `citations`：完整引用列表。
4. `usage`：模型与 Token 用量。
5. `completed`：与非流式接口语义一致的最终答案对象。

处理异常时发送 `error`，其中只包含稳定错误码、安全提示和 `trace_id`，不会发送内部
异常、绝对路径或凭证。

## 7. 统一错误

HTTP 错误统一为：

```json
{
  "code": "http.404",
  "message": "知识库不存在。",
  "trace_id": "trace-..."
}
```

请求字段校验使用 `request.invalid`，未捕获的服务异常使用 `internal.error`。前端记录
`trace_id`，服务端据此关联规划、检索、精排、生成与引用审计。

## 8. 当前验收结果

接口契约测试覆盖 OpenAPI、存活检查、知识库统计、多文件幂等上传、任务查询、非流式
回答、SSE 完成事件和 SSE 安全错误事件。真实代表性快照冒烟检查已验证 Qdrant Alias、
DuckDB、BM25 词表与快照清单可同时加载。
