# Changelog

## 0.01 - 2026-08-06

首个版本化的 Docling 文档预处理实验实现。

- 默认从 CLI 调用迁移为 Docling Python API。
- 将项目自定义设置与 Docling 原生设置分为 `project`、`docling` 两个配置区。
- 使用 Docling 2.118.0 的配置模型和 JSON Schema 校验 PDF pipeline 参数。
- 复用单个 `DocumentConverter`，避免两份 PDF 重复加载模型。
- 针对 8 核 16 线程 CPU、RTX 4060 Laptop 8GB 配置 CUDA、8 个 CPU 线程和批大小 4。
- 不设置文档超时，不做自动重试。
- 使用短暂存文件名规避 Windows 长路径问题。
- 输出 Markdown、HTML、图片资源和直接显示中文的 UTF-8 JSON。
- 首个手动实验仅处理未完成的 398、399 两份 PDF。
