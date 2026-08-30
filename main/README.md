# 银行业监管制度与统计报表可信 RAG 系统

本目录是比赛最终整合代码的唯一正式目录。根目录现有代码、`LYJ` 和 `ZRY` 仅作为迁移来源，不在这里创建嵌套 Git 仓库，也不复制原始比赛数据或真实凭证。

## 当前工程入口

```powershell
conda activate CPIPC
cd E:\CPIPC_competition_2026\main
python -m pip install -e ".[dev]"
ruff check src tests
python -m mypy src tests
python -m pytest
```

公共来源登记入口：

```powershell
python .\scripts\ingestion\register_sources.py --help
```

该入口只读登记来源并创建版本化运行和异步任务，不执行解析。详细格式与命令见
[公共入库基础设施](./docs/public-ingestion-foundation.md)。完整 Docker 启动、建库、问答、评测和
交付核验命令见[运行与复现说明](./docs/running-and-reproduction.md)，最终交付内容和实际结果见
[比赛交付清单](./docs/delivery-checklist.md)。

Word/PDF 最终处理链路、配置与全量质量结果见
[Word/PDF 处理链路](./docs/word-pdf-processing.md)。

## 目录职责

| 目录 | 职责 |
|---|---|
| `src/trusted_rag/api` | 前端 HTTP、SSE 接口与请求响应适配 |
| `src/trusted_rag/application` | 入库和问答用例编排 |
| `src/trusted_rag/domain` | 稳定领域契约与端口协议 |
| `src/trusted_rag/ingestion` | 文档、表格、标准化与分块实现 |
| `src/trusted_rag/retrieval` | 查询规划、召回、融合和精排 |
| `src/trusted_rag/answering` | 证据约束回答、拒答和引用 |
| `src/trusted_rag/storage` | Qdrant、DuckDB 与文件产物适配器 |
| `src/trusted_rag/evaluation` | 数据集、指标、评测和报告 |
| `src/trusted_rag/infrastructure` | 配置、日志、标识和外部客户端基础设施 |
| `configs` | 不含凭证的版本化业务配置 |
| `scripts` | 可由用户直接执行的正式入口 |
| `tests` | 单元、契约、集成与端到端测试 |
| `docs` | 最终运行、接口、数据与交付说明 |
| `resources` | 词典、模式等受版本管理的静态资源 |
| `docker` | 镜像构建资源 |
