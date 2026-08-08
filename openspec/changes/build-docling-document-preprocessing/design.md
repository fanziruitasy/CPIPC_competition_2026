## Context

参见 proposal.md 的 Why。当前 docANDpdf 语料约 111 份文件，包含 PDF、DOCX 和旧版 DOC；PDF 主要是带文本层的 Word 导出版，部分文档篇幅很长且表格密集。结构化产物将成为后续分块模块的输入，因此必须把 Docling 的解析细节、批次临时文件和稳定数据契约隔离开。

开发环境为 Windows，已创建 CPIPC Conda 环境（Python 3.12.13）。LibreOffice 26.2.4.2 已安装在 D:/Program Files/LibreOffice/program；当前终端 PATH 尚未发现 soffice，因此首版配置允许显式指定 soffice.com 的绝对路径。Docling 当前未安装。原始比赛数据必须保持只读。

## Goals / Non-Goals

**Goals:**

- 用清晰的目录所有权防止原始数据、临时文件、发布产物和索引相互污染。
- 让解析器、旧格式转换器和导出格式均可替换，不影响批处理编排及下游契约。
- 将单次运行与可供下游消费的不可变发布版本分开。
- 用显式 JSON Schema 和 manifest 契约管理兼容性。
- 支持先处理代表性样本，再批准全量发布。

**Non-Goals:**

- 不在当前阶段创建未来模块的空代码骨架；只预留命名与边界，避免过早设计。
- 不让下游直接消费 DoclingDocument 或 Docling 原生 JSON。
- 不把 generated Markdown 当作人工维护的 docs，也不把大批生成物提交到 openspec。
- 不承诺 Word 的精确物理页码；可验证页码证据以 PDF 为准。

## Decisions

### 1. Repository and data directories have separate ownership

采用以下目标结构；实现时只创建本变更实际使用的目录，未来目录在对应 OpenSpec 变更中落地。

~~~text
CPIPC_competition_2026/
├─ openspec/                         # 需求、设计、任务，不存放语料产物
├─ resources/                        # 小型、稳定、需要版本控制的外部项目资料
│  └─ competition/
│     ├─ brief/                      # 官方赛题原件
│     ├─ rules/                      # 提交规则、保密凭证等
│     └─ references/                 # 主办方参考资料和练习附件
├─ docs/                             # 团队编写或整理的可读文档
│  ├─ product/                       # PRD、用户场景、验收口径
│  ├─ architecture/                  # 技术方案、数据字典、接口说明
│  └─ operations/                    # 安装、运行、排障和发布手册
├─ configs/
│  └─ preprocessing/
│     ├─ document_pipeline.yaml      # 路径、模式、阈值和发布配置
│     └─ sample_corpus.yaml          # 代表性样本清单
├─ contracts/
│  └─ document/
│     └─ v1/
│        ├─ normalized-document.schema.json
│        ├─ document-manifest.schema.json
│        └─ source-relation.schema.json
├─ src/
│  └─ trusted_rag/
│     ├─ common/                     # 配置、哈希、日志、原子写入等共享基础能力
│     └─ preprocessing/
│        └─ documents/
│           ├─ pipeline.py           # 编排，不包含解析器实现细节
│           ├─ inventory.py
│           ├─ normalization.py
│           ├─ export.py
│           ├─ manifest.py
│           ├─ quality.py
│           ├─ parsers/
│           │  └─ docling.py
│           └─ converters/
│              └─ libreoffice.py
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  └─ fixtures/documents/            # 小型、可提交、无敏感信息的测试夹具
├─ scripts/                          # 薄命令入口，不承载业务规则
├─ deliverables/                     # 最终提交包的组装清单与轻量说明
└─ Data/
   ├─ originalData/                  # 比赛原始数据，只读
   ├─ staging/
   │  └─ document_preprocessing/
   │     └─ runs/<run_id>/
   │        ├─ normalized_sources/   # DOC 转换得到的临时 DOCX
   │        ├─ work/                 # 解析器缓存和中间文件
   │        ├─ candidate/            # 尚未发布的完整候选产物
   │        └─ logs/
   ├─ processed/
   │  └─ document_corpus/
   │     └─ releases/<release_id>/
   │        ├─ markdown/<doc_id>.md
   │        ├─ structured/<doc_id>.json
   │        ├─ assets/<doc_id>/
   │        ├─ manifests/
   │        │  ├─ documents.jsonl
   │        │  ├─ source_relations.jsonl
   │        │  └─ release.json
   │        └─ reports/
   │           ├─ qa_summary.json
   │           └─ qa_summary.md
   └─ indexes/                       # 未来检索变更拥有；本变更不创建
