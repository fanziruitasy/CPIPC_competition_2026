"""在当前代表性快照运行法规、数值和混合三类端到端样例。

功能：自动从当前 Qdrant 与 DuckDB 快照选取真实证据构造三类问题，调用统一问答
服务，并输出只含最终结果的 JSON 与 Markdown 验收报告。
输入：``--config`` 指向固定检索配置。
输出：``data_runtime/query_runs/stage7-validation-v0.01-001`` 运行目录。
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import yaml
from qdrant_client import QdrantClient

MAIN_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = MAIN_ROOT / "src"
SCRIPT_ROOT = Path(__file__).resolve().parent
for path in (SRC_ROOT, SCRIPT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ask import build_service

RUN_ID = "stage7-validation-v0.01-006"


def main() -> int:
    """执行三类端到端问题并生成验收产物。

    :return: 三类运行均完成时返回 0，否则异常退出。
    """
    parser = argparse.ArgumentParser(description="验证查询、检索与可信回答链路")
    parser.add_argument("--config", type=Path, default=MAIN_ROOT / "configs/retrieval/v0.01.yaml")
    args = parser.parse_args()
    config_path = args.config.resolve()
    config: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    snapshot_root = (MAIN_ROOT / config["snapshot"]["run_root"]).resolve()
    examples = _examples(config, snapshot_root)
    results = []
    for kind, question in examples:
        trace_id = f"{RUN_ID}-{kind}"
        service = build_service(config_path, trace_id=trace_id)
        answer = service.ask(question, trace_id=trace_id)
        results.append(
            {
                "kind": kind,
                "question": question,
                "status": answer.status.value,
                "answer": answer.answer_text,
                "refusal_reason": answer.refusal_reason,
                "citation_count": len(answer.citations),
                "retrieval": answer.retrieval.model_dump(mode="json"),
                "usage": answer.usage.model_dump(mode="json"),
                "trace_id": trace_id,
            }
        )
    output_root = (MAIN_ROOT / config["audit"]["directory"] / RUN_ID).resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    payload = {
        "schema_version": "stage7_validation.v1",
        "run_id": RUN_ID,
        "snapshot_id": json.loads((snapshot_root / "snapshot.json").read_text(encoding="utf-8"))["snapshot_id"],
        "results": results,
    }
    (output_root / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_root / "report.md").write_text(_report(payload), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _examples(config: dict[str, Any], snapshot_root: Path) -> list[tuple[str, str]]:
    client = QdrantClient(url=config["snapshot"]["qdrant_url"])
    points, _ = client.scroll(
        collection_name=config["snapshot"]["collection_alias"],
        limit=100,
        with_payload=True,
        with_vectors=False,
    )
    document_text = next(
        str((point.payload or {})["chunk"]["display_text"])
        for point in points
        if (point.payload or {}).get("source_profile") in {"native_docx", "converted_docx", "pdf"}
    )
    connection = duckdb.connect(str(snapshot_root / config["snapshot"]["duckdb_uri"]), read_only=True)
    try:
        row = connection.execute(
            """
            SELECT metric_name, entity_name, period_end, unit
            FROM table_facts
            WHERE metric_name IS NOT NULL AND entity_name IS NOT NULL
              AND period_end IS NOT NULL AND normalized_value IS NOT NULL
              AND quality_status = 'passed'
            ORDER BY metric_name, entity_name, period_end
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("代表性 DuckDB 中没有可用数值事实。")
    metric, entity, period, unit = (str(item or "") for item in row)
    parsed_period = date.fromisoformat(period)
    period_text = f"{parsed_period.year}年{parsed_period.month}月{parsed_period.day}日"
    numeric = f"{period_text}{entity}的{metric}是多少{unit}？"
    mixed = f"根据相关监管规定，{period_text}{entity}的{metric}是多少{unit}，并说明相关要求？"
    document = f"请根据知识库说明以下监管材料的主要内容：{document_text[:100]}"
    return [("document", document), ("structured", numeric), ("mixed", mixed)]


def _report(payload: dict[str, Any]) -> str:
    lines = [
        "# 查询规划、检索融合与可信回答验收报告",
        "",
        f"- 运行标识：`{payload['run_id']}`",
        f"- 知识库快照：`{payload['snapshot_id']}`",
        "- 检索方案：DashScope text-embedding-v3 Dense + 本地 Jieba BM25 + RRF + qwen3-rerank",
        "- 回答约束：证据充分性、冲突、数字、规范强度和引用 ID 确定性校验",
        "",
        "| 类型 | 状态 | 引用数 | Dense | BM25 | 最终候选 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in payload["results"]:
        retrieval = item["retrieval"]
        lines.append(
            f"| {item['kind']} | {item['status']} | {item['citation_count']} | "
            f"{retrieval['dense_candidate_count']} | {retrieval['bm25_candidate_count']} | "
            f"{retrieval['fused_candidate_count']} |"
        )
    lines.extend(["", "逐题答案与拒答原因保存在 `results.json`，完整候选和模型调用审计可按 `trace_id` 查询。", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
