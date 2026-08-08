"""Evaluate one immutable Docling run and generate QA artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trusted_rag.preprocessing.documents.document_quality import evaluate_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config',
        default='configs/preprocessing/document_quality_v0.01.yaml',
    )
    arguments = parser.parse_args()
    result = evaluate_run((REPO_ROOT / arguments.config).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result['automated_status'] == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
