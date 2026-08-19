from __future__ import annotations

from collections import defaultdict
import hashlib
from typing import Any

from .utils import clean_text, compact_text, sha256_text, token_count

TEXT_TYPES = {"heading", "text", "list_item", "caption", "footnote", "key_value"}
BOUNDARY_TYPES = {"table", "figure", "formula"}


def context_prefix(doc: dict[str, Any], section_path: list[str] | None, clause_no: str | None = None) -> str:
    parts = []
    title = doc.get("origin", {}).get("filename") or doc.get("name") or doc.get("doc_id")
    if title:
        parts.append(f"文档：{title}")
    if section_path:
        parts.append("章节：" + " > ".join(section_path))
    if clause_no:
        parts.append(f"条款：{clause_no}")
    return "\n".join(parts)


def merge_page(values: list[int | None], fn: str) -> int | None:
    pages = [v for v in values if isinstance(v, int)]
    if not pages:
        return None
    return min(pages) if fn == "min" else max(pages)


def make_text_chunk(
    doc: dict[str, Any],
    elements: list[dict[str, Any]],
    chunk_index: int,
    parent_id: str,
) -> dict[str, Any]:
    content = "\n".join(e.get("text") or "" for e in elements if e.get("text"))
    section_path = elements[-1].get("section_path") or []
    clause_no = next((e.get("clause_no") for e in elements if e.get("clause_no")), None)
    prefix = context_prefix(doc, section_path, clause_no)
    embedding_text = (prefix + "\n正文：" + content).strip() if prefix else content
    page_start = merge_page([e.get("page_start") for e in elements], "min")
    page_end = merge_page([e.get("page_end") for e in elements], "max")
    return {
        "schema_version": "rag_chunk.v1",
        "chunk_id": f"chk_{doc['doc_id']}_text_{chunk_index:06d}",
        "doc_id": doc["doc_id"],
        "source_profile": doc.get("source_profile"),
        "chunk_type": "text",
        "parent_chunk_id": parent_id,
        "order": chunk_index,
        "content_text": content,
        "content_markdown": None,
        "embedding_text": embedding_text,
        "bm25_text": embedding_text,
        "section_path": section_path,
        "clause_no": clause_no,
        "source_element_ids": [e["element_id"] for e in elements],
        "page_start": page_start,
        "page_end": page_end,
        "bbox": [b for e in elements for b in (e.get("bbox") or [])],
        "modality_ref": {"table_id": None, "row_start": None, "row_end": None, "figure_id": None, "formula_id": None},
        "quality_status": "ready",
        "quality_flags": [],
        "content_hash": sha256_text(content),
        "token_count": token_count(embedding_text),
        "lineage": {"chunker_version": "docling-p0-structure-token.v1"},
    }


def split_long_text_element(doc: dict[str, Any], element: dict[str, Any], next_index: int, parent_id: str, hard_limit: int) -> list[dict[str, Any]]:
    text = clean_text(element.get("text"))
    if token_count(text) <= hard_limit:
        return [make_text_chunk(doc, [element], next_index, parent_id)]
    separators = ["\n\n", "\n", "。", "；", ";", "."]
    pieces = [text]
    for sep in separators:
        new_pieces: list[str] = []
        changed = False
        for piece in pieces:
            if token_count(piece) <= hard_limit:
                new_pieces.append(piece)
                continue
            parts = [p.strip() for p in piece.split(sep) if p.strip()]
            if len(parts) > 1:
                changed = True
                suffix = sep if sep in {"。", "；", ";", "."} else ""
                new_pieces.extend(p + suffix for p in parts)
            else:
                new_pieces.append(piece)
        pieces = new_pieces
        if changed and all(token_count(p) <= hard_limit for p in pieces):
            break
    chunks = []
    for piece in pieces:
        clone = dict(element)
        clone["text"] = piece
        chunks.append(make_text_chunk(doc, [clone], next_index + len(chunks), parent_id))
    return chunks


