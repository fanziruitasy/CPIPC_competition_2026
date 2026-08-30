"""在固定全量知识库快照运行官方 300 题三剖面评测。

功能：复用正式问答服务，运行 Dense、Jieba BM25 和 RRF 双路融合消融，逐题保存
答案、来源召回、引用、Token、延迟和正确性，并生成汇总报告。
输入：版本化评测 YAML；可选 ``--profile``、``--question-id``；``--resume`` 续跑。
输出：``data_runtime/evaluation_runs/<run_id>`` 下逐题 JSONL、summary.json 和 report.md。
返回：已选择题目均落盘后返回 0；配置或快照不一致时抛出异常。
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

MAIN_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = MAIN_ROOT.parent
SRC_ROOT = MAIN_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trusted_rag.application.runtime import build_query_service
from trusted_rag.domain.enums import RetrievalProfile
from trusted_rag.evaluation.dataset import EvaluationRecord
from trusted_rag.evaluation.option_resolver import DashScopeOptionResolver
from trusted_rag.evaluation.runner import (
    build_source_mapping,
    evaluate_profile,
    summarize_outcomes,
    write_evaluation_report,
)
from trusted_rag.infrastructure.artifacts import sha256_file, write_json_atomic


def main() -> int:
    """加载配置并运行选择的评测剖面和题号。

    :return: 所有选中题目均写入逐题结果后返回 0。
    """
    parser = argparse.ArgumentParser(description="运行官方 QA 统一评测")
    parser.add_argument(
        "--config",
        type=Path,
        default=MAIN_ROOT / "configs/evaluation/run_v0.01.yaml",
    )
    parser.add_argument("--profile", choices=[item.value for item in RetrievalProfile])
    parser.add_argument("--question-id", action="append")
    parser.add_argument("--resume", action="store_true")
    arguments = parser.parse_args()
    config_path = (
        arguments.config
        if arguments.config.is_absolute()
        else (MAIN_ROOT / arguments.config).resolve()
    )
    config: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    load_dotenv(WORKSPACE_ROOT / ".env", override=False)
    run_root = MAIN_ROOT / str(config["runtime_root"]) / str(config["run_id"])
    if run_root.exists() and not arguments.resume:
        raise FileExistsError(f"评测运行已存在，请使用 --resume：{run_root}")
    run_root.mkdir(parents=True, exist_ok=arguments.resume)

    query_config_path = MAIN_ROOT / str(config["query_config"])
    query_config = yaml.safe_load(query_config_path.read_text(encoding="utf-8"))
    snapshot_root = MAIN_ROOT / str(query_config["snapshot"]["run_root"])
    snapshot_manifest = json.loads((snapshot_root / "snapshot.json").read_text(encoding="utf-8"))
    if snapshot_manifest["snapshot_id"] != config["snapshot_id"]:
        raise ValueError("评测配置与在线查询快照不一致。")
    dataset_root = MAIN_ROOT / str(config["dataset_root"]) / str(config["dataset_run_id"])
    records = [
        EvaluationRecord.model_validate_json(line)
        for line in (dataset_root / "records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    configured_ids = config.get("question_ids")
    selected_question_ids = arguments.question_id or configured_ids
    if selected_question_ids:
        selected_ids = set(selected_question_ids)
        records = [record for record in records if record.question_id in selected_ids]
        if len(records) != len(selected_ids):
            raise ValueError("评测题号包含不存在或重复的题号。")
    corpus_root = MAIN_ROOT / str(query_config["snapshot"]["corpus_root"])
    source_mapping = build_source_mapping(records, corpus_root / "source_aliases.jsonl")
    profiles = [
        RetrievalProfile(arguments.profile)
        if arguments.profile
        else RetrievalProfile(item)
        for item in ([arguments.profile] if arguments.profile else config["profiles"])
    ]
    resolver_config = config["option_resolver"]
    resolver = DashScopeOptionResolver(
        model=os.getenv(str(resolver_config["model_env"]), "qwen3.7-plus"),
        timeout_seconds=int(resolver_config["timeout_seconds"]),
        max_retries=int(resolver_config["max_retries"]),
    )
    audit_root = MAIN_ROOT / str(query_config["audit"]["directory"])
    for profile in profiles:
        service = build_query_service(
            MAIN_ROOT,
            query_config_path,
            trace_id=f"{config['run_id']}-{profile.value}",
            profile=profile.value,
        )
        evaluate_profile(
            run_id=str(config["run_id"]),
            profile=profile,
            records=records,
            service=service,
            resolver=resolver,
            source_mapping=source_mapping,
            audit_root=audit_root,
            output_path=run_root / f"{profile.value}.jsonl",
        )
        summary = summarize_outcomes(
            run_id=str(config["run_id"]),
            snapshot_id=str(config["snapshot_id"]),
            expected_per_profile=int(config["expected_question_count"]),
            selected_profile=str(config.get("selected_profile", "dense_bm25")),
            profile_paths={
                item: run_root / f"{item}.jsonl"
                for item in config["profiles"]
            },
        )
        write_evaluation_report(run_root, summary)
    run_manifest = {
        "schema_version": "evaluation_run.v1",
        "run_id": config["run_id"],
        "snapshot_id": config["snapshot_id"],
        "dataset_run_id": config["dataset_run_id"],
        "dataset_sha256": sha256_file(dataset_root / "records.jsonl"),
        "query_config_sha256": sha256_file(query_config_path),
        "profiles": list(config["profiles"]),
        "source_mapping_coverage": sum(bool(value) for value in source_mapping.values()),
        "selected_question_count": len(records),
    }
    write_json_atomic(run_root / "run.json", run_manifest)
    print(json.dumps(run_manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
