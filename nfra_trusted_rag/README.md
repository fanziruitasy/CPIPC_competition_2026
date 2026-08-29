# 金融监管 Excel 可信 RAG 问答

本项目面向银行业监管制度与统计报表问答，采用“LLM 生成受控 QueryPlan，确定性程序执行”的架构。

核心能力：

- 显式文件标题作为硬约束；没有标题时，根据领域、主题、期间、指标和工作表画像动态定位 Excel 与 Sheet。
- `structured_query`、`semantic_retrieval`、`hybrid`、`analysis_pipeline`、`clarify_or_reject` 五类能力路由。
- 参数化 DuckDB 查询，不允许大模型自由生成和执行 SQL。
- 支持取数、比较、差值、聚合和限定时间范围的趋势查询。
- 返回来源文件、Sheet、单元格、期间、单位和口径证据。

资源定位逻辑不会读取测试答案，也没有写死具体文件名。测试中的固定答案只用于断言结果是否正确。

## 快速开始

```powershell
python -m pip install -r requirements.txt
```

仓库不包含原始 Excel、评测集、清洗产物或 DuckDB。请在本地准备：

```text
data/nfra_page_attachments_500/   # 原始 .xls/.xlsx
QA数据.xlsx                       # 可选，旧评测集
Excel新增评测集_v1.xlsx           # 可选，新增评测集
```

然后构建本地数据库：

```powershell
python clean_all_excel.py --input-dir data/nfra_page_attachments_500 --replace-output
python build_duckdb.py --replace
python -m rag_agent.cli info
```

数据库准备完成后即可提问：

```powershell
python -m rag_agent.cli ask "根据 Excel 附件《2023年10月人身险公司经营情况表》（工作表：人身保险公司（月度） ），“原保险保费收入”在“本年累计/截至当期”口径下的数值是多少？"
```

配置 Qwen 后，系统会优先使用大模型理解问题；未配置时自动使用规则规划器：

```powershell
$env:DASHSCOPE_API_KEY="your-key"
$env:QWEN_MODEL="qwen3.7-plus"
python -m rag_agent.cli ask "查询2020年银行业金融机构总资产全年趋势"
```

临时禁用大模型：

```powershell
python -m rag_agent.cli ask "问题" --no-llm-planner
```

## 验证

```powershell
python -m unittest discover -s tests -v
python -m rag_agent.cli evaluate --qa-file QA数据.xlsx --no-llm-planner
```

没有本地数据库时，2 个数据库集成测试会自动跳过；规划器单元测试仍可直接运行。完整数据环境的回归结果为：旧 Excel 评测集 82/100，新增评测集 80/80，自动化测试 6/6 通过。

更完整的架构和接口说明见 [`rag_agent/README.md`](rag_agent/README.md)。
