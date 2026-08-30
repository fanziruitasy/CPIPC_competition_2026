"""固定怡佳 Word/PDF 分块辅助实现中可复用的当前行为。"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LYJ_PROJECT = REPOSITORY_ROOT / "LYJ" / "docling_rag_chunk_index_retrieval_eval"


@pytest.mark.source_characterization
def test_lyj_text_chunks_keep_context_and_neighbors(monkeypatch: pytest.MonkeyPatch) -> None:
    """文本分块保留文档、章节上下文以及前后相邻关系。"""
    chunking = _load_chunking(monkeypatch)
    document = {
        "doc_id": "001",
        "source_profile": "native-docx",
        "origin": {"filename": "监管办法.docx"},
    }
    elements = [
        _element("e1", "第一章 总则", ["第一章 总则"]),
        _element("e2", "本办法适用于商业银行。", ["第一章 总则"]),
        _element("e3", "第二章 管理要求", ["第二章 管理要求"]),
    ]

    chunks, parents = chunking.chunk_text_elements(document, elements, soft_limit=20, hard_limit=40)

    assert len(chunks) == 2
    assert len(parents) == 2
    assert chunks[0]["embedding_text"].startswith("文档：监管办法.docx\n章节：第一章 总则")
    assert chunks[0]["next_chunk_id"] == chunks[1]["chunk_id"]
    assert chunks[1]["prev_chunk_id"] == chunks[0]["chunk_id"]
    assert chunks[0]["bm25_text"] == chunks[0]["embedding_text"]


@pytest.mark.source_characterization
def test_lyj_modal_chunks_filter_unsearchable_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """图片与公式只有具有可检索文本和允许质量状态时才生成分块。"""
    chunking = _load_chunking(monkeypatch)
    document = {"doc_id": "001", "name": "监管办法", "source_profile": "pdf"}
    elements = [
        {
            **_element("picture_1", "", ["附件"]),
            "element_type": "figure",
            "quality_status": "ready",
            "vl_description": "资本构成示意图",
            "annotations_text": ["核心一级资本"],
            "caption": ["图1"],
        },
        {
            **_element("formula_1", "RWA=K×12.5", ["附件"]),
            "element_type": "formula",
            "quality_status": "ready",
            "formula_text": "RWA=K×12.5",
        },
        {
            **_element("picture_2", "", ["附件"]),
            "element_type": "figure",
            "quality_status": "requires_review",
            "vl_description": "未经复核描述",
        },
    ]

    chunks = chunking.chunk_modal_elements(document, elements)

    assert [chunk["chunk_type"] for chunk in chunks] == ["figure", "formula"]
    assert "图片说明：资本构成示意图" in chunks[0]["content_text"]
    assert "公式：RWA=K×12.5" in chunks[1]["content_text"]
    assert all("未经复核描述" not in chunk["content_text"] for chunk in chunks)


def _load_chunking(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    if not (LYJ_PROJECT / "rag_doc_ingestion" / "docling_p0" / "chunking.py").is_file():
        pytest.skip("本地未提供怡佳来源代码。")
    monkeypatch.syspath_prepend(str(LYJ_PROJECT))
    return importlib.import_module("rag_doc_ingestion.docling_p0.chunking")


def _element(element_id: str, text: str, section_path: list[str]) -> dict[str, object]:
    return {
        "element_id": element_id,
        "element_type": "text",
        "text": text,
        "section_path": section_path,
        "quality_status": "ready",
        "page_start": 1,
        "page_end": 1,
        "bbox": [],
    }