~~~

staging 可整体删除和重建；releases 一旦发布视为不可变。下游通过显式 release_id 读取语料版本，而不是搜索最新文件或依赖机器本地绝对路径。

备选方案是把 Markdown、JSON、图片分别放在全局目录。该方案难以原子发布同一批语料，也容易让不同版本交叉，因此不采用。

resources 不等同于数据目录：它只接收官方赛题说明、规则、凭证和主办方提供的小型参考附件等稳定外部资料；不接收原始语料、转换结果、模型缓存、向量索引或运行日志。官方原件保留原文件名，团队从中整理出的 PRD 和技术说明进入 docs。deliverables 只负责组装 8 月 31 日提交物，不成为代码、数据或文档的唯一来源。

### 2. Use a stable normalized-document contract between parsing and chunking

Docling adapter 先把解析结果映射为项目自有的 normalized-document v1。核心字段分为：

- identity：doc_id、logical_document_id、source_id、schema_version。
- provenance：源相对路径、SHA-256、媒体类型、转换链、解析器和配置摘要。
- content：有序节点数组；节点具有 node_id、type、text、parent_id、children、attributes。
- evidence：源文件、PDF 页码或 Word 结构定位符、定位精度；可选边界框。
- table：行列、单元格、row_span、col_span、表头语义及来源位置。
- resources：资源标识、相对路径、媒体类型、校验值及对应内容节点。
- diagnostics：文档级和节点级警告。

Markdown 从 normalized-document 生成；它用于抽检和人工阅读，不作为 JSON 的反向解析来源。复杂表格优先输出 HTML 表示并在 JSON 中保存完整网格。

备选方案是直接保存 Docling 原生 JSON。它能更快启动，但会把下游绑定到具体库版本，并且不便统一处理未来的其他解析器，因此只允许将其作为可选调试产物，不能成为发布契约。

### 3. Separate ports from adapters

流水线依赖三个稳定行为边界：

- Source converter：输入源描述，输出暂存的规范格式及转换诊断。
- Document parser：输入可解析文件，输出中立的文档模型。
- Artifact publisher：验证模型并原子发布产物与 manifest。

Docling 和 LibreOffice 位于 adapters 目录，只实现边界；批处理、重试、哈希、发布和 QA 不调用其私有对象。未来增加 MinerU、其他 Office 转换方式或对特定格式使用专用解析器时，只需新增 adapter 和配置选择。

备选方案是在一个批处理脚本中直接调用全部工具。文件少时简单，但无法单测、断点恢复或替换组件，因此不采用。

### 4. Inventory first, then normalize, parse, validate, and publish

单次运行采用如下状态流：

~~~text
discover → fingerprint → relate sources → normalize DOC
         → parse → map to contract → export candidate
         → schema/quality validation → publish release
~~~

运行状态使用 success、warning、failed、skipped、reused。单文档写入 candidate 下的独立临时目录，所有文件和校验值完成后再原子移动到候选完成位置。只有样本门禁通过且用户执行 publish，才形成新的 release。

批次不因单文件失败中断。重跑根据 source_sha256 + pipeline_version + schema_version + config_digest 判断是否安全复用；任何一项变化均重新生成。

### 5. Use deterministic identities and explicit versions

- source_id：源相对路径规范化值与源 SHA-256 的稳定摘要。
- doc_id：默认由 canonical source_id 派生，源内容不变时保持稳定。
- logical_document_id：同源多格式记录的聚合标识；不替代各自 source_id。
- node_id：doc_id、结构路径、节点类型和稳定序号的组合摘要。
- run_id：每次执行唯一，用于日志和暂存路径，不代表可发布数据版本。
- pipeline_version：影响解析或映射行为的代码/配置版本。
- schema_version：normalized-document 契约版本。
- release_id：人工批准的不可变语料版本；release.json 固化上述版本、输入集合摘要和 QA 结论。

