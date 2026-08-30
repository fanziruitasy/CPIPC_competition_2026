"""将官方 QA XLSX 转换为版本化统一评测记录。

功能：只读校验官方 300 道选择题，输出 ``evaluation_record.v1`` JSONL 和运行清单。
输入：``--config`` 指定的工作区相对 XLSX 路径、运行标识和输出根目录。
输出：``main/data_runtime/evaluation_datasets/<run_id>``；原工作簿保持不变。
返回：成功返回 0；输入或契约不合法时抛出异常并返回非零。
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

MAIN_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = MAIN_ROOT.parent
SRC_ROOT = MAIN_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trusted_rag.evaluation.dataset import normalize_official_qa


def main() -> int:
    """加载配置并规范化官方评测集。

    :return: 规范化和校验成功返回 0。
    """
    parser = argparse.ArgumentParser(description="规范化官方 QA 评测集")
    parser.add_argument(
        "--config",
        type=Path,
        default=MAIN_ROOT / "configs/evaluation/dataset_v0.01.yaml",
    )
    arguments = parser.parse_args()
    config_path = (
        arguments.config
        if arguments.config.is_absolute()
        else (MAIN_ROOT / arguments.config).resolve()
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    output_root = MAIN_ROOT / str(config["runtime_root"]) / str(config["run_id"])
    manifest = normalize_official_qa(
        workbook_path=WORKSPACE_ROOT / str(config["source_workbook"]),
        output_root=output_root,
        run_id=str(config["run_id"]),
        expected_question_count=int(config["expected_question_count"]),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
