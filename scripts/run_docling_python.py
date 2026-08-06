"""Thin command entrypoint for the versioned Docling Python adapter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trusted_rag.preprocessing.documents import check_config, run_documents


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config',
        default='configs/preprocessing/docling_python_v0.01.yaml',
    )
    parser.add_argument(
        '--check-only',
        action='store_true',
        help='Validate configuration and inputs without running Docling.',
    )
    arguments = parser.parse_args()
    config_path = (REPO_ROOT / arguments.config).resolve()
    if arguments.check_only:
        print(
            json.dumps(
                check_config(config_path),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    return run_documents(config_path)


if __name__ == '__main__':
    raise SystemExit(main())
