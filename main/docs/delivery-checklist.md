# 比赛交付清单

## 交付内容

| 赛题交付项 | 正式内容 |
|---|---|
| RAG 问答系统或 API | FastAPI 非流式 Chat、SSE Chat、上传、任务、知识库和健康接口 |
| 知识库构建脚本 | Word/PDF Docling JSON 标准化分块、Excel 事实抽取、Dense＋Jieba BM25、Qdrant＋DuckDB 快照构建 |
| 文档解析结果 | Word、转换型 Word、PDF 的 JSON/Markdown/HTML 与质量产物；Excel 的事实和证据产物 |
| 训练/评测问答集 | 官方 QA 的版本化统一评测记录，保留题号、来源范围、题型和难度 |
| 评测报告 | 18道分层代表题、三检索剖面对照、逐题结果、来源覆盖和模型调用统计 |
| 运行说明 | `docs/running-and-reproduction.md` 与 `docs/frontend-api.md` |
| 可复现环境配置 | 固定 Python 依赖、Dockerfile、Compose、Qdrant 版本和持久化目录 |

## 当前正式结果

- 文档语料：500个来源别名、498份去重文档、8,953个可检索 Chunk。
- 数值事实：24,961条 DuckDB 事实。
- 向量快照：Qdrant Collection `trusted_rag_documents_v0_01_003`，稳定 Alias
  `trusted_rag_documents_current`。
- 最终检索剖面：`dense_bm25`，即 `text-embedding-v3` Dense 与 Jieba BM25 经 RRF 融合，随后使用 `qwen3-rerank` 精排。
- 代表性评测：18题；`dense_only` 11题正确，`bm25_only` 12题正确，
  `dense_bm25` 12题正确；三组已回答题均与参考答案一致，来源召回率均为100%。
- `dense_bm25` 分来源结果：Excel 3/6、PDF 4/6、Word 5/6；未回答题为
  Q035、Q068、Q070、Q105、Q203、Q206。

代表性测试用于快速验证系统链路和检索配置，不等同于官方最终隐藏集成绩。模型单价未写入配置，
因此报告保留 Token、请求标识和调用次数，实际费用以 DashScope 控制台为准。

## 交付前检查

```powershell
python scripts/delivery/verify_delivery.py --check-docker
```

核验器只报告疑似凭证所在文件，不打印凭证内容。交付包不得包含 `.env`、`data_runtime/qdrant_storage`、
缓存目录、临时日志或开发期测试目录；大体积解析结果和数据库快照应作为明确命名的数据产物单独提供。
