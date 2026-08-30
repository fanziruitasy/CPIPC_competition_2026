"""验证 DOCX 解析器按文档原子导出并生成质量清单。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trusted_rag.domain.common import LineageMetadata
from trusted_rag.domain.enums import SourceFormat, SourceKind
from trusted_rag.domain.knowledge import SourceDocument
from trusted_rag.ingestion.documents.docling_configuration import load_docx_parsing_config
from trusted_rag.ingestion.documents.docling_docx_parser import DoclingDocxParser

MAIN_ROOT = Path(__file__).resolve().parents[4]
WORKSPACE_ROOT = MAIN_ROOT.parent
CONFIG_PATH = MAIN_ROOT / "configs" / "ingestion" / "documents" / "native_docx" / "v0.01.yaml"
EXISTING_JSON = (
    WORKSPACE_ROOT
    / "Data"
    / "staging"
    / "document_preprocessing"
    / "runs"
    / "docling"
    / "native-docx"
    / "v0.04"
    / "001"
    / "parsed_documents"
    / "405"
    / "405.json"
)


class _FakeDocument:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.name = ""

    def save_as_markdown(self, *, filename: Path, **_: Any) -> None:
        filename.write_text("# 测试文档\n", encoding="utf-8")

    def save_as_json(self, *, filename: Path, **_: Any) -> None:
        payload = {**self.payload, "name": self.name}
        filename.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def save_as_html(self, *, filename: Path, **_: Any) -> None:
        filename.write_text("<html lang=\"zh-CN\"><body>测试文档</body></html>", encoding="utf-8")


class _FakeConverter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def convert(self, *, source: Path) -> SimpleNamespace:
        assert source.suffix == ".docx"
        return SimpleNamespace(status=SimpleNamespace(value="success"), document=_FakeDocument(self.payload))


def test_parser_exports_complete_atomic_artifact_set(tmp_path: Path) -> None:
    """正式解析发布目录必须包含三类正文产物、质量报告及哈希清单。"""
    if not EXISTING_JSON.is_file():
        return
    payload = json.loads(EXISTING_JSON.read_text(encoding="utf-8"))
    parser = object.__new__(DoclingDocxParser)
    parser.config = load_docx_parsing_config(CONFIG_PATH)
    parser.converter = _FakeConverter(payload)
    source_path = tmp_path / "input.docx"
    source_path.write_bytes(b"fake")
    source = SourceDocument(
        source_id="source_0123456789abcdef01234567",
        knowledge_base_id="kb-test",
        original_file_name="监管文件.docx",
        normalized_file_name="0123456789abcdef01234567.docx",
        source_format=SourceFormat.DOCX,
        source_kind=SourceKind.ORIGINAL,
        source_sha256="0" * 64,
        file_size_bytes=4,
        relative_path="监管文件.docx",
        lineage=LineageMetadata(
            run_id="run-test",
            producer="test",
            producer_version="0.01",
            created_at=datetime.now(UTC),
        ),
    )

    result = parser.parse(source, source_path=source_path, output_root=tmp_path / "parsed")

    suffixes = {Path(item.relative_uri).suffix for item in result.files}
    assert {".json", ".md", ".html"} <= suffixes
    assert any(item.relative_uri.endswith("quality_report.json") for item in result.files)
    assert result.quality.unresolved_body_references == []
    assert not any(path.name.startswith(".") for path in (tmp_path / "parsed").iterdir())
