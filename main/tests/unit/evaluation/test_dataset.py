"""验证官方 QA 规范化的顺序、字段与来源可追溯性。"""

import json
from pathlib import Path

from openpyxl import Workbook

from trusted_rag.evaluation.dataset import EXPECTED_COLUMNS, normalize_official_qa


def test_normalize_official_qa_preserves_ids_and_source_rows(tmp_path: Path) -> None:
    """规范化记录应连续、可追溯且不改变源工作簿。"""
    source = tmp_path / "qa.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "official"
    sheet.append(EXPECTED_COLUMNS)
    for index, source_type in enumerate(("excel", "word", "pdf"), start=1):
        sheet.append(
            (
                f"Q{index:03d}",
                source_type,
                "easy",
                "简单",
                "单事实检索",
                f"问题{index}",
                "选项A",
                "选项B",
                "选项C",
                "选项D",
                "A",
                "选项A",
                "证据",
                "来源标题",
                f"{index}.xlsx",
            )
        )
    workbook.save(source)
    digest_before = source.read_bytes()

    manifest = normalize_official_qa(
        workbook_path=source,
        output_root=tmp_path / "run",
        run_id="official-qa-test",
        expected_question_count=3,
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "run/records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert manifest["question_count"] == 3
    assert manifest["counts"]["source_type"] == {"excel": 1, "pdf": 1, "word": 1}
    assert [item["question_id"] for item in records] == ["Q001", "Q002", "Q003"]
    assert records[1]["source_row"] == 3
    assert records[1]["options"]["A"] == "选项A"
    assert source.read_bytes() == digest_before