def chunk_text_elements(
    doc: dict[str, Any],
    elements: list[dict[str, Any]],
    soft_limit: int = 450,
    hard_limit: int = 650,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chunks: list[dict[str, Any]] = []
    parent_chunks: list[dict[str, Any]] = []
    buffer: list[dict[str, Any]] = []
    parent_buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def parent_for(e: dict[str, Any]) -> str:
        section = e.get("section_path") or ["root"]
        raw = " > ".join(section) or "root"
        digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return f"parent_{doc['doc_id']}_{digest}"

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        p_id = parent_for(buffer[-1])
        chunk = make_text_chunk(doc, buffer, len(chunks), p_id)
        chunks.append(chunk)
        parent_buffers[p_id].extend(buffer)
        buffer = []

    for e in elements:
        if e.get("quality_status") != "ready":
            continue
        etype = e.get("element_type")
        if etype in BOUNDARY_TYPES:
            flush()
            continue
        if etype not in TEXT_TYPES:
            continue
        text = clean_text(e.get("text"))
        if not text:
            continue
        if token_count(text) > hard_limit:
            flush()
            p_id = parent_for(e)
            long_chunks = split_long_text_element(doc, e, len(chunks), p_id, hard_limit)
            chunks.extend(long_chunks)
            parent_buffers[p_id].append(e)
            continue
        candidate = buffer + [e]
        if buffer:
            same_section = (buffer[-1].get("section_path") or []) == (e.get("section_path") or [])
        else:
            same_section = True
        if buffer and (not same_section or token_count("\n".join(x.get("text", "") for x in candidate)) > soft_limit):
            flush()
        buffer.append(e)
    flush()

    for p_id, p_elements in parent_buffers.items():
        content = "\n".join(e.get("text") or "" for e in p_elements if e.get("text"))
        section_path = p_elements[-1].get("section_path") or []
        prefix = context_prefix(doc, section_path)
        parent_chunks.append({
            "schema_version": "rag_parent_chunk.v1",
            "parent_chunk_id": p_id,
            "doc_id": doc["doc_id"],
            "source_profile": doc.get("source_profile"),
            "chunk_type": "parent_text",
            "content_text": content,
            "embedding_text": (prefix + "\n正文：" + content).strip() if prefix else content,
            "section_path": section_path,
            "source_element_ids": [e["element_id"] for e in p_elements],
            "page_start": merge_page([e.get("page_start") for e in p_elements], "min"),
            "page_end": merge_page([e.get("page_end") for e in p_elements], "max"),
            "token_count": token_count(content),
            "content_hash": sha256_text(content),
        })

    for idx, chunk in enumerate(chunks):
        chunk["prev_chunk_id"] = chunks[idx - 1]["chunk_id"] if idx > 0 else None
        chunk["next_chunk_id"] = chunks[idx + 1]["chunk_id"] if idx + 1 < len(chunks) else None
    return chunks, parent_chunks


def rows_for_table(cells: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        row = cell.get("row_start")
        if isinstance(row, int):
            rows[row].append(cell)
    for row_cells in rows.values():
        row_cells.sort(key=lambda c: (c.get("col_start") if isinstance(c.get("col_start"), int) else 9999))
    return dict(sorted(rows.items()))


def row_text(row_cells: list[dict[str, Any]]) -> str:
    return " | ".join(clean_text(c.get("text")) for c in row_cells if clean_text(c.get("text")))


def markdown_from_rows(headers: list[str], body_rows: list[str]) -> str:
    all_rows = [headers] + [[c.strip() for c in r.split(" | ")] for r in body_rows]
    width = max((len(r) for r in all_rows), default=0)
    if width == 0:
        return ""
    lines = []
    header = (all_rows[0] + [""] * width)[:width]
    lines.append("| " + " | ".join(c.replace("|", "\\|") for c in header) + " |")
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for row in all_rows[1:]:
        norm = (row + [""] * width)[:width]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in norm) + " |")
    return "\n".join(lines)


