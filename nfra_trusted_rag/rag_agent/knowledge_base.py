"""结构化问答知识库：封装只读 DuckDB 数据仓库。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .database import DuckDBRepository
class KnowledgeBase:
    """为问答引擎提供数据仓库和知识库概况。"""

    def __init__(self, db_path: str | Path):
        self.repo = DuckDBRepository(db_path)

    def summarize(self) -> dict[str, Any]:
        return self.repo.summarize()

    def close(self) -> None:
        self.repo.close()
