"""预检 PDF 页面尺寸和 Rotate 元数据并选择 Docling 后端。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, cast

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pydantic import Field

from trusted_rag.domain.common import ContractModel


class RotatedPage(ContractModel):
    """包含非零 PDF Rotate 标记的页面。"""

    page_number: Annotated[int, Field(ge=1)]
    rotation_degrees: Literal[90, 180, 270]


class PdfPreflightReport(ContractModel):
    """PDF 页面预检和后端路由结果。"""

    page_count: Annotated[int, Field(ge=1)]
    landscape_page_numbers: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list)
    rotated_pages: list[RotatedPage] = Field(default_factory=list)
    selected_backend: Literal["pypdfium2", "docling_parse"]


def inspect_pdf_pages(path: Path) -> PdfPreflightReport:
    """读取页面尺寸和 Rotate 标记并执行稳定后端路由。

    :param path: 原始 PDF 文件。
    :return: 页面数量、横版页、旋转页和所选后端。
    :raises FileNotFoundError: PDF 不存在时抛出。
    :raises RuntimeError: PDFium 无法打开或文件没有页面时抛出。
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    document = pdfium.PdfDocument(path)
    try:
        if len(document) == 0:
            raise RuntimeError("PDF 不包含页面。")
        landscape_pages: list[int] = []
        rotated_pages: list[RotatedPage] = []
        for page_index in range(len(document)):
            page = document[page_index]
            try:
                width, height = page.get_size()
                if width > height:
                    landscape_pages.append(page_index + 1)
                rotation = int(page.get_rotation()) % 360
                if rotation:
                    if rotation not in {90, 180, 270}:
                        raise RuntimeError(f"PDF 页面包含不支持的 Rotate 值：{rotation}")
                    rotation_literal = cast(Literal[90, 180, 270], rotation)
                    rotated_pages.append(
                        RotatedPage(page_number=page_index + 1, rotation_degrees=rotation_literal)
                    )
            finally:
                page.close()
        backend: Literal["pypdfium2", "docling_parse"] = (
            "docling_parse" if rotated_pages else "pypdfium2"
        )
        return PdfPreflightReport(
            page_count=len(document),
            landscape_page_numbers=landscape_pages,
            rotated_pages=rotated_pages,
            selected_backend=backend,
        )
    finally:
        document.close()