def chunk_tables(
    doc: dict[str, Any],
    table_elements: list[dict[str, Any]],
    table_cells: list[dict[str, Any]],
    target_limit: int = 550,
    hard_limit: int = 700,
    start_order: int = 0,
) -> list[dict[str, Any]]:
    by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in table_cells:
        by_table[cell["table_id"]].append(cell)
    chunks: list[dict[str, Any]] = []
    order = start_order
    for table in table_elements:
        if table.get("quality_status") != "ready":
            continue
        table_id = table.get("table_id")
        if not table_id:
            continue
        cells = by_table.get(table_id, [])
        row_map = rows_for_table(cells)
        header_rows = []
        data_rows = []
        for ridx, row_cells in row_map.items():
            text = row_text(row_cells)
            if not text:
                continue
            if any(c.get("is_column_header") for c in row_cells) or ridx == 0:
                header_rows.append(text)
            else:
                data_rows.append((ridx, text))
        header_text = "\n".join(header_rows[:4])
        caption = "；".join(table.get("caption") or [])
        prefix = context_prefix(doc, table.get("section_path") or [])
        summary = "\n".join(x for x in [prefix, f"表：{caption or table_id}", f"表头：{header_text}"] if x)
        chunks.append({
            "schema_version": "rag_chunk.v1",
            "chunk_id": f"chk_{doc['doc_id']}_{table_id}_summary",
            "doc_id": doc["doc_id"],
            "source_profile": doc.get("source_profile"),
            "chunk_type": "table_summary",
            "parent_chunk_id": f"parent_{doc['doc_id']}_{table_id}",
            "order": order,
            "content_text": summary,
            "content_markdown": None,
            "embedding_text": summary,
            "bm25_text": summary,
            "section_path": table.get("section_path") or [],
            "clause_no": None,
            "source_element_ids": [table["element_id"]],
            "page_start": table.get("page_start"),
            "page_end": table.get("page_end"),
            "bbox": table.get("bbox") or [],
            "modality_ref": {"table_id": table_id, "row_start": None, "row_end": None, "figure_id": None, "formula_id": None},
            "quality_status": "ready",
            "quality_flags": [],
            "content_hash": sha256_text(summary),
            "token_count": token_count(summary),
            "lineage": {"chunker_version": "docling-p0-table-rowgroup.v1"},
        })
        order += 1
        current: list[tuple[int, str]] = []
        for ridx, text in data_rows:
            candidate_text = "\n".join([r for _, r in current] + [text])
            candidate = "\n".join(x for x in [summary, "行：" + candidate_text] if x)
            if current and token_count(candidate) > target_limit:
                order = emit_table_group(chunks, doc, table, table_id, summary, header_rows, current, order)
                current = []
            current.append((ridx, text))
            if token_count("\n".join(x for x in [summary, "行：" + text] if x)) > hard_limit:
                order = emit_table_group(chunks, doc, table, table_id, summary, header_rows, current, order)
                current = []
        if current:
            order = emit_table_group(chunks, doc, table, table_id, summary, header_rows, current, order)
    return chunks


def emit_table_group(
    chunks: list[dict[str, Any]],
    doc: dict[str, Any],
    table: dict[str, Any],
    table_id: str,
    summary: str,
    header_rows: list[str],
    rows: list[tuple[int, str]],
    order: int,
) -> int:
    row_start = rows[0][0]
    row_end = rows[-1][0]
    body = [text for _, text in rows]
    content = "\n".join(x for x in [summary, "行：" + "\n".join(body)] if x)
    markdown = markdown_from_rows(["表头/字段", "内容"], header_rows + body)
    chunks.append({
        "schema_version": "rag_chunk.v1",
        "chunk_id": f"chk_{doc['doc_id']}_{table_id}_rows_{row_start:04d}_{row_end:04d}",
        "doc_id": doc["doc_id"],
        "source_profile": doc.get("source_profile"),
        "chunk_type": "table_rows",
        "parent_chunk_id": f"parent_{doc['doc_id']}_{table_id}",
        "order": order,
        "content_text": content,
        "content_markdown": markdown,
        "embedding_text": content,
        "bm25_text": content,
        "section_path": table.get("section_path") or [],
        "clause_no": None,
        "source_element_ids": [table["element_id"]],
        "page_start": table.get("page_start"),
        "page_end": table.get("page_end"),
        "bbox": table.get("bbox") or [],
        "modality_ref": {"table_id": table_id, "row_start": row_start, "row_end": row_end, "figure_id": None, "formula_id": None},
        "quality_status": "ready",
        "quality_flags": [],
        "content_hash": sha256_text(content),
        "token_count": token_count(content),
        "lineage": {"chunker_version": "docling-p0-table-rowgroup.v1"},
    })
    return order + 1





