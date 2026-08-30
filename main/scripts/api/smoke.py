"""对真实代表性快照执行不产生模型费用的 API 冒烟检查。

功能：在进程内创建 FastAPI，检查 OpenAPI、存活、就绪和知识库状态接口。
输入：项目默认 ``configs/retrieval/v0.01.yaml`` 与当前 Qdrant/DuckDB 快照。
输出：标准输出 JSON；任一接口失败时返回非零退出码。
"""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

MAIN_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = MAIN_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trusted_rag.api.app import create_app


def main() -> int:
    """运行四项只读接口检查。

    :return: 全部成功返回 0，否则返回 1。
    """
    client = TestClient(create_app(main_root=MAIN_ROOT))
    responses = {
        "openapi": client.get("/openapi.json"),
        "live": client.get("/health/live"),
        "ready": client.get("/health/ready"),
        "knowledge_base": client.get("/api/v1/knowledge-bases/nfra-regulations"),
    }
    result = {
        name: {"status_code": response.status_code, "body": response.json()}
        for name, response in responses.items()
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(response.status_code == 200 for response in responses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
