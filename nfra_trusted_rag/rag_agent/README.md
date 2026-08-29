# 金融监管 Excel 可信结构化问答 Agent

面向金融监管 Excel 附件的可规划问答系统。原始工作簿先被清洗为结构化事实、字典和文档行，再构建为单文件 DuckDB；问答阶段由大模型生成受控 QueryPlan，并由确定性执行器完成文件/Sheet 定位、精确取数、比较计算、文档检索和证据校验。没有配置大模型时自动回退到兼容规则规划器。

大模型只负责理解、拆解和规划，不直接生成 SQL 或猜测答案。结构化操作被编译为参数化查询；文档问题在已定位的文件和 Sheet 内检索。新构建的 DuckDB 同时包含 `source_sheets` 画像和 `document_chunks`，用于混合资源定位和后续语义索引。

## 能力概览

| 环节 | 实现 |
| --- | --- |
| Excel 清洗 | `clean_all_excel.py` 调度专用清洗器，生成事实、字典、模板和参考文档 Parquet。 |
| 单文件数据仓库 | `build_duckdb.py` 构建事实、字典、Sheet 画像和文档块。 |
| 查询规划 | 配置 Qwen 时使用 LLM 优先规划；输出经过白名单和类型校验的 QueryPlan。 |
| 混合资源定位 | 显式文件标题作为硬约束；缺少标题时根据领域、主题、期间、指标和 Sheet 内容定位。 |
| 受控执行 | 参数化查询完成取数、比较和跨期计算；文档检索负责模板、规则和名单问题。 |
| 可信证据 | 返回来源文件、工作表、单元格、期间、单位和口径；条件不足时拒答或要求澄清。 |
| 评测与服务 | 100 道 Excel MCQ 评测；FastAPI 服务和 CLI。 |

## 目录结构

```text
rag_agent/
├── database.py        # 只读 DuckDB repository
├── knowledge_base.py  # 数据仓库与概况封装
├── agent.py           # 可溯源结构化问答入口
├── evaluate.py        # 评测与报告
├── service.py         # FastAPI 服务
└── cli.py             # 命令行入口
```

## 快速开始

```bash
# 1. 清洗 Excel
python clean_all_excel.py \
  --input-dir data/nfra_page_attachments_500 \
  --replace-output

# 2. 构建单文件 DuckDB
python build_duckdb.py --replace

# 3. 查看数据仓库概况
python -m rag_agent.cli info

# 4. 提问
python -m rag_agent.cli ask "根据 Excel 附件《2023年10月人身险公司经营情况表》（工作表：人身保险公司（月度） ），“原保险保费收入”在“本年累计/截至当期”口径下的数值是多少？"

# 5. 评测
python -m rag_agent.cli evaluate --qa-file QA数据.xlsx

# 6. 启动 HTTP 服务
python -m rag_agent.cli serve --port 8000
# GET  /health
# POST /ask
# POST /evaluate
```

## 大模型规划器

默认模式为 `auto`：存在 `DASHSCOPE_API_KEY` 或 `QWEN_API_KEY` 时启用 Qwen Planner，否则使用本地规则回退。

```bash
set DASHSCOPE_API_KEY=your-key
set QWEN_MODEL=qwen3.7-plus
python -m rag_agent.cli ask "2020-01-31保险业经营主题里，哪份 Excel 包含全国保险业汇总的原保险保费收入？"
```

临时禁用大模型可增加 `--no-llm-planner`。Planner 输出五类能力模式：

- `structured_query`：取数、过滤、排名和计算。
- `semantic_retrieval`：模板、规则和解释类文档检索。
- `hybrid`：先定位文件/Sheet，再执行查询或检索。
- `analysis_pipeline`：预留给跨文件、多步分析计划。
- `clarify_or_reject`：条件不足、来源不唯一或口径冲突。

HTTP 服务可通过 `RAG_USE_LLM_PLANNER=false` 关闭 Planner。

如果数据库不在默认位置，可在子命令前传入：

```bash
python -m rag_agent.cli --db path/to/nfra.duckdb info
```

## HTTP 示例

```bash
curl -s http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"根据 Excel 附件《2023年10月人身险公司经营情况表》（工作表：人身保险公司（月度） ），‘原保险保费收入’在‘本年累计/截至当期’口径下的数值是多少？"}'
```

## 评测口径

对 `QA数据.xlsx` 中 `source_type == "excel"` 的 100 道题评测：表格取数 34 道、表格比较 33 道、表格计算 33 道。报告统计总体和分组准确率、证据非空率、证据单元格命中率以及拒答/异常明细。

## 依赖

运行依赖见项目根目录的 `../requirements.txt`。运行时只需要 DuckDB，不需要 PostgreSQL服务、账号、密码或驱动。
