"""公共来源登记命令。

功能：只读扫描 Word、PDF、Excel，生成输入清单、运行清单和排队任务状态。
输入：来源根目录、知识库标识、版本化 YAML 配置及可选每格式样例上限。
输出：``data_runtime/ingestion_runs`` 运行目录和 ``ingestion_jobs`` 状态 JSON。
返回：成功返回 0；配置、来源或登记失败时由异常产生非零退出码。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

MAIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = MAIN_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from trusted_rag.application.ingestion_registration import (  # noqa: E402
    IngestionRegistrationService,
)

DEFAULT_CONFIG = MAIN_ROOT / "configs" / "ingestion" / "v0.01.yaml"


def build_parser() -> argparse.ArgumentParser:
    """创建公共来源登记命令参数解析器。

    :return: 已配置的参数解析器。
    """
    parser = argparse.ArgumentParser(description="只读登记可信 RAG 知识来源。")
    parser.add_argument("--source-root", type=Path, required=True, help="只读来源根目录。")
    parser.add_argument("--knowledge-base-id", required=True, help="目标知识库标识。")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="版本化登记配置。")
    parser.add_argument(
        "--limit-per-format",
        type=int,
        default=None,
        help="每种格式最多登记数量；仅用于样例，正式运行不设置。",
    )
    parser.add_argument("--code-version", default="working-tree", help="Git 提交或交付版本。")
    return parser


def main() -> int:
    """执行公共来源登记。

    :return: 登记成功返回 0。
    """
    args = build_parser().parse_args()
    config = _load_config(args.config)
    runtime = _require_mapping(config, "runtime")
    registration = _require_mapping(config, "source_registration")
    runs_root = _resolve_from_main(str(runtime["runs_root"]))
    jobs_root = _resolve_from_main(str(runtime["jobs_root"]))
    configured_limit = registration.get("limit_per_format")
    limit = args.limit_per_format if args.limit_per_format is not None else configured_limit
    service = IngestionRegistrationService(runs_root=runs_root, jobs_root=jobs_root)
    outcome = service.register(
        source_root=args.source_root,
        knowledge_base_id=args.knowledge_base_id,
        pipeline=str(config["pipeline"]),
        config_version=str(config["config_version"]),
        effective_config=config,
        code_version=args.code_version,
        recursive=bool(registration.get("recursive", True)),
        limit_per_format=int(limit) if limit is not None else None,
        tool_versions={"python": "3.12"},
    )
    payload = {
        "job_id": outcome.job.job_id,
        "job_status": outcome.job.status.value,
        "source_count": len(outcome.job.source_ids),
        "reused_existing_job": outcome.reused_existing_job,
        "run_id": outcome.run_manifest.run_id if outcome.run_manifest else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("登记配置根节点必须是对象。")
    return payload


def _require_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"配置字段 {key} 必须是对象。")
    return value


def _resolve_from_main(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (MAIN_ROOT / path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
