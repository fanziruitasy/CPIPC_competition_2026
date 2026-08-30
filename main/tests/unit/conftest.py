"""提供各单元测试包共享的最小 Office 与 PDF 来源文件。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def supported_source_root(tmp_path: Path) -> Path:
    """创建具有正确最小签名的五种来源格式。

    :param tmp_path: Pytest 临时目录。
    :return: 包含 DOC、DOCX、PDF、XLS、XLSX 的目录。
    """
    root = tmp_path / "源文件"
    root.mkdir()
    ole_signature = bytes.fromhex("D0CF11E0A1B11AE1")
    (root / "监管制度.doc").write_bytes(ole_signature + b"legacy-word")
    (root / "统计报表.xls").write_bytes(ole_signature + b"legacy-excel")
    (root / "横版附件.pdf").write_bytes(b"%PDF-1.7\nminimal-test")
    _write_office_package(root / "原生制度.docx", "word/document.xml")
    _write_office_package(root / "统计报表.xlsx", "xl/workbook.xml")
    (root / "~$临时.docx").write_bytes(b"ignored")
    return root


def _write_office_package(path: Path, required_part: str) -> None:
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types />")
        package.writestr(required_part, "<root />")
