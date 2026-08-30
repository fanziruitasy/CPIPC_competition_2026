# 公共入库基础设施

公共入库基础设施是 Word、PDF、Excel 三条专用处理链路的共同起点，负责只读来源登记、幂等识别、版本化运行和任务状态，不执行文档或表格解析。

## 1. 最终采用规则

- 文件摘要使用 1 MiB 分块计算 SHA-256，适用于大文件且不会一次载入内存。
- `source_id` 由知识库标识和文件内容摘要生成；同一知识库内同内容换名仍识别为同一来源。
- 同时保存原始中文文件名、稳定规范文件名和相对路径，前端展示原始文件名。
- 文件类型通过扩展名和文件签名共同识别，支持 DOC、DOCX、PDF、XLS、XLSX。
- 幂等键由知识库、全部来源摘要和脱敏后的有效配置摘要共同生成。
- 每次新构建使用独立 `run_id` 和运行目录，不覆盖历史结果。
- 配置快照自动掩码 API Key、Token、密码、Secret 和凭证字段。
- 单文件异常隔离，其他文件继续执行；前端只获得脱敏错误摘要。
- 任务状态以每任务一个 UTF-8 JSON 文件原子保存，存储实现位于端口之后，可在不改变应用层的情况下替换。

## 2. 直接执行

在 `main` 目录执行：

```powershell
python .\scripts\ingestion\register_sources.py `
  --source-root "<比赛来源目录>" `
  --knowledge-base-id competition `
  --config .\configs\ingestion\v0.01.yaml
```

仅生成每种格式一份的公共样例：

```powershell
python .\scripts\ingestion\register_sources.py `
  --source-root "<比赛来源目录>" `
  --knowledge-base-id competition-sample `
  --config .\configs\ingestion\v0.01.yaml `
  --limit-per-format 1
```

脚本可以从仓库检出目录直接运行，不要求预先执行可编辑安装。

## 3. 输出目录

```text
data_runtime/
├── ingestion_runs/<run_id>/
│   ├── manifest.json
│   ├── input_manifest.json
│   ├── config_snapshot/effective_config.json
│   ├── converted/
│   ├── parsed/
│   ├── normalized/
│   ├── quality/
│   ├── logs/
│   └── failed/
└── ingestion_jobs/<job_id>.json
```

`manifest.json` 记录配置摘要、代码版本、工具版本、阶段状态和输出产物摘要；`input_manifest.json` 保存来源记录及幂等键；任务 JSON 保存总体状态和逐文件阶段。

## 4. 真实样例结果

公共样例对 DOC、DOCX、PDF、XLS、XLSX 各登记一份，共 5 份文件：

| 项目 | 结果 |
|---|---:|
| 登记文件 | 5 |
| 格式覆盖 | 5/5 |
| 来源摘要复核 | 5/5 一致 |
| 登记失败 | 0 |
| 重复请求幂等复用 | 通过 |
| 配置凭证脱敏 | 通过 |

样例任务标识为 `job_2140432625ea6ac1c47162bf`，运行标识为 `knowledge-ingestion-v0.01-20260829T184700Z-7308b96e`。任务保持 `queued`，由后续专用格式处理器推进。
