"""从统一语料产物恢复可引用证据和公开来源别名。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trusted_rag.domain.common import PublicSourceLocation
from trusted_rag.domain.enums import EvidenceType, SourceFormat
from trusted_rag.domain.knowledge import EvidenceUnit
from trusted_rag.domain.ports import SearchHit
from trusted_rag.domain.query import EvidenceCitation


class CorpusEvidenceRepository:
    """以内存只读映射提供分块证据和前端安全引用。"""

    def __init__(self, corpus_root: Path) -> None:
        """加载统一语料的 Evidence 与来源别名。

        :param corpus_root: 包含 evidence.jsonl 和 source_aliases.jsonl 的语料目录。
        :return: 无。
        """
        self.evidence = {
            item.evidence_id: item
            for item in (
                EvidenceUnit.model_validate(row)
                for row in _read_jsonl(corpus_root / "evidence.jsonl")
            )
        }
        aliases: dict[str, list[dict[str, Any]]] = {}
        for row in _read_jsonl(corpus_root / "source_aliases.jsonl"):
            aliases.setdefault(str(row["source_id"]), []).append(row)
        self.aliases = aliases

    def from_hits(self, hits: Sequence[SearchHit], *, limit: int = 12) -> list[EvidenceUnit]:
        """按候选顺序恢复去重后的原子证据。

        :param hits: 已完成最终排序的检索候选。
        :param limit: 最大证据条数。
        :return: 能在统一语料中找到的证据。
        """
        selected: list[EvidenceUnit] = []
        seen: set[str] = set()
        for hit in hits:
            for evidence_id in hit.chunk.evidence_ids:
                evidence = self.evidence.get(evidence_id)
                if evidence is not None and evidence_id not in seen:
                    selected.append(evidence)
                    seen.add(evidence_id)
                    if len(selected) >= limit:
                        return selected
        return selected

    def citation(self, evidence: EvidenceUnit) -> EvidenceCitation:
        """把内部证据转换为不含绝对路径的前端引用。

        :param evidence: 已用于回答的原子证据。
        :return: 文件名、格式、摘录和公开定位。
        """
        candidates = self.aliases.get(evidence.source_id, [])
        alias = next((item for item in candidates if item.get("is_canonical")), None)
        alias = alias or (candidates[0] if candidates else {})
        file_name = str(alias.get("original_file_name") or f"{evidence.source_id}.unknown")
        suffix = Path(file_name).suffix.casefold().lstrip(".")
        if suffix not in {"doc", "docx", "pdf", "xls", "xlsx"}:
            suffix = "xlsx" if evidence.evidence_type in {EvidenceType.CELL, EvidenceType.CALCULATION} else "pdf"
            file_name = "确定性事实查询结果.xlsx" if suffix == "xlsx" else "知识库证据.pdf"
        return EvidenceCitation(
            evidence_id=evidence.evidence_id,
            source_id=evidence.source_id,
            original_file_name=file_name,
            source_format=SourceFormat(suffix),
            excerpt=evidence.excerpt[:1200],
            location=PublicSourceLocation(
                page_number=evidence.location.page_number,
                section_path=evidence.location.section_path,
                clause=evidence.location.clause,
                element_ref=evidence.location.element_ref,
                sheet_name=evidence.location.sheet_name,
                cell_range=evidence.location.cell_range,
                table_id=evidence.location.table_id,
            ),
        )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
