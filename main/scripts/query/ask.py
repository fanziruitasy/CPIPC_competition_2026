"""从固定索引快照执行一次完整可信问答。

功能：加载规则/模型规划器、Dense+Jieba BM25、Qdrant、DuckDB、qwen3-rerank
和回答模型，输出前端可直接使用的 answer.v1，并保存 trace_id 审计。
输入：``--config`` 检索配置、``--question`` 用户问题及可选检索 Profile。
输出：标准输出中的 UTF-8 JSON；审计写入配置指定的 query_runs 目录。
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

MAIN_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = MAIN_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trusted_rag.application.query_service import TrustedRagQueryService
from trusted_rag.application.runtime import build_query_service
from trusted_rag.domain.enums import RetrievalProfile


def main() -> int:
    """解析命令行并执行一次问答。

    :return: 成功返回 0，异常由调用方和日志捕获。
    """
    parser = argparse.ArgumentParser(description="执行统一可信 RAG 问答")
    parser.add_argument("--config", type=Path, default=MAIN_ROOT / "configs/retrieval/v0.01.yaml")
    parser.add_argument("--question", required=True)
    parser.add_argument("--trace-id", default=f"trace-{uuid.uuid4().hex}")
    parser.add_argument("--profile", choices=[item.value for item in RetrievalProfile])
    args = parser.parse_args()
    service = build_service(args.config.resolve(), trace_id=args.trace_id, profile=args.profile)
    answer = service.ask(args.question, trace_id=args.trace_id)
    print(json.dumps(answer.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


def build_service(config_path: Path, *, trace_id: str, profile: str | None = None) -> TrustedRagQueryService:
    """从版本化配置和环境变量构造完整问答服务。

    :param config_path: 检索配置文件。
    :param trace_id: 当前查询追踪标识，用于模型审计文件。
    :param profile: 可选 Dense/BM25 检索剖面覆盖。
    :return: 已连接代表性快照的问答服务。
    """
    return build_query_service(
        MAIN_ROOT,
        config_path,
        trace_id=trace_id,
        profile=profile,
    )


if __name__ == "__main__":
    raise SystemExit(main())
