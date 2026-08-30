"""验证 LibreOffice XLSX 与 openpyxl 的兼容读取。"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import openpyxl

from trusted_rag.ingestion.spreadsheets.cleaners.region_premium import xlsx_sheets


def test_xlsx_sheets_ignores_invalid_libreoffice_auto_filter(tmp_path: Path) -> None:
    """无效筛选范围不应阻止读取工作表中的真实单元格数据。"""
    clean_path = tmp_path / "clean.xlsx"
    malformed_path = tmp_path / "malformed.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A1"] = "指标"
    sheet["B1"] = 100
    sheet.auto_filter.ref = "A1:B1"
    workbook.save(clean_path)
    workbook.close()

    with ZipFile(clean_path) as source_zip, ZipFile(
        malformed_path,
        "w",
        ZIP_DEFLATED,
    ) as target_zip:
        for item in source_zip.infolist():
            payload = source_zip.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                payload = payload.replace(b'ref="A1:B1"', b'ref="8:3355"')
            target_zip.writestr(item, payload)

    sheets = xlsx_sheets(malformed_path)

    assert sheets[0]["values"][0][:2] == ["指标", 100]
