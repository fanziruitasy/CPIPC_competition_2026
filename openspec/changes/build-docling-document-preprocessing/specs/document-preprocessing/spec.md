## Purpose

为银行监管制度文档提供统一、可复现且可审计的预处理契约，使后续分块、检索和可信引用能够消费稳定的结构化内容，并从任何产物追溯到未经修改的原始文件。

## ADDED Requirements

### Requirement: Discover supported source documents without mutation
系统 SHALL 递归发现配置输入根目录中的 DOC、DOCX 和 PDF 文件，生成稳定的待处理清单，并将输入根目录视为只读。

#### Scenario: Build an input inventory
- **GIVEN** 输入根目录包含受支持文件、子目录和不受支持文件
- **WHEN** 用户执行清单扫描
- **THEN** 系统仅将 DOC、DOCX 和 PDF 写入待处理清单，并记录每个源文件的相对路径、大小、修改时间和 SHA-256
- **AND** 系统不在输入根目录中创建、修改、重命名或删除文件

### Requirement: Normalize legacy document formats
系统 SHALL 将不能直接进入统一解析流程的旧版 DOC 文件转换为暂存 DOCX，同时保留原文件身份和转换关系。

#### Scenario: Convert a legacy DOC successfully
- **GIVEN** 清单项为可读取的 DOC 文件
- **WHEN** 系统完成格式归一化
- **THEN** 转换后的 DOCX 只写入该次运行的暂存目录
- **AND** 清单记录原 DOC、暂存 DOCX、转换工具版本、状态和诊断信息之间的关系

#### Scenario: Legacy conversion fails
- **GIVEN** 某个 DOC 文件损坏或无法转换
- **WHEN** 格式归一化返回失败
- **THEN** 系统将该文档标记为 failed 并记录可定位的错误
- **AND** 系统继续处理其他清单项

### Requirement: Publish a stable document artifact set
每个成功解析的逻辑文档 SHALL 产生一套稳定产物，包括人工可审阅的 Markdown、下游可消费的规范化结构化 JSON、资源文件目录以及清单记录。下游系统 MUST NOT 依赖解析器私有对象才能读取结构化 JSON。

#### Scenario: Publish successful parse artifacts
- **GIVEN** 一个受支持的源文档被成功解析
- **WHEN** 系统发布该文档的处理结果
- **THEN** Markdown、结构化 JSON 和清单记录使用同一个稳定 doc_id 关联
- **AND** 清单列出各产物的相对路径、校验值、模式版本和生成状态

#### Scenario: Document contains no extracted resources
- **GIVEN** 一个成功解析的文档不包含需要独立保存的图片或附件
- **WHEN** 系统发布该文档的处理结果
- **THEN** 结构化 JSON 和清单明确表示资源集合为空
- **AND** 下游无需通过缺失目录推断资源状态

### Requirement: Preserve semantic document structure
规范化结构化 JSON SHALL 按阅读顺序表示标题、段落、条款、列表、表格、图片引用、脚注或注释，并保留节点层级与类型。Markdown SHALL 是同一规范化内容的审阅视图，而不是唯一事实来源。

#### Scenario: Parse a regulation with nested sections and tables
- **GIVEN** 源文档包含多级标题、条款列表和表格
- **WHEN** 系统完成解析
- **THEN** 结构化 JSON 保留节点顺序、父子层级、节点类型及表格单元格内容
- **AND** Markdown 以可读形式呈现对应标题、条款和表格

#### Scenario: Complex table cannot be represented losslessly in pipe Markdown
- **GIVEN** 源表格包含合并单元格或复杂表头
- **WHEN** Markdown 导出无法无损使用管道表格表示
- **THEN** 系统使用能够表达行列合并的 HTML 表格或等价表示
- **AND** 结构化 JSON 仍保留单元格坐标、跨度和内容

### Requirement: Retain source evidence locations
系统 SHALL 为可定位的内容节点记录来源证据，使答案引用能够回到原始文档。PDF 内容至少包含页码；DOCX 派生内容至少包含稳定的段落、表格或节点定位符。无法精确定位时 MUST 明确标记定位精度，不得伪造页码。