def trim_modal_content(text: str, max_tokens: int = 620) -> str:
    text = clean_text(text)
    if token_count(text) <= max_tokens:
        return text
    marker = "\n[说明：图片/公式完整解析文本见 figures.jsonl 或 formulas.jsonl；此处为检索截断文本。]"
    while token_count(text + marker) > max_tokens and len(text) > 200:
        text = text[: int(len(text) * 0.85)].rstrip()
    return text + marker


def make_modal_chunk(doc: dict[str, Any], element: dict[str, Any], order: int) -> dict[str, Any]:
    chunk_type = "figure" if element.get("element_type") == "figure" else "formula"
    if chunk_type == "figure":
        figure_id = f"fig_{element['element_id']}"
        parts = [
            context_prefix(doc, element.get("section_path") or []),
            "图片说明：" + clean_text(element.get("vl_description")),
            "图片标注：" + "\n".join(element.get("annotations_text") or []),
            "图片标题：" + "；".join(element.get("caption") or []),
        ]
        modality_ref = {"table_id": None, "row_start": None, "row_end": None, "figure_id": figure_id, "formula_id": None}
    else:
        formula_id = f"fml_{element['element_id']}"
        formula_text = clean_text(element.get("formula_text") or element.get("text"))
        parts = [context_prefix(doc, element.get("section_path") or []), "公式：" + formula_text]
        modality_ref = {"table_id": None, "row_start": None, "row_end": None, "figure_id": None, "formula_id": formula_id}
    content = "\n".join(p for p in parts if clean_text(p.split("：", 1)[-1] if "：" in p else p))
    content = trim_modal_content(content)
    return {
        "schema_version": "rag_chunk.v1",
        "chunk_id": f"chk_{doc['doc_id']}_{chunk_type}_{order:06d}",
        "doc_id": doc["doc_id"],
        "source_profile": doc.get("source_profile"),
        "chunk_type": chunk_type,
        "parent_chunk_id": None,
        "order": order,
        "content_text": content,
        "content_markdown": None,
        "embedding_text": content,
        "bm25_text": content,
        "section_path": element.get("section_path") or [],
        "clause_no": element.get("clause_no"),
        "source_element_ids": [element["element_id"]],
        "page_start": element.get("page_start"),
        "page_end": element.get("page_end"),
        "bbox": element.get("bbox") or [],
        "modality_ref": modality_ref,
        "quality_status": element.get("quality_status"),
        "quality_flags": element.get("quality_flags") or [],
        "content_hash": sha256_text(content),
        "token_count": token_count(content),
        "lineage": {"chunker_version": f"docling-p0-{chunk_type}-chunk.v1"},
        "asset_uri": element.get("asset_uri") if chunk_type == "figure" else None,
    }


def chunk_modal_elements(doc: dict[str, Any], elements: list[dict[str, Any]], start_order: int = 0) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    order = start_order
    for element in elements:
        if element.get("element_type") not in {"figure", "formula"}:
            continue
        if element.get("quality_status") not in {"ready", "searchable_low_confidence"}:
            continue
        if not clean_text(element.get("text") or element.get("vl_description") or element.get("formula_text")):
            continue
        chunks.append(make_modal_chunk(doc, element, order))
        order += 1
    return chunks

