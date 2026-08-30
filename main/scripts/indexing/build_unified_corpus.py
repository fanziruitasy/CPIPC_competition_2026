"""构建 Word、PDF 与 Excel 的统一可索引语料快照。

输入：通过质量门禁的 Docling 三类运行和 Excel 全量运行。
输出：`data_runtime/corpus_runs/<run-id>` 下的 v1 JSONL 契约及运行清单。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAIN_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = MAIN_ROOT.parent
sys.path.insert(0, str(MAIN_ROOT / "src"))

from trusted_rag.indexing.corpus_builder import build_unified_corpus  # noqa: E402


def main() -> int:
    """解析命令行并构建统一语料快照。

    :return: 构建成功返回 0。
    """
    parser = argparse.ArgumentParser(description="构建统一可索引语料快照")
    parser.add_argument("--run-id", default="unified-corpus-v0.01-001")
    parser.add_argument("--excel-run-id", default="excel-regression-v0.01-002")
    parser.add_argument("--knowledge-base-id", default="nfra-regulations")
    arguments = parser.parse_args()
    summary = build_unified_corpus(
        docling_runs_root=WORKSPACE_ROOT
        / "Data"
        / "staging"
        / "document_preprocessing"
        / "runs"
        / "docling",
        excel_run_root=MAIN_ROOT / "data_runtime" / "ingestion_runs" / arguments.excel_run_id,
        output_root=MAIN_ROOT / "data_runtime" / "corpus_runs" / arguments.run_id,
        knowledge_base_id=arguments.knowledge_base_id,
        run_id=arguments.run_id,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
