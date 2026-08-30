"""启动面向前端的可信 RAG FastAPI 服务。

功能：加载固定知识库快照并暴露健康检查、入库任务、知识库状态、JSON Chat 和
SSE Chat 接口。
输入：环境变量 ``RAG_API_HOST``、``RAG_API_PORT`` 和 ``RAG_API_WORKERS``。
输出：监听指定地址的 HTTP 服务；进程退出码由 Uvicorn 管理。
"""

# ruff: noqa: E402

from __future__ import annotations

import os
import sys
from pathlib import Path

MAIN_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = MAIN_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import uvicorn


def main() -> None:
    """按环境变量启动 Uvicorn。

    :return: 无；服务运行至收到终止信号。
    """
    uvicorn.run(
        "trusted_rag.api.app:app",
        host=os.getenv("RAG_API_HOST", "0.0.0.0"),
        port=int(os.getenv("RAG_API_PORT", "8000")),
        workers=int(os.getenv("RAG_API_WORKERS", "1")),
    )


if __name__ == "__main__":
    main()