manifest 只保存相对仓库或发布根目录的路径，避免 E 盘绝对路径泄漏到可移植产物。兼容字段新增使用 schema 次版本；删除、改名或语义变化使用主版本并由后续变更提供迁移器。

### 6. Treat DOCX as structural source and PDF as page evidence when relation is verified

同源关系先使用文件名、规范化标题、文号和内容指纹生成候选，再输出置信度及判定依据。高置信度配对默认选择 DOCX 作为结构首选来源、PDF 作为精确页码证据来源；低置信度只报告，不自动合并。

下游去重依据 manifests/source_relations.jsonl，而不是删除任一解析结果。这样既减少重复召回，也保留可审计证据。

### 7. Make quality gates configuration-driven

第一批选择 8 至 10 份样本，覆盖三种格式、长文档、表格密集文档和同源格式对。已知优先样本包括银行函证工作操作指引、263 页附件类文档以及包含表格的旧版 DOC。

自动检查包括：

- 文件清单覆盖率、成功率、空内容和异常短内容。
- 标题层级连续性、条款/列表识别、表格数量和单元格非空率。
- PDF 页码覆盖率、Word 结构定位覆盖率、资源引用完整性。
- JSON Schema 校验、产物校验值、绝对路径泄漏和半成品检测。
- 同源关系冲突、重复 doc_id、warning 与 failed 汇总。

人工抽检在 qa_summary.md 中记录文档、检查人、结论和备注。阈值位于配置文件，不硬编码在解析器 adapter 中。

### 8. Keep generated corpus out of normal Git history

代码、配置、契约、小型测试夹具、OpenSpec 和人工文档进入 Git。Data/staging、Data/processed 和 Data/indexes 的大规模生成物通过 .gitignore 排除；release.json 和 QA 摘要是否作为比赛证据提交，由发布脚本复制到单独的轻量 artifacts 目录或制品存储决定。

备选方案是直接 Git add Data。普通 Git 会将每个大文件完整写入历史，删除后历史仍膨胀，也难以管理多轮处理版本，因此不采用。

## Risks / Trade-offs

- [Docling 对个别 Word 复杂版式或表格还原不稳定] → 保留结构化警告、样本门禁和解析器 adapter；关键表格人工抽检。
- [旧版 DOC 经 LibreOffice 转换发生版式变化] → 保存转换链、工具版本和临时 DOCX 校验值，并将原 DOC 作为最终证据源。
- [Docling 首次运行需要下载模型，离线环境可能失败] → 安装阶段预下载并缓存模型，运行手册提供缓存检查和离线验证。
- [超长文档占用大量内存和时间] → 进程级单文档隔离、可配置并发、超时、资源监控和断点重跑。
- [DOCX 与 PDF 同源判断误合并] → 低置信度不自动合并，manifest 保存依据，允许人工覆盖。
- [Markdown 无法完整表达合并表格] → JSON 表格网格作为事实来源，Markdown 使用 HTML 表格。
- [发布目录增长较快] → staging 可清理；release 不覆盖，并由明确保留策略管理旧版本。

## Migration Plan

1. 建立 Python 3.12 独立环境，安装 Docling；安装 LibreOffice 并验证 soffice 命令可用。
2. 落地目录、配置、JSON Schema 和 adapter 接口，不触碰 originalData。
3. 用最小测试夹具完成单元和集成测试。
4. 对代表性样本运行 sample，修正解析配置并完成自动及人工 QA。
5. 冻结 pipeline_version、schema_version 和配置摘要，运行全部 111 份文档。
6. QA 通过后创建首个 release，后续分块变更仅引用该 release_id。

回滚不删除已发布 release；将下游配置切回前一个 release_id 即可。失败运行只清理经过路径校验的对应 staging/runs/<run_id>，不递归操作 Data 根目录。

## Open Questions

- 比赛最终提交包是否允许携带全部结构化产物，还是只提交代码、轻量样例和可复现实验说明；该问题影响打包策略，不影响流水线契约。
- 是否需要为人工抽检提供简单 Web 页面；首版可以直接审阅 Markdown 与 QA 报告，后续按时间决定。
