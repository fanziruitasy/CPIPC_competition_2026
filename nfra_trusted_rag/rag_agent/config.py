"""共享运行配置，避免 CLI、HTTP 服务和 Agent 各自维护默认值。"""

from __future__ import annotations

import os
import re
from pathlib import Path


def load_env_file(path: Path = Path(".env")) -> None:
    """Load local defaults once; existing process variables always win."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file()
DEFAULT_DB_PATH = Path(os.environ.get("RAG_DB_PATH", "nfra.duckdb"))


def env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "off", "no"}
