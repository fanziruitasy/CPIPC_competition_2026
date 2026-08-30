"""运行 Word/PDF 全量解析与分块质量回归。

输入：现有 ``Data/staging/document_preprocessing/runs/docling``。
输出：``main/data_runtime/quality/<run-id>`` 下的 JSON 与 Markdown 报告。
返回：全部 111 份通过返回 0，否则返回 2。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MAIN_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = MAIN_ROOT.parent
sys.path.insert(0, str(MAIN_ROOT / "src"))

from trusted_rag.ingestion.documents.regression import run_document_regression  # noqa: E402


def main() -> int:
    """解析命令行参数并运行全量回归。

    :return: 全部通过返回 0，否则返回 2。
    """
    parser = argparse.ArgumentParser(description="运行 Word/PDF Docling 全量质量回归")
    parser.add_argument("--run-id", default="word-pdf-regression-v0.01-001")
    args = parser.parse_args()
    report = run_document_regression(
        WORKSPACE_ROOT / "Data" / "staging" / "document_preprocessing" / "runs" / "docling",
        MAIN_ROOT / "data_runtime" / "quality" / args.run_id,
    )
    print(
        f"通过 {report['passed_documents']}/{report['expected_documents']}，"
        f"报告：data_runtime/quality/{args.run_id}/document_regression_report.md"
    )
    return 0 if report["failed_documents"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
