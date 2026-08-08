# 文档解析质量检测运行手册（v0.01）

## 1. 目的和适用范围

本方案用于每次 DOC、DOCX、PDF 解析完成后判断候选语料是否可以进入分块和向量索引。它不把“程序执行成功”当作“解析质量合格”，而是把完整性、内容保真、结构、数字、表格和证据定位分层检查。

研究依据、指标来源和后续金标指标见 [`document_parsing_quality_evaluation_research.md`](../research/document_parsing_quality_evaluation_research.md)。当前可执行配置为 [`document_quality_v0.01.yaml`](../../configs/preprocessing/document_quality_v0.01.yaml)。

## 2. 每次运行的检测路径

```text
Docling run
  ├─ L0：100% 完整性硬门槛
  ├─ L1：100% 无标注自动保真检查
  ├─ L2：固定人工金标回归（下一阶段）
  ├─ L3：分层人工抽样与所有异常复核
  └─ L4：分块后 RAG 证据可恢复性（分块阶段启用）
```

### L0：硬门槛

- manifest 文档数量、格式分布和唯一 ID 与配置一致；
- `success=100%`，MD、JSON、HTML 覆盖率 100%；
- 解析前后原文件 SHA-256 完全相同；
- manifest 中全部产物大小和 SHA-256 与磁盘文件一致；
- JSON 可解析并通过当前 `DoclingDocument` 模型校验；
- JSON Pointer、图片和页面资源引用全部存在且不越界；
- U+FFFD、非控制字符 Unicode 转义、乱码特征和本机绝对路径为 0；
- PDF 独立页数与 Docling 页数一致，页码和 bbox provenance 合法。

任何 L0 失败均为 `FAIL`，禁止把该运行直接发布为正式下游语料。

### L1：无金标自动检测

- DOC/DOCX：直接读取 OOXML `word/document.xml`，建立独立文本、数字和表格数量参考；
- PDF：用 PDFium 独立读取页数与文本，不复用 Docling 布局结果；
- 计算字符多重集召回、数字 token 多重集召回、JSON 到 Markdown 召回；
- 核查表格行列维度、单元格跨度、PDF 证据页码和 bbox；
- 解析日志中的 VML、公式等警告自动关联到当前文档。

这些指标是异常探针，不是人工金标。低分先进入人工复核，不应为了让运行通过而临时降低阈值。

### L2：固定金标回归

从首次人工确认样本中固化 24～30 个高风险页面，后续加入：

- 文本标准化编辑距离；
- 标题/列表 F1；
- 阅读顺序 Kendall's tau；
- 表格 TEDS、GriTS 和关键数字单元格 exact match；
- PDF 页码和证据框准确率。

### L3：人工抽样

首次全量运行每种来源格式抽 8 份，共 24 份。抽样优先级是“自动异常、最大文件、表格密集、长文档”，再用固定随机种子补足。PDF 默认检查首、中、末页，异常页额外检查。

人工评分维度：正文完整性、阅读顺序、标题层级、列表、表格、数字、图片/公式、证据可追溯性。缺陷分为：

- P0：整页/整文档缺失、源数据变化、不可读、敏感路径泄露；
- P1：数字或单位错误、表格错行错列、阅读顺序改变语义、引用页错误；
- P2：不影响语义的 Markdown 样式、空白或空行问题。

建议发布条件为 P0=0、P1=0，P2 页面占比不超过 5%。

## 3. 执行命令

在项目根目录、CPIPC 环境中执行：

```powershell
conda activate CPIPC
python .\scripts\evaluate_document_run.py --config .\configs\preprocessing\document_quality_v0.01.yaml
```

退出码：0 表示自动门禁没有硬失败；2 表示存在硬失败，适合接入 CI。`PASS_WITH_WARNINGS` 仍须完成抽样复核。

## 4. 输出文件

每次运行在 `Data/staging/document_preprocessing/runs/<run_id>/quality/` 生成：

| 文件 | 用途 |
|---|---|
| `quality_config.yaml` | 本轮阈值与抽样规则快照 |
| `run_metrics.json` / `qa_summary.json` | 全局机器可读结论 |
| `document_metrics.jsonl` | 每文档指标 |
| `findings.csv` | 失败和告警明细 |
| `sample_manifest.json` | 24 份分层抽样清单及原因 |
| `human_review.csv` | 可直接填写的人工复核表 |
| `quality_report.md` / `qa_summary.md` | 人读报告 |

## 5. 处理原则

- JSON 是结构、表格和证据的主记录，Markdown 是面向检索/阅读的派生序列化，不应作为复杂表格的唯一事实来源。
- DOC/DOCX 没有稳定页码。可信引用使用文档 ID、标题路径、节点引用和内容哈希；需要页码时，额外冻结 LibreOffice 版本并生成派生 PDF。
- PDF provenance 失败时，即使正文存在，也不能宣称页码引用可信。
- VML 图片缺失、公式回退和低数字召回必须进入人工队列；不得静默忽略。
- 修改 Docling、Torch、LibreOffice、模型或关键配置后，恢复 24 份首轮抽样强度并运行全部金标。

