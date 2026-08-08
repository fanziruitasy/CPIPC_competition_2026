# DOC/DOCX/PDF 解析质量评估方案调研

> 适用项目：银行监管制度与统计报表可信 RAG 文档预处理  
> 调研日期：2026-08-07  
> 适用流水线：`.doc → LibreOffice → .docx → Docling`、`.docx → Docling`、`.pdf → Docling StandardPdfPipeline`

## 1. 结论先行

解析任务不能只检查“程序是否退出成功”。建议在每次运行后固定执行四层质量检测：

1. **100% 运行完整性门禁**：输入、输出、状态、哈希、编码、JSON 引用和资源引用全部检查。
2. **100% 无标注自动检测**：文本/数字覆盖率、页码证据覆盖率、文档结构、表格结构和异常值检测。
3. **固定金标集回归检测**：用人工校对的页面计算文本 NED、阅读顺序、标题/列表 F1、表格 TEDS/GriTS、数字精确召回率。
4. **分层人工复核**：首轮建立基线时覆盖 24 份文档；以后每轮复核固定金标页、分层随机样本以及所有自动告警对象。

对于可信 RAG，建议采用以下发布原则：

- `FAILURE`、`PARTIAL_SUCCESS`、漏文档、源文件哈希变化、JSON 断链、资源断链、乱码、关键数字丢失均为**阻断发布**。
- 表格不能只查看 Markdown。Docling 官方说明 Markdown 会展平 `rowspan/colspan`；复杂表格应以 Docling JSON 或 HTML 为质量主记录，Markdown 只作为检索用序列化结果。[Docling Serialization](https://docling-project.github.io/docling/concepts/serialization/)
- Docling 的 `mean_grade/low_grade` 可用于筛出待人工复核文档，但不能替代表格检查；其 `table_score` 当前仍未实现，且官方建议关注等级而非绑定具体浮点分数。[Docling Confidence Scores](https://docling-project.github.io/docling/concepts/confidence_scores/)
- PDF 的“文字都出现了”仍不等于解析正确。监管制度要检查条款阅读顺序，统计报表要重点检查“表头—行名—数值—单位”的关系和页码证据。

本文中的数值阈值是针对本项目的**首版验收建议**，不是论文或 Docling 官方规定。首轮运行后应保存分布和缺陷，后续只能通过版本化变更调整阈值，不能临时降低阈值使失败结果通过。

## 2. 研究依据

### 2.1 Docling 能提供哪些质量信号

`ConversionResult` 原生包含 `status`、`errors`、`pages`、`timings`、`confidence` 和 `document`。状态枚举包括 `success`、`partial_success`、`failure`、`skipped` 等，因此运行器应保存 Docling 原始状态，而不是仅以“生成了 Markdown”为成功。[Docling DocumentConverter API](https://docling-project.github.io/docling/reference/document_converter/)

`DoclingDocument` 是 Pydantic 类型，正文、页眉页脚、文本、表格、图片、分组及父子关系通过 JSON Pointer 组织；正文树中子节点的排列体现阅读顺序。解析后的 JSON 因而可以做模式校验、引用可达性、结构一致性和阅读顺序检测。[Docling Document model](https://docling-project.github.io/docling/concepts/docling_document/)

Docling 对 PDF 的能力来自版面分析、阅读顺序和 TableFormer 表格结构识别。其技术报告明确将高质量转换视为版面和表格识别共同作用的结果。[Docling Technical Report](https://arxiv.org/abs/2408.09869)

### 2.2 业界/学术界如何评价文档解析

- **端到端文本**：OmniDocBench 使用标准化编辑距离，并同时评价表格 TEDS、公式和阅读顺序；它覆盖金融报告等多类真实文档，也提供块级位置和阅读顺序标注。因此，本项目不应只用字符数比值评价解析。[OmniDocBench 论文与官方代码](https://github.com/opendatalab/OmniDocBench)
- **版面检测**：DocLayNet 用 11 类人工框标注和 mAP 评价版面检测，并通过重复标注估计人工一致性；其数据中特别包含金融报告、法律/政府类和手册等复杂版式。[DocLayNet](https://arxiv.org/abs/2206.01062)
- **表格**：TEDS 将 HTML 表格视为树，比较结构、跨行/跨列和单元格内容；GriTS 直接在二维网格上统一评价拓扑、位置和内容。两者都比简单比较 Markdown 行数更适合复杂统计报表。[PubTabNet/TEDS](https://www.ecva.net/papers/eccv_2020/papers_ECCV/papers/123660562.pdf)、[GriTS](https://arxiv.org/abs/2203.12555)
- **阅读顺序**：ReadingBank 说明错误顺序会直接伤害表单和信息抽取任务；对于同一组块的排序，可用 Kendall's τ 或标准化序列编辑距离评价。[LayoutReader/ReadingBank](https://arxiv.org/abs/2108.11591)、[Kendall's τ 信息排序评价](https://aclanthology.org/J06-4002/)
- **抽样**：NIST 建议用分层和随机化减少系统抽样偏差；抽检的目的主要是判断一批结果是否可接受，而不能代替 100% 自动检测。[NIST Sampling Scheme](https://www.itl.nist.gov/div898/handbook/ppc/section3/ppc332.htm)、[NIST Acceptance Sampling](https://www.itl.nist.gov/div898/handbook/pmc/section2/pmc21.htm)

## 3. 四层质量检测架构

```text
原始文件清单
    │
    ├── L0：运行与数据完整性（全部文件，硬门禁）
    │
解析结果 ── L1：无金标自动检测（全部文件，硬门禁 + 告警）
    │
    ├── L2：固定金标集回归（文本/结构/表格/阅读顺序）
    │
    ├── L3：分层人工复核（固定样本 + 随机样本 + 全部异常）
    │
    └── L4：RAG 证据可恢复性测试（在分块/索引完成后启用）
```

### L0：运行完整性

对每次运行的全部文件执行，任何一项失败都阻断发布。

| 指标 | 计算方式 | 建议阈值 |
|---|---|---:|
| 输入清单覆盖率 | `manifest 中唯一源文件数 / inventory 源文件数` | 100% |
| 输出覆盖率 | 同一源文件所需 JSON、MD、HTML 等是否齐全 | 100% |
| 转换成功率 | `status=success / 输入文件数` | 100% |
| 部分成功/失败数 | Docling 原始状态及异常 | 0 |
| 源文件完整性 | 运行前后 SHA-256 相同 | 100% |
| 唯一映射 | 一个源文件对应一个稳定 `document_id`，无重复、无覆盖 | 100% |
| 版本可复现性 | 配置哈希、Git commit、Docling/Torch/CUDA/LibreOffice 版本齐全 | 100% |

SHA-256 用于发现源文件是否被修改，符合 NIST 对消息摘要“检测生成摘要后消息是否变化”的用途说明。[NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final)

### L1：无金标自动检测

这层不需要人工标注，适合每次全量运行。需要区分两类结果：

- **硬门禁**：确定是工程错误，例如 JSON 无法读取、引用断链、文件为空。
- **质量告警**：可能是解析错误，也可能是源文档特征，例如文本覆盖率较低、标题层级跳跃。告警必须进入人工复核队列，不能直接当成失败或忽略。

### L2：固定金标集

从真实赛题语料中人工建立一个小而稳定的、版本化的参考集。每个金标页至少保存：

- 正文块及规范化文本；
- 正确阅读顺序；
- 标题层级和列表关系；
- 表格 HTML/二维网格、跨行跨列关系和单元格文本；
- 关键数字、单位、日期、百分比；
- PDF 页号以及证据区域。

金标集必须固定，不能每次随机重建，否则无法判断代码/配置变化是否造成回归。

### L3：人工复核

人工对照源文件可发现自动指标不易识别的“语义关系错误”，例如：

- 一段文字完整，但被放到了错误标题下面；
- 数字没有丢失，但错配到另一行或另一列；
- 页眉页脚进入正文，影响条款语义；
- 跨页表格被拆成两个无关联表格；
- 表注、单位、脚注与对应表格分离。

### L4：RAG 证据可恢复性

解析 QA 和最终 RAG QA 应分层，不能让大模型答案掩盖解析错误。分块和索引完成后，在金标集上建立至少 50 个问题：条款类、数值/表格类、跨段引用类。先评价目标证据是否进入 chunk，再评价 `Recall@k`、`nDCG@10`、答案忠实度和引用正确率。BEIR 展示了跨多类语料使用 nDCG 等指标评估检索的重要性；ARES 和 RAGChecker 都强调将检索与生成分开诊断，并保留少量人工标注进行校准。[BEIR](https://arxiv.org/abs/2104.08663)、[ARES](https://aclanthology.org/2024.naacl-long.20/)、[RAGChecker](https://arxiv.org/abs/2408.08067)

## 4. 自动指标定义和阈值

### 4.1 文本完整性

先建立两个规范化视图，原始文本始终保留：

1. `text_nfc`：Unicode NFC、统一换行、折叠连续空白，用于普通文字比较。
2. `number_strict`：只做全角/半角和常见空白规范化，保留正负号、小数点、千分位、百分号、币种和单位，用于关键数值比较。

建议指标：

| 指标 | 定义 | 应用 | 首版阈值 |
|---|---|---|---:|
| 标准化编辑距离 NED | `Levenshtein(pred, gold) / max(len(pred), len(gold))` | 金标页全文，顺序敏感 | ≤ 0.02 |
| 字符袋召回率 BCR | 各字符频次交集 / 参考字符总数 | 无金标覆盖代理，弱化阅读顺序影响 | 文档 ≥ 0.95；中位数 ≥ 0.98 |
| 关键数字召回率 | 规范化数字 token 命中数 / 参考数字 token 数 | 全格式，银行文档重点 | 自动代理 ≥ 0.995；金标 100% |
| 非空正文率 | Markdown/JSON 正文字符数是否大于最低值 | 防止空输出 | 100% 非空 |
| 异常压缩比 | 解析字符数 / 独立提取器字符数 | 发现整页/大段缺失 | `<0.85` 或 `>1.20` 告警 |

无金标时的“参考文本”是独立提取器提供的**哨兵信号**，不是绝对真值：

- DOCX：直接读取 OOXML 中段落、表格、页眉页脚文本。
- PDF：使用另一种程序化文本提取器按页读取文本。
- DOC：先检查 LibreOffice 转换清单，再以转换后的 DOCX OOXML 为 Docling 阶段参考；另外通过人工样本检查 DOC→DOCX 是否损失内容。

由于独立提取器也可能存在顺序、连字、页眉页脚等差异，BCR/压缩比低于阈值先告警，再由金标或人工确认。

### 4.2 编码、隐私和路径泄漏

全部 JSON、Markdown、HTML 必须：

- 严格按 UTF-8 解码；
- 不出现 U+FFFD `�`、常见 mojibake（如 `锟斤拷`、`Ã`、`â€`）；
- 不出现 `E:\...`、用户目录、临时目录、`file://` 或 staging 绝对路径；
- JSON 解析成功，不允许未配对代理项；
- 输出内容中的中文可直接阅读。

RFC 8259 规定 JSON 可用 `\uXXXX` 表示 Unicode 字符，也要求开放系统中的 JSON 使用 UTF-8。因此，Unicode 转义在语义上不是错误；本项目将“中文直接可读”作为额外可维护性规范，序列化时使用 `ensure_ascii=False`。[RFC 8259](https://www.rfc-editor.org/rfc/rfc8259)

U+FFFD 是 Unicode 对无法映射或错误编码序列的替代字符，正常监管语料中出现它应视为编码缺陷。[Unicode Standard：Replacement Character](https://www.unicode.org/versions/Unicode16.0.0/core-spec/chapter-2/)

建议门禁：上述问题数全部为 0。

### 4.3 JSON 结构和引用完整性

Docling JSON 的 `$ref`、`self_ref`、父子节点和正文树应进行以下检查：

- JSON 能通过当前运行保存的 DoclingDocument JSON Schema；
- 每个 `self_ref` 唯一；
- 每个 `$ref` 可以从文档根解析；
- `parent/children` 双向关系一致；
- `body` 可达的内容节点无循环引用；
- 非 furniture 的文本、表格、图片节点都可以从 `body` 到达；
- 表格、图片、caption、footnote 引用均存在；
- Markdown/HTML 中的相对资源路径存在且没有越出 artifacts 目录；
- 不允许同一资源路径被不同文档意外覆盖。

JSON Pointer 的解析方式应遵循 RFC 6901，包括 `~0`、`~1` 的解码顺序。[RFC 6901](https://www.rfc-editor.org/rfc/rfc6901)

建议门禁：断链、循环、越界路径、缺失资源均为 0；正文节点可达率 100%。

### 4.4 页和证据覆盖率

PDF 是本项目页码级可信引用的主要对象。建议定义：

```text
页面覆盖率 = Docling 已解析页数 / 独立 PDF 页数
正文证据覆盖率 = 具有合法 provenance 的正文节点数 / 可引用正文节点数
关键证据覆盖率 = 具有合法 provenance 的表格和含关键数字节点数 / 对应节点数
```

合法 provenance 至少要求：

- `page_no` 在 `1..page_count`；
- bbox 坐标有限、面积大于 0，并落在页面边界容差内；
- charspan 不反向、不越界；
- 页码与可视化叠框位置一致。

建议阈值：

- PDF 页面覆盖率：100%；
- 正文证据覆盖率：≥99%；
- 表格和关键数字证据覆盖率：100%；
- 任一源 PDF 非空页没有正文/表格/图片节点：告警并人工检查。

DOCX 的分页取决于排版引擎，不能将其页码当成稳定证据主键。对 DOC/DOCX 建议使用 `document_id + 标题路径 + 节点 self_ref/段落序号 + 内容哈希`；如果比赛界面必须显示页码，再额外生成固定 LibreOffice 版本的渲染 PDF，并将该 PDF 页号作为派生证据。

### 4.5 标题、列表和阅读顺序

无金标自动检查：

- 标题文本不能全为空；
- 标题层级突然跨越两级以上时告警；
- 列表组必须至少含一个 list item，item 必须可达；
- 页眉/页脚重复文本不应大量进入正文；
- PDF 多栏页、文本框页、跨页表格页自动加入人工抽检。

金标指标：

| 指标 | 计算方式 | 首版阈值 |
|---|---|---:|
| 标题识别 F1 | 标题文本匹配后比较 title/section_header | ≥ 0.95 |
| 标题层级准确率 | 匹配标题的 level 正确率 | ≥ 0.95 |
| 列表项 F1 | 列表项边界与归组 | ≥ 0.95 |
| 阅读顺序 Kendall's τ | 匹配块的相对顺序 | ≥ 0.95 |
| 阅读顺序标准化编辑距离 | 块 ID 序列编辑距离 / 较长序列长度 | ≤ 0.05 |

如果同一页面存在多个合理顺序，金标应允许多参考顺序或部分序约束，避免把人工可接受的差异判成错误。DocLayNet 的重复人工标注结果也表明复杂版面存在天然标注不确定性。[DocLayNet](https://arxiv.org/abs/2206.01062)

### 4.6 表格质量

#### 无金标硬检查

- `num_rows/num_cols > 0`；
- 单元格的起止行列和 `row_span/col_span` 均在表格边界内；
- 网格中不存在非法重叠、负索引、孤立引用；
- 表格导出为 DataFrame/HTML 成功；Docling 官方提供逐表导出 DataFrame、CSV 和 HTML 的接口。[Docling Export Tables](https://docling-project.github.io/docling/_generated/examples/export_tables/)
- Markdown 中出现的表格数与 JSON 中可导出的表格数一致；
- 表格内容中所有关键数字都能在 JSON 单元格网格中找到；
- 表格 caption、footnote、单位说明仍与表格关联。

#### DOCX 可自动对照的代理指标

OOXML 是较强的结构参考，可比较：

- 表格数量召回率：100%；
- 每张表行列形状完全匹配率：≥99%；
- 合并单元格关系匹配率：≥99%；
- 单元格关键数字召回率：100%；
- 单元格文本 NED：≤0.01。

DOC 经 LibreOffice 转成 DOCX 后使用相同检查，但首轮必须对 `.doc → .docx` 单独进行人工复核，避免两个阶段的问题相互掩盖。

#### 金标指标

- TEDS：比较 HTML 树结构和单元格内容；
- TEDS-S：忽略单元格文字，单独评价结构；
- GriTS-Top：网格拓扑；
- GriTS-Con：网格内容；
- 数字单元格 exact match：银行统计表的关键指标。

首版建议：普通表格 TEDS/TEDS-S ≥0.95；复杂合并、无框线或跨页表格 ≥0.90，且不得出现数字错列；所有金标数字单元格 exact match 为 100%。如果资源有限，优先实现 TEDS + 数字 exact match，第二阶段补 GriTS。

## 5. 分格式检测策略

### 5.1 `.doc`

```text
原始 DOC
  → SHA-256、大小、文件名和转换日志
  → LibreOffice headless 转 DOCX
  → 检查输出可打开、非空、OOXML 完整
  → 与转换后 DOCX 建立一对一哈希谱系
  → 按 DOCX 规则评价 Docling 解析
  → 分层人工检查 DOC 与 DOCX 的视觉/内容一致性
```

重点缺陷：旧格式损坏、公式/文本框丢失、分页变化、表格合并变化、页眉页脚变化。Lib­reOffice 成功退出只证明生成了文件，不证明语义完全保留。

### 5.2 `.docx`

```text
原始 DOCX
  → 直接读取 OOXML 建立段落/标题/列表/表格参考清单
  → Docling Word backend / SimplePipeline
  → JSON 结构检查
  → OOXML—Docling 文本、数字、表格和层级对照
```

DOCX 自带结构，因此重点是“原生结构是否被映射完整”，无需像 PDF 一样用视觉模型重建页面版面。

### 5.3 `.pdf`

```text
原始 PDF
  → 独立读取页数和程序化文本作为哨兵
  → Docling StandardPdfPipeline
  → 状态/置信等级/页面和 provenance 检查
  → 文字、数字、表格结构代理指标
  → 渲染页面 + Docling bbox 叠框人工检查
```

重点缺陷：双栏阅读顺序、页眉页脚污染、表格漏检或错列、跨页结构、脚注/单位分离和证据页错位。

## 6. 人工抽检方案

### 6.1 首次全量运行

本项目 111 份语料建议首轮人工检查 24 份（约 21.6%）：

- `.doc` 来源 8 份；
- 原生 `.docx` 8 份；
- `.pdf` 8 份。

这不是简单随机抽 24 份，而是按以下维度分层后，在层内用固定随机种子抽取：

- 格式：DOC / DOCX / PDF；
- 文件大小或页数：小 / 中 / 大；
- 表格密度：无表 / 普通表 / 表格密集；
- 版式风险：多栏、文本框、合并单元格、跨页表、公式、图片；
- 自动指标风险：置信等级低、覆盖率低、异常压缩比、数字召回异常。

对每份文档检查首、中、末各一页；表格密集或告警文档额外检查风险最高页。这样约检查 72 个基础页面，再加异常页。PDF 最大文件和每种罕见版式必须强制入样，不能交给随机数决定。

### 6.2 以后每次运行

每轮固定执行：

1. 100% 自动检查；
2. 固定金标集全部回归；
3. 12 份分层随机文档，每种来源格式至少 4 份；
4. 自动告警文档和与上一基线有显著差异的文档全部检查；
5. 更换 Docling/模型/LibreOffice 版本或关键配置时，恢复首轮 24 份强度。

固定随机种子和样本清单要随报告保存。分层抽样用于保证格式与高风险特征都被覆盖，随机化用于避免只挑“看起来容易”的文档。[NIST Sampling Scheme](https://www.itl.nist.gov/div898/handbook/ppc/section3/ppc332.htm)

### 6.3 人工评分表

每个页面/文档按 0、1、2 分评价：

- `2`：正确，无实质问题；
- `1`：轻微格式问题，不影响检索、数字含义和引用；
- `0`：语义或证据错误。

评价维度：

1. 正文完整性；
2. 阅读顺序；
3. 标题层级和列表；
4. 表格结构；
5. 数字、日期、单位、百分比；
6. 页码/区域证据；
7. caption、footnote 和资源关联；
8. 中文显示和可读性。

缺陷分级：

- **P0**：整文档/整页缺失、源文件被修改、结果不可读、证据指向错误文档、敏感本地路径泄漏。
- **P1**：条款语义改变、阅读顺序影响理解、数字/单位错误、表格错行错列、引用页错误。
- **P2**：不影响语义的 Markdown 样式或空白问题。

建议发布条件：P0=0、P1=0；P2 页面占比≤5%。抽样发现任一 P0/P1 时，拒绝本轮发布，并扩大检查到同一格式/版式/配置分层的全部文档。

首轮金标的高风险页建议双人独立检查，分歧由第三人或共同复核裁决。DocLayNet 使用重复标注估计人工一致性，说明对复杂版面保留复核机制比假定单一标注绝对正确更可靠。[DocLayNet](https://arxiv.org/abs/2206.01062)

## 7. 每次运行的落地流程

### 7.1 运行前

- 冻结并保存输入 `inventory.jsonl`；
- 保存配置文件与 SHA-256；
- 记录 Git commit、Python、Docling、docling-core、Torch、CUDA、LibreOffice 版本；
- 生成本轮 `run_id`，禁止覆盖旧结果；
- 验证固定金标文件仍存在且源哈希匹配。

### 7.2 解析结束后自动执行

```text
1. qa inventory     输入/输出/状态/哈希
2. qa encoding      UTF-8、乱码、路径泄漏
3. qa schema        JSON Schema、JSON Pointer、树和资源引用
4. qa content       文本/数字/页覆盖率和异常压缩比
5. qa structure     标题、列表、表格内部结构
6. qa evidence      PDF 页码与 bbox provenance
7. qa regression    与上一个通过基线的结构化差异
8. qa gold          固定金标 NED/F1/TEDS/顺序指标
9. qa sample        生成分层人工抽样清单和可视化材料
10. qa report        汇总报告并给出 PASS/WARN/FAIL
```

### 7.3 人工复核后

- 录入 P0/P1/P2、维度得分、说明和截图证据；
- 对自动告警标记为 true positive 或 false positive；
- 汇总各格式、文件大小和版式分层的缺陷率；
- 仅当硬门禁通过、金标无回归、人工无 P0/P1 时，将本轮标为可进入分块阶段；
- 通过结果成为新基线，失败结果永远不更新基线。

## 8. 建议实现的产物

每个解析运行目录增加：

```text
quality/
├─ quality_config.yaml          # 阈值、抽样规则、规范化规则
├─ run_metrics.json             # 全局机器可读指标
├─ document_metrics.jsonl       # 每文档指标和告警
├─ findings.csv                 # 缺陷明细
├─ sample_manifest.json         # 本轮人工样本，含随机种子和分层原因
├─ human_review.csv             # 人工评分模板/结果
├─ gold_metrics.json            # 固定金标回归结果
├─ diff_from_baseline.json      # 与上一通过版本的差异
├─ visualizations/              # 原页、bbox 叠框、表格 HTML
└─ quality_report.md            # 给人阅读的最终报告
```

`quality_report.md` 至少包含：

- 本轮输入、成功/失败/部分成功数量；
- DOC 转 DOCX、原生 DOCX、PDF 各自解析方法；
- 各格式的耗时、输出数量和异常数量；
- 所有硬门禁结果；
- 文本、数字、表格、阅读顺序、证据覆盖率；
- 金标回归和人工抽检结论；
- P0/P1/P2 明细；
- 与上一个通过版本相比的变化；
- 最终结论：`PASS`、`PASS_WITH_WARNINGS` 或 `FAIL`。

## 9. 首版门禁汇总

| 维度 | 硬门禁/目标 |
|---|---|
| 输入与输出 | 111 份全部有唯一结果，覆盖率 100% |
| Docling 状态 | `success=100%`，`partial_success=0`，`failure=0` |
| 源完整性 | 运行前后 SHA-256 完全一致 |
| 编码 | UTF-8 可读，U+FFFD/乱码/绝对路径泄漏均为 0 |
| JSON/资源 | Schema 通过、JSON Pointer/资源断链为 0，正文可达率 100% |
| PDF 页面 | 页面覆盖率 100% |
| PDF 证据 | 正文 ≥99%，表格与关键数字 100% |
| 文本代理 | 单文档 BCR ≥0.95，中位数 ≥0.98；低于阈值告警 |
| 数字代理 | ≥0.995；金标页 100% |
| 金标全文 | NED ≤0.02 |
| 金标标题/列表 | F1 ≥0.95 |
| 金标阅读顺序 | Kendall's τ ≥0.95，序列 NED ≤0.05 |
| 金标表格 | 普通 TEDS ≥0.95，复杂 ≥0.90，关键数字 exact match 100% |
| 人工检查 | P0=0、P1=0、P2 页面占比≤5% |

## 10. 实施优先级

### 第一阶段：本次全量解析后必须完成

1. L0 全部门禁；
2. UTF-8、乱码、绝对路径泄漏；
3. JSON Schema、JSON Pointer、父子关系、资源引用；
4. PDF 页面/provenance 覆盖；
5. DOCX OOXML 的文本、数字、表格数量与形状代理；
6. PDF 独立文本和数字覆盖代理；
7. 24 份文档的首轮人工复核；
8. 形成版本化 Markdown 测试报告。

### 第二阶段：建立稳定回归能力

1. 人工金标 24～30 个高风险页面；
2. NED、标题/列表 F1、阅读顺序 Kendall's τ；
3. TEDS 和关键数字 exact match；
4. 运行间结构化 diff 与可视化叠框；
5. 固定金标集自动回归。

### 第三阶段：进入分块和 RAG 后

1. 50～100 个带证据位置的问题；
2. 证据进入 chunk 的覆盖率 100%；
3. 检索 Recall@5/Recall@10、nDCG@10；
4. 数字答案 exact match；
5. 引用文档、页码、表格/条款位置准确率；
6. 将解析缺陷、分块缺陷、检索缺陷和生成缺陷分开归因。

## 11. 关键风险

- **将 Markdown 当成无损格式**：复杂表格的跨行跨列会被展平，应保留 JSON/HTML。
- **只依赖 Docling confidence**：官方尚未实现 table_score，且分数计算可能变化；只应用等级做路由。
- **只比较字符数**：无法识别阅读顺序和数字错行。
- **只随机抽样**：可能漏掉大文件、多栏和复杂表格；必须“强制高风险 + 分层随机”。
- **只检查本次、不比较基线**：模型或参数升级造成的静默回归难以发现。
- **用 LLM 单独担任裁判**：可作为辅助，但表格数字、引用和发布门禁应由确定性指标与人工金标决定；如以后引入模型裁判，应以人工标注校准，类似 ARES 的小规模人工标注思路。

## 参考资料

1. [Docling DocumentConverter API](https://docling-project.github.io/docling/reference/document_converter/)
2. [Docling Document model](https://docling-project.github.io/docling/concepts/docling_document/)
3. [Docling Serialization](https://docling-project.github.io/docling/concepts/serialization/)
4. [Docling Confidence Scores](https://docling-project.github.io/docling/concepts/confidence_scores/)
5. [Docling Technical Report](https://arxiv.org/abs/2408.09869)
6. [DocLayNet](https://arxiv.org/abs/2206.01062)
7. [OmniDocBench](https://github.com/opendatalab/OmniDocBench)
8. [PubTabNet/TEDS](https://www.ecva.net/papers/eccv_2020/papers_ECCV/papers/123660562.pdf)
9. [GriTS](https://arxiv.org/abs/2203.12555)
10. [LayoutReader/ReadingBank](https://arxiv.org/abs/2108.11591)
11. [Kendall's τ for Information Ordering](https://aclanthology.org/J06-4002/)
12. [NIST Sampling Scheme](https://www.itl.nist.gov/div898/handbook/ppc/section3/ppc332.htm)
13. [NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final)
14. [RFC 8259: JSON](https://www.rfc-editor.org/rfc/rfc8259)
15. [RFC 6901: JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901)
16. [BEIR](https://arxiv.org/abs/2104.08663)
17. [ARES](https://aclanthology.org/2024.naacl-long.20/)
18. [RAGChecker](https://arxiv.org/abs/2408.08067)
