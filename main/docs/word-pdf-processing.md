# Word/PDF 处理链路

## 输入与分类

- 原生 DOCX：直接进入 Docling Word Simple Pipeline。
- DOC：先由 LibreOffice 转为 DOCX，再进入相同 Word Pipeline，并保留原 DOC 回溯关系。
- PDF：先检查页面尺寸与 `Rotate`；普通页面使用 PDFium 后端，存在非零 `Rotate` 时使用 `docling_parse`。

所有来源文件只读登记并在处理前后核验 SHA-256。

## Docling 配置

- 固定版本：2.120.1。
- Word：`SimplePipeline + MsWordDocumentBackend`，导出图片并调用内联视觉模型描述图片。
- PDF：Standard Pipeline，启用准确表格结构、标题层级、图片描述与公式识别；OCR 默认关闭。
- 结构化 JSON 是标准化和分块的规范输入；Markdown 与 HTML 用于人工检查。

配置位于 `configs/ingestion/documents`，按 conversion、native_docx、converted_docx 和 pdf 分开管理。

## 输出

每份文档输出结构化 JSON、Markdown、HTML、引用图片资产和质量报告；PDF 额外输出页面预检报告。随后从 JSON 生成统一文档、元素、原子证据、父分块和检索子分块。每个子块均保留来源元素、证据、章节或页码位置。

## 已完成质量结果

| 来源 | 文档 | 元素/证据 | 父块 | 子块 |
|---|---:|---:|---:|---:|
| DOC 转换型 DOCX | 32 | 2699 | 84 | 302 |
| 原生 DOCX | 34 | 8085 | 233 | 2079 |
| PDF | 45 | 8414 | 1073 | 1999 |
| 合计 | 111 | 19198 | 1390 | 4380 |

覆盖率 100%，失败 0。机器图片描述和公式转写在通过质量校验前不会作为可信检索内容使用。

```powershell
cd E:\CPIPC_competition_2026\main
python .\scripts\quality\run_document_regression.py --run-id word-pdf-regression-v0.01-001
```

报告位于 `data_runtime/quality/<run-id>/document_regression_report.md`。
