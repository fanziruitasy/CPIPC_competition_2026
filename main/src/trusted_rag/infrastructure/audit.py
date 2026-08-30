"""实现按 trace_id 追加的 UTF-8 JSONL 审计仓储。"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

from trusted_rag.domain.ports import AuditRecord


class JsonlAuditRepository:
    """把查询、检索和模型事件追加到同一追踪文件。"""

    def __init__(self, root: Path) -> None:
        """初始化审计目录。

        :param root: 只允许写入审计 JSONL 的运行根目录。
        :return: 无。
        """
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, record: AuditRecord) -> None:
        """追加一条中文不转义且不含凭证的审计事件。

        :param record: 已完成契约校验的审计记录。
        :return: 无。
        """
        path = self.root / f"{record.trace_id}.jsonl"
        with self._lock, path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")

