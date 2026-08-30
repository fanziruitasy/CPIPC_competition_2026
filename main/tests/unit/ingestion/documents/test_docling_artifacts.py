"""验证 Docling JSON 质量统计、结构恢复和代表样例覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trusted_rag.domain.enums import SourceProfile
from trusted_rag.ingestion.documents.docling_artifacts import inspect_docling_json

WORKSPACE_ROOT = Path(__file__).resolve().parents[5]
DOCLING_RUNS = WORKSPACE_ROOT / "Data" / "staging" / "document_preprocessing" / "runs" / "docling"


@pytest.mark.source_characterization
@pytest.mark.parametrize(
    ("profile", "relative_path"),
    [
        (SourceProfile.NATIVE_DOCX, "native-docx/v0.04/001/parsed_documents/405/405.json"),
        (SourceProfile.NATIVE_DOCX, "native-docx/v0.04/001/parsed_documents/420/420.json"),
        (SourceProfile.CONVERTED_DOCX, "converted-docx/v0.03/001/parsed_documents/393/393.json"),
        (SourceProfile.CONVERTED_DOCX, "converted-docx/v0.03/001/parsed_documents/419/419.json"),
    ],
)
def test_representative_docx_json_passes_structural_validation(
    profile: SourceProfile,
    relative_path: str,
) -> None:
    """原生与转换型 DOCX 各两份现有产物必须能恢复为 DoclingDocument。"""
    path = DOCLING_RUNS / relative_path
    if not path.is_file():
        pytest.skip("本地未提供现有 Docling 解析产物。")

    report = inspect_docling_json(path, source_profile=profile)

    assert report.text_count + report.table_count + report.picture_count > 0
    assert report.unresolved_body_references == []
    assert report.docling_schema_version == "1.10.0"
