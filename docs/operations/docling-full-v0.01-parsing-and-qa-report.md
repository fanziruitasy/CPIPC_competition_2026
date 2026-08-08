# Docling v0.01 全量解析与质量测试报告

## 1. 结论

本次全量解析已完成，111/111 份源文档均成功生成 MD、JSON、HTML 和相关图片资源；源文件未被修改，产物与 manifest 的哈希全部一致。

质量门禁最终结论为 **FAIL / HOLD**，原因不是解析任务失败，而是两份 PDF 存在不可信的页内定位证据：

- `370`：39 个文本 bbox 越出页面，且超宽统计表被拆为一张不完整的 15×7 表和多段游离数字文本，不能直接用于精确数字问答；
- `373`：两行标题文字实际位于第 3 页，却被 provenance 标为第 2 页，属于引用页错误。

因此，当前结果可以作为候选语料和后续修复输入，但在处理这两份 PDF 并完成 24 份人工复核前，不建议发布为正式向量库数据版本。

## 2. 本次解析范围和方法

运行 ID：`docling-python-v0.01-full-001`  
配置版本 / 流水线版本：`0.01 / 0.01`  
输入目录：`Data/originalData/docANDpdf`  
输出目录：`Data/staging/document_preprocessing/runs/docling-python-v0.01-full-001`

### DOC（32 份）

1. Python 编排调用 LibreOffice `soffice.com`，将旧 `.doc` 转为独立 staging 目录中的 `.docx`；
2. 记录原文件、转换文件 SHA-256、LibreOffice 版本和一对一转换链；
3. 转换后的 DOCX 使用 Docling `WordFormatOption + SimplePipeline + MsWordDocumentBackend`；
4. 不使用 OCR，也不使用 PDF Standard Pipeline。

DOC→DOCX 转换结果：32 成功、0 失败；LibreOffice 版本为 26.2.4.2。原 DOC 保持只读不变。

### 原生 DOCX（34 份）

直接使用 `WordFormatOption + SimplePipeline`。`WordFormatOption` 负责声明 Word 输入的后端和流水线配置，`SimplePipeline` 负责读取 Word 已存在的段落、表格、列表和图片结构，不做 PDF 式页面版面重建。Word 的页码受排版引擎影响，本次 JSON 不把 Word 页码伪装成精确证据。

### PDF（45 份）

使用 `PdfFormatOption + StandardPdfPipeline + PyPdfiumDocumentBackend`：

- CUDA GPU；12 个 CPU 线程；
- OCR 关闭（数据均为文本型 PDF）；
- 表格结构识别开启，`accurate + cell matching`；
- 公式增强和图片分类开启；图片描述/远程服务关闭；
- 生成页面图和图片资源；
- page/layout/table batch 为 8/8/6；
- 不设置文档超时，不自动重试，按文档隔离失败。

## 3. 运行结果

| 格式 | 成功/总数 | 文本节点 | 表格 | 单元格 | 图片 | PDF 页 |
|---|---:|---:|---:|---:|---:|---:|
| DOC | 32/32 | 3,092 | 23 | 855 | 23 | - |
| DOCX | 34/34 | 7,863 | 251 | 13,980 | 494 | - |
| PDF | 45/45 | 9,124 | 140 | 5,700 | 115 | 847 |
| 合计 | 111/111 | 20,079 | 414 | 20,535 | 632 | 847 |

- 产物文件：1,784 个；
- 全量耗时：1,121.281 秒，约 18 分 41 秒；
- 运行状态：success 111、warning 0、failed 0；
- 原始文件哈希不变率：100%。

解析后发现 Docling 最初把 62 份 JSON 中 1,451 个资源 URI 写成临时候选目录的绝对路径；随后确认 27 份 Markdown 中有 604 个普通绝对引用、对应 HTML 中另有 604 个 URL 编码的绝对引用。现已全部改为相对路径、验证目标文件存在，并同步更新 manifest 哈希。未来运行器已在原子发布前自动执行相同规范化。

## 4. 质量检测方法

本次实现并执行了：

- L0 全量硬门槛：清单、状态、源哈希、必需产物、产物哈希、UTF-8/JSON、Docling 模型、JSON Pointer、资源、路径泄露、PDF 页数和 provenance；
- L1 全量保真代理：OOXML/PDFium 独立文本和数字召回、Word 表格数量、Markdown 序列化覆盖、PDF 证据覆盖与表格边界；
- L3 分层抽样：每种格式 8 份，共 24 份，并生成固定种子样本清单和人工评分表；
- 针对 370、373、398、443、459、498、417 做了重点人工 spot-check。

详细方法和后续 L2/L4 设计见 [`document-parsing-quality-evaluation.md`](document-parsing-quality-evaluation.md)，研究依据见 [`document_parsing_quality_evaluation_research.md`](../research/document_parsing_quality_evaluation_research.md)。

## 5. 自动检测结果

| 硬门槛 | 结果 |
|---|---:|
| 输入与成功率 | 111/111，100% |
| MD/JSON/HTML 覆盖率 | 100% |
| 源文件未变化率 | 100% |
| 1,784 个产物哈希一致率 | 100% |
| JSON 可解析 / Docling 模型校验 | 100% / 100% |
| JSON Pointer / 正文可达率 | 100% / 100% |
| JSON / Markdown·HTML 资源引用完整率 | 100% / 100% |
| PDF 独立页数一致率 | 100%（45/45，847 页） |
| PDF 文本 / 表格证据覆盖最低值 | 100% / 100% |
| 绝对路径 / U+FFFD / 非控制字符 Unicode 转义 | 0 / 0 / 0 |
| 非法 PDF provenance | 2 份文档（阻断） |

