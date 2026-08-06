# Docling Python API v0.01 运行说明

## 目标

该版本只重新处理样本 398、399。运行入口直接使用 Docling Python API，不调用
`docling convert` CLI。一个 `DocumentConverter` 在同一进程内依次处理两份 PDF，
从而复用布局、表格、公式和图片分类模型。

## 配置边界

配置文件为 `configs/preprocessing/docling_python_v0.01.yaml`。

- `project`：项目版本、输入输出目录、样本、硬件、输出格式、短文件名和 JSON 编码。
- `docling`：Docling 版本、全局 runtime settings、`PdfPipelineOptions`、转换参数和导出参数。

`docling.pdf_pipeline_options` 直接映射至 Docling 2.118.0 的
`PdfPipelineOptions`。该版本全部允许字段记录在
`configs/preprocessing/schemas/docling-2.118.0-pdf-pipeline-options.schema.json`。
每次实验应复制配置文件、修改 `project.config_version`、`pipeline_version` 和
`run_id`，不得覆盖旧运行目录。

## 硬件策略

- CUDA 必须可用，目标显卡为 RTX 4060 Laptop 8GB。
- CPU 使用 8 个线程，对应 8 个物理核心，避免 16 个逻辑核心造成线程过量竞争。
- 页面、布局和表格批大小为 4，适配 8GB 显存；后续实验可通过配置调整。
- `torch.compile` 在 Windows 上关闭，避免 Triton 错误；模型推理仍使用 CUDA。
- 不设置文档超时，不自动重试。

## 手动执行

在 PowerShell 中运行：

```powershell
conda activate CPIPC
Set-Location 'E:\CPIPC_competition_2026'

python .\scripts\run_docling_python.py `
  --config .\configs\preprocessing\docling_python_v0.01.yaml `
  --check-only

python .\scripts\run_docling_python.py `
  --config .\configs\preprocessing\docling_python_v0.01.yaml
```

`--check-only` 只验证配置和源文件，不创建运行目录、不加载模型、不执行转换。

另开一个 PowerShell 监控 GPU：

```powershell
nvidia-smi -l 2
```

运行日志实时显示在当前终端，同时保存到：

```text
Data/staging/document_preprocessing/runs/
  docling-python-v0.01-pdf-retry-001/logs/run.log
```

完成摘要位于：

```text
Data/staging/document_preprocessing/runs/
  docling-python-v0.01-pdf-retry-001/reports/summary.json
```

成功条件是命令退出码为 0，摘要中 `success` 为 2、没有 `failed`，且
`all_sources_unchanged` 为 `true`。

## 注意事项

- 脚本拒绝覆盖已存在的 `run_id`。再次实验时必须复制配置并修改版本和 `run_id`。
- 不要在运行中关闭终端；模型首次加载可能较慢。
- 原始 PDF 始终只读。解析使用 `normalized_sources/398/398.pdf` 和
  `normalized_sources/399/399.pdf` 两份哈希一致的短名副本。
- JSON 导出后会进行原子 UTF-8 重写和语义等价校验，中文不会写成 `\uXXXX`。
