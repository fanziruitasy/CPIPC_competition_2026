# 运行与复现说明

## 1. 固定环境

- Docker Desktop，启用 Linux 容器。
- API 与构建器：Python 3.12.11，项目版本 `0.1.0`。
- Qdrant：`1.19.0`；Docling：`2.120.1`；DuckDB：`1.5.5`。
- Dense：DashScope `text-embedding-v3`，1024维。
- 词法检索：Jieba `0.42.1` 与 BM25（`k1=1.2`、`b=0.75`）。
- 构建器内置 LibreOffice，用于旧版 DOC/XLS 转换；宿主机无需另行安装。

Python 包精确版本以 `pyproject.toml` 为准，基础镜像以 `docker/Dockerfile` 中的摘要为准。

## 2. 凭证准备

在仓库根目录创建 `.env`，按照 `main/.env.example` 填写 `DASHSCOPE_API_KEY`。
`.env` 只在本机使用，不得提交或放入交付包。Embedding、Rerank、查询规划和回答生成
会调用 DashScope，因此首次建库和在线问答需要可用网络并产生模型费用。

## 3. 构建并启动

```powershell
cd E:\CPIPC_competition_2026\main
docker compose build rag-api knowledge-builder
docker compose up -d qdrant rag-api
docker compose ps
```

服务地址为 `http://127.0.0.1:8000`，OpenAPI 地址为 `http://127.0.0.1:8000/docs`。

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
Invoke-RestMethod http://127.0.0.1:8000/api/v1/knowledge-bases/nfra-regulations
```

Qdrant、DuckDB、审计和运行产物持久化在 `main/data_runtime`。重新启动不会重建知识库：

```powershell
docker compose restart qdrant rag-api
```

## 4. 知识库构建

构建器只读挂载仓库的 `Data`，写入 `main/data_runtime`。正式顺序如下：

```powershell
docker compose --profile tools run --rm knowledge-builder `
  python scripts/indexing/build_unified_corpus.py --run-id unified-corpus-v0.01-003

docker compose --profile tools run --rm knowledge-builder `
  python scripts/indexing/build_snapshot.py `
  --config configs/indexing/production_v0.01.yaml --activate
```

Word/PDF 使用 Docling JSON 作为规范输入；Excel 进入结构化事实链路。统一快照验收通过后，
脚本才会把 `trusted_rag_documents_current` Alias 切换到新 Collection。

## 5. API 调用

```powershell
$body = @{
  knowledge_base_id = "nfra-regulations"
  question = "商业银行监管评级包含哪些要素？"
  retrieval_profile = "dense_bm25"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/chat `
  -ContentType "application/json" -Body $body
```

文件上传、任务查询、知识库状态、非流式 Chat 和 SSE Chat 的完整契约见
[前端 API](./frontend-api.md)。回答包含来源文件、证据位置、引用和 `trace_id`。

## 6. 代表性评测

固定18题覆盖 Word、PDF、Excel，以及三档难度和三种题型；三个检索剖面使用同一快照：

```powershell
docker compose --profile tools run --rm knowledge-builder `
  python scripts/evaluation/run_official_qa.py `
  --config configs/evaluation/representative_v0.01.yaml --resume
```

报告位于 `data_runtime/evaluation_runs/representative-qa-evaluation-v0.01-001/report.md`。

## 7. 交付核验

```powershell
python scripts/delivery/verify_delivery.py --check-docker
ruff check src tests scripts
python -m mypy src tests
python -m pytest
docker compose config --quiet
```

停止服务使用 `docker compose down`。不要添加 `-v`，以免删除知识库持久化数据。
