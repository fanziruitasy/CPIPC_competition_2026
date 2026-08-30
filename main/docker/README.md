# Docker 资源目录

`Dockerfile` 提供两个固定构建目标：

- `rag-api`：运行 FastAPI 问答与前端接口。
- `knowledge-builder`：在相同 Python 环境上增加 LibreOffice 和中文字体，执行知识库构建脚本。

正式编排文件位于 `main/compose.yaml`。Qdrant 固定为 `1.19.0`，Qdrant 数据、DuckDB、上传、审计和版本化运行产物均持久化到 `main/data_runtime`。