自动发现 2 条 FAIL 和 122 条 WARN。WARN 构成为：Markdown 数字召回 66、独立源数字召回 22、Markdown 字符召回 21、解析日志 10、独立源字符召回 3。

这些召回指标使用无金标代理。特别是 Markdown 会展平合并表格、跳过部分图片语义或去重单元格，不应把低 Markdown 召回直接解释为 JSON 丢失；但所有低分仍应进入人工队列。

### 分格式代理指标

| 格式 | 独立源字符召回（最小/平均） | 独立源数字召回（最小/平均） | JSON→MD 字符召回（最小/平均） | JSON→MD 数字召回（最小/平均） |
|---|---:|---:|---:|---:|
| DOC | 100% / 100% | 100% / 100% | 68.57% / 94.07% | 50.00% / 84.88% |
| DOCX | 99.94% / 约100% | 97.73% / 99.93% | 79.26% / 96.84% | 50.00% / 91.39% |
| PDF | 91.16% / 99.22% | 49.18% / 91.09% | 98.05% / 99.54% | 52.38% / 86.98% |

DOC/DOCX 的 JSON 与 OOXML 文本、数字总体高度一致；低点主要发生在 Markdown 展平阶段。PDF 低点集中在复杂统计表和公式/数字密集页面，说明精确数字问答不能只依赖 Markdown。

## 6. 解析日志和人工 spot-check

### 解析日志

- 9 份 Word 文档出现 `VML image cannot be found`：494、459、493、500、501、492、503、502、405；
- DOCX 423 的 `sum` 公式出现 7 次 OMML→LaTeX 回退告警；
- 这些告警没有导致文档状态失败，但图片/公式必须由人工检查或在后续金标中覆盖。

### 人工重点检查结论

| 文档 | 结论 | 级别 | 说明 |
|---|---|---|---|
| 370 PDF | 不通过 | P1 | 单页横向超宽统计表；39 个数字/单位文本 bbox 使用了旋转前坐标，越出 1191×842 页面；表格结构仅识别为 15×7，很多数值游离，不能用于精确行列问答。 |
| 373 PDF | 不通过 | P1 | “国务院发布保险业 / 新国十条”实际在第 3 页，但两个节点标为第 2 页；正文存在，引用页不可信。 |
| 398 PDF | 有条件通过 | P2 | 第 44 页 5 个空白函证表单的表头均被识别，空白填写行会被折叠；适合语义检索，不适合还原原始空白表单布局。 |
| 443 DOC | 有条件通过 | P2 | 10/10 张表保留，正文/数字代理 100%；部分章节表现为粗体正文而非标题层级。 |
| 498 DOC | 有条件通过 | P2 | 长文正文完整，目录和多级条目可读；标题层级较多依靠粗体/文本，后续分块需增强规则。 |
| 459 DOCX | 待复核图片 | P2/待定 | 1/1 张表保留，但有 VML 图片缺失告警，Markdown 对 JSON 覆盖偏低；需在人工表中确认缺失图片是否有业务含义。 |
| 417 DOCX | 有条件通过 | P2 | 140/140 张表保留；复杂合并表在 Markdown 中会展平，后续数字 RAG 必须读取 JSON 单元格而不是仅使用 Markdown。 |

这是定向 spot-check，不代替 24 份正式人工签字复核。完整抽样清单和待填写表位于运行目录的 `sample_manifest.json`、`human_review.csv`。

## 7. 下一步处理建议

1. **370 单独路由到表格专用流程**：优先使用原始 Excel（如存在同源表）或旋转归一化后的 PDF；用 Camelot/Tabula 或基于页面网格的表格抽取建立二维单元格，人工核对数字和单位。
2. **373 修复 provenance**：尝试更新 Docling 后重跑该文档，或对第 2/3 页做旋转/页面边界归一化；未修复前，可保留全文检索，但禁止展示该两行的页码引用。
3. **Word VML/公式告警专项复核**：检查 9 份 VML 文档和 423；如图片承载表格、公式或字段含义，则改用 LibreOffice 渲染 PDF 路径或补充资源提取。
4. **数字数据以 JSON 表格为主**：分块时同时产生 `text_chunk` 和 `table_chunk`，表格 chunk 保存表名、表头路径、行名、列名、单位、数值、来源文档和证据位置。
5. **完成 24 份人工抽样**：无 P0/P1 后再把修复结果冻结为基线；从高风险页建立首批金标。

## 8. 报告与机器产物

- 运行摘要：`Data/staging/document_preprocessing/runs/docling-python-v0.01-full-001/reports/summary.json`
- 自动报告：`Data/staging/document_preprocessing/runs/docling-python-v0.01-full-001/quality/quality_report.md`
- 文档级指标：`Data/staging/document_preprocessing/runs/docling-python-v0.01-full-001/quality/document_metrics.jsonl`
- 问题明细：`Data/staging/document_preprocessing/runs/docling-python-v0.01-full-001/quality/findings.csv`
- 抽样清单：`Data/staging/document_preprocessing/runs/docling-python-v0.01-full-001/quality/sample_manifest.json`
- 人工评分表：`Data/staging/document_preprocessing/runs/docling-python-v0.01-full-001/quality/human_review.csv`
