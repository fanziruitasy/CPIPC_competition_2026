# Excel 事实处理链路

## 输入

- 来源目录：`Data/03-金融大模型与智能体赛道-南京银行-面向银行业监管制度与统计报表的可信RAG问答/nfra_page_attachments_500`
- 支持格式：`.xls`、`.xlsx`
- 全量文件：389 份，其中 XLS 232 份、XLSX 157 份
- 配置文件：`configs/ingestion/spreadsheets/v0.01.yaml`

原始文件只读登记，并在每份文件处理前后计算 SHA-256。XLS 通过 LibreOffice 转为 XLSX；原生 XLSX 直接进入分类和抽取。内容摘要相同的来源只保留一份知识内容，清单保留去重关系。

## 处理与输出

1. 按文件名和表内结构识别数据集、期间、工作表和表区。
2. 复用已验证的专用清洗器抽取指标、机构、期间、单位、公式和缓存值。
3. 生成统一 `TableFact`、行/单元格 `EvidenceUnit` 和解释性 `ChunkRecord`。
4. 每个文件输出 `document.json`、`elements.jsonl`、`evidence.jsonl`、`chunks.jsonl` 和 `facts.parquet`。
5. 全量事实合并为 `normalized/all_facts.parquet`，供 DuckDB 快照加载；精确数值查询返回对应 Sheet 和单元格证据。

公式保留公式文本与缓存值。公式缺少缓存值时不猜测数值，文档标记为需人工复核。LibreOffice 生成的无效筛选范围仅在内存读取副本中移除，不修改原始文件、转换文件和单元格数据。

## 执行命令

```powershell
conda run -n CPIPC python scripts/quality/run_excel_regression.py `
  --config configs/ingestion/spreadsheets/v0.01.yaml `
  --mode full `
  --run-id excel-regression-v0.01-002
```

运行中断或部分文件失败时，仅重跑失败项：

```powershell
conda run -n CPIPC python scripts/quality/run_excel_regression.py `
  --config configs/ingestion/spreadsheets/v0.01.yaml `
  --mode full `
  --run-id excel-regression-v0.01-002 `
  --resume-failed
```

## 全量质量结果

- 成功：389/389
- 失败：0
- 原文件 SHA-256 前后一致：389/389
- 内容去重：1 份（377 与 376 内容相同）
- 标准事实：24,961 条
- 行/单元格证据：82,809 条
- 检索分块：4,575 个
- 需人工复核：2 份
- `all_facts.parquet` 行数：24,961，与质量摘要一致

运行产物位于 `data_runtime/ingestion_runs/excel-regression-v0.01-002`；完整质量报告位于该目录的 `quality/report.md`。