#### Scenario: Record PDF page evidence
- **GIVEN** 一个带文本层的 PDF 被解析
- **WHEN** 系统输出正文节点和表格节点
- **THEN** 每个可定位节点包含源 PDF 标识和一页或多页页码

#### Scenario: Word source has no trustworthy page number
- **GIVEN** 一个 DOCX 的分页依赖本地排版环境
- **WHEN** 系统输出其内容节点
- **THEN** 系统记录段落、表格或结构节点定位符
- **AND** 系统不将推测的 Word 页码标记为精确页码

### Requirement: Make every artifact auditable
每条文档清单记录 SHALL 包含 doc_id、源相对路径、源 SHA-256、媒体类型、源文件大小、逻辑文档关系、处理状态、运行标识、流水线版本、模式版本、转换器版本、解析器版本、产物路径、警告、错误和时间信息。

#### Scenario: Trace an artifact to its source
- **GIVEN** 用户取得任意一份结构化 JSON
- **WHEN** 用户使用其 doc_id 查询清单
- **THEN** 用户能够确定唯一源文件、源文件哈希、生成该产物的运行和工具版本

### Requirement: Isolate generated data by lifecycle and version
系统 SHALL 将不可变原始数据、可丢弃暂存数据、可发布标准化产物、运行清单和质量报告置于职责明确的独立命名空间。可发布产物 SHALL 按解析器或流水线及模式版本隔离。

#### Scenario: Run a new pipeline version
- **GIVEN** 已存在旧流水线版本的发布产物
- **WHEN** 用户以新流水线版本处理相同源数据
- **THEN** 新产物写入新的版本命名空间
- **AND** 旧产物和清单保持可读且不会被静默覆盖

### Requirement: Support idempotent restart and per-file failure isolation
系统 SHALL 以源哈希、流水线版本和配置摘要判断已有结果是否可复用；单个文件失败 MUST NOT 中止整个批次。发布单份文档产物 SHALL 使用原子完成语义，避免下游读取半成品。

#### Scenario: Rerun an unchanged successful document
- **GIVEN** 源哈希、流水线版本、模式版本和影响输出的配置均未改变
- **WHEN** 用户重跑批处理
- **THEN** 系统跳过或安全复用已验证产物，并在本次运行报告中记录 skipped 或 reused

#### Scenario: Parser fails on one document
- **GIVEN** 批次中一个文档解析失败而其他文档可解析
- **WHEN** 批处理完成
- **THEN** 失败文档记录为 failed 且不发布不完整产物
- **AND** 其他文档仍被处理并独立记录状态

### Requirement: Relate duplicate or alternate source representations
系统 SHALL 检测并记录疑似同一逻辑文档的多种源格式关系，但 MUST 保留各源文件的独立哈希和证据定位。系统不得在没有可审计依据时自动删除源表示。

#### Scenario: DOCX and PDF represent the same document
- **GIVEN** DOCX 与 PDF 的规范化标题、文号或内容指纹满足配置的同源判定规则
- **WHEN** 系统生成清单
- **THEN** 两个源记录关联到同一 logical_document_id 并记录判定依据与置信度
- **AND** 清单标记首选结构来源和首选页码证据来源，供下游去重

### Requirement: Gate full-corpus processing on representative QA
系统 SHALL 支持样本模式和全量模式，并在代表性样本未达到质量门槛时阻止默认全量发布。质量报告至少覆盖完成率、空文档、标题层级、表格保真、来源定位、警告和失败清单。

#### Scenario: Representative sample passes
- **GIVEN** 样本集覆盖 DOC、DOCX、PDF、长文档和复杂表格文档
- **WHEN** 自动检查通过且人工抽检被批准
- **THEN** 系统生成通过的样本质量报告
- **AND** 用户可以显式启动全量模式

#### Scenario: Representative sample fails
- **GIVEN** 样本中存在空输出、严重表格丢失或来源定位缺失
- **WHEN** 质量门禁完成评估
- **THEN** 报告列出失败文档、失败规则和复核建议
- **AND** 默认全量发布被阻止，除非用户显式记录覆盖决定
