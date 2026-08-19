from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .utils import clean_text, compact_text, ref_index, sha256_text, token_count

HEADING_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十百千万0-9]+章"),
    re.compile(r"^第[一二三四五六七八九十百千万0-9]+节"),
    re.compile(r"^第[一二三四五六七八九十百千万0-9]+条"),
    re.compile(r"^[一二三四五六七八九十]+、"),
    re.compile(r"^（[一二三四五六七八九十]+）"),
    re.compile(r"^[0-9]+[.、]"),
]
CLAUSE_RE = re.compile(r"第[一二三四五六七八九十百千万0-9]+条")


def build_ref_map(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in ("texts", "tables", "groups", "pictures", "key_value_items", "form_items"):
        for idx, item in enumerate(doc.get(key, []) or []):
            ref = item.get("self_ref") or f"#/{key}/{idx}"
            out[ref] = item
    out["#/body"] = doc.get("body", {})
    out["#/furniture"] = doc.get("furniture", {})
    return out


def label_to_type(label: str | None) -> str:
    return {
        "section_header": "heading",
        "list_item": "list_item",
        "caption": "caption",
        "footnote": "footnote",
        "page_header": "page_header",
        "page_footer": "page_footer",
        "table": "table",
        "picture": "figure",
        "formula": "formula",
    }.get(label or "", "text")


def infer_heading_level(text: str, label: str | None, raw_level: Any) -> int | None:
    if isinstance(raw_level, int):
        return max(1, min(raw_level, 6))
    if label == "section_header":
        if re.match(r"^第[一二三四五六七八九十百千万0-9]+章", text):
            return 1
        if re.match(r"^第[一二三四五六七八九十百千万0-9]+节", text):
            return 2
        if re.match(r"^第[一二三四五六七八九十百千万0-9]+条", text):
            return 3
        return 2
    if any(p.search(text) for p in HEADING_PATTERNS):
        if text.startswith("第") and "章" in text[:8]:
            return 1
        if text.startswith("第") and "节" in text[:8]:
            return 2
        if text.startswith("第") and "条" in text[:8]:
            return 3
        return 4
    return None


def update_section_path(section_stack: list[str], level: int | None, text: str) -> list[str]:
    if not level or not text:
        return section_stack
    idx = max(0, level - 1)
    if len(section_stack) <= idx:
        section_stack.extend([""] * (idx + 1 - len(section_stack)))
    section_stack[idx] = text
    del section_stack[idx + 1 :]
    return section_stack


def prov_location(obj: dict[str, Any]) -> tuple[int | None, int | None, list[dict[str, Any]]]:
    prov = obj.get("prov") or []
    pages = []
    bboxes: list[dict[str, Any]] = []
    for item in prov:
        page_no = item.get("page_no")
        if isinstance(page_no, int):
            pages.append(page_no)
        bbox = item.get("bbox")
        if bbox:
            row = {"page_no": page_no, "bbox": bbox}
            if "charspan" in item:
                row["charspan"] = item.get("charspan")
            bboxes.append(row)
    if pages:
        return min(pages), max(pages), bboxes
    return None, None, bboxes


def is_probable_furniture(text: str, label: str | None, furniture_counts: Counter[str]) -> bool:
    if label in {"page_header", "page_footer"}:
        return True
    if not text:
        return True
    if re.fullmatch(r"[-—_ ]*\d+[-—_ ]*", text):
        return True
    return furniture_counts[text] >= 3 and token_count(text) <= 40


def caption_texts(obj: dict[str, Any], ref_map: dict[str, dict[str, Any]], key: str) -> list[str]:
    out = []
    for ref_obj in obj.get(key) or []:
        ref = ref_obj.get("$ref") if isinstance(ref_obj, dict) else None
        if ref and ref in ref_map:
            text = clean_text(ref_map[ref].get("text"))
            if text:
                out.append(text)
    return out


def table_cells(table: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for cell in (table.get("data") or {}).get("table_cells", []) or []:
        text = clean_text(cell.get("text"))
        rows.append({
            "row_start": cell.get("start_row_offset_idx"),
            "row_end": cell.get("end_row_offset_idx"),
            "col_start": cell.get("start_col_offset_idx"),
            "col_end": cell.get("end_col_offset_idx"),
            "row_span": cell.get("row_span"),
            "col_span": cell.get("col_span"),
            "text": text,
            "is_column_header": bool(cell.get("column_header")),
            "is_row_header": bool(cell.get("row_header")),
            "is_row_section": bool(cell.get("row_section")),
            "bbox": cell.get("bbox"),
            "fillable": cell.get("fillable"),
        })
    return rows


def annotation_texts(obj: dict[str, Any]) -> list[str]:
    out = []
    for ann in obj.get("annotations") or []:
        if isinstance(ann, str):
            text = clean_text(ann)
        elif isinstance(ann, dict):
            parts = []
            for key in ("text", "description", "label", "content"):
                value = ann.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            text = clean_text("\n".join(parts))
        else:
            text = ""
        if text:
            out.append(text)
    return out


def picture_description(obj: dict[str, Any]) -> str:
    meta = obj.get("meta") or {}
    desc = meta.get("description") if isinstance(meta, dict) else None
    if isinstance(desc, dict):
        return clean_text(desc.get("text"))
    if isinstance(desc, str):
        return clean_text(desc)
    return ""


def image_meta(obj: dict[str, Any]) -> dict[str, Any]:
    image = obj.get("image") or {}
    uri = image.get("uri")
    is_data_uri = isinstance(uri, str) and uri.startswith("data:")
    return {
        "mimetype": image.get("mimetype"),
        "dpi": image.get("dpi"),
        "size": image.get("size"),
        "uri": None if is_data_uri else uri,
        "uri_kind": "data_uri" if is_data_uri else ("external_uri" if uri else None),
        "data_uri_bytes_estimate": len(uri) if is_data_uri else None,
    }


def normalize_docling_document(
    doc: dict[str, Any],
    manifest_row: dict[str, Any] | None,
    data_root: Path,
    docling_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    doc_id = str(doc.get("name") or (manifest_row or {}).get("doc_id") or "unknown")
    ref_map = build_ref_map(doc)
    texts = doc.get("texts", []) or []
    furniture_counts = Counter(clean_text(t.get("text")) for t in texts if clean_text(t.get("text")))
    known_issue_flags = list((manifest_row or {}).get("known_issue_flags") or [])

    source_path = (manifest_row or {}).get("source_path")
    normalized_source_path = (manifest_row or {}).get("normalized_source_path")
    doc_record = {
        "schema_version": "normalized_document.v2",
        "doc_id": doc_id,
        "name": doc.get("name"),
        "origin": doc.get("origin") or {},
        "source_profile": (manifest_row or {}).get("source_profile"),
        "profile_run": (manifest_row or {}).get("profile_run"),
        "source_format": (manifest_row or {}).get("source_format"),
        "parser_input_format": (manifest_row or {}).get("parser_input_format"),
        "source_path": source_path,
        "normalized_source_path": normalized_source_path,
        "source_uri": f"source://{source_path}" if source_path else None,
        "normalized_source_uri": f"source://{normalized_source_path}" if normalized_source_path else None,
        "json_path": (manifest_row or {}).get("json_path"),
        "artifact_dir": (manifest_row or {}).get("artifact_dir"),
        "docling_version": doc.get("version"),
        "docling_status": (manifest_row or {}).get("docling_status") or (manifest_row or {}).get("status"),
        "source_sha256": (manifest_row or {}).get("source_sha256"),
        "normalized_source_sha256": (manifest_row or {}).get("normalized_source_sha256"),
        "known_issue_flags": known_issue_flags,
    }

    elements: list[dict[str, Any]] = []
    table_rows: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    section_stack: list[str] = []

    def base_lineage(version: str) -> dict[str, Any]:
        return {
            "source_sha256": doc_record.get("source_sha256"),
            "parser_version": f"docling-{doc.get('version')}",
            "normalizer_version": version,
        }

    def add_exclusion(row: dict[str, Any], reason: str) -> None:
        exclusions.append({
            "doc_id": doc_id,
            "source_profile": doc_record.get("source_profile"),
            "element_id": row["element_id"],
            "source_ref": row.get("source_ref"),
            "reason": reason,
            "text_preview": compact_text(row.get("text") or row.get("vl_description") or row.get("formula_text") or "")[:160],
        })

    def add_text_element(obj: dict[str, Any], ref: str, parent_group_ref: str | None = None, force_label: str | None = None) -> None:
        label = force_label or obj.get("label")
        text = clean_text(obj.get("text"))
        orig = clean_text(obj.get("orig"))
        element_type = label_to_type(label)
        level = infer_heading_level(text, label, obj.get("level"))
        nonlocal section_stack
        if element_type == "heading":
            section_stack = update_section_path(section_stack, level, text)
        elif element_type != "formula" and level and any(p.search(text) for p in HEADING_PATTERNS):
            section_stack = update_section_path(section_stack, level, text)
            element_type = "heading"
        page_start, page_end, bboxes = prov_location(obj)
        flags: list[str] = list(known_issue_flags)
        quality_status = "ready"
        if element_type == "formula":
            if not text:
                quality_status = "quarantine"
                flags.append("empty_formula")
            else:
                flags.append("formula_text")
        elif is_probable_furniture(text, label, furniture_counts):
            quality_status = "exclude"
            flags.append("page_furniture" if text else "empty_text")
        elif not text:
            quality_status = "exclude"
            flags.append("empty_text")
        clause = None
        m = CLAUSE_RE.search(text)
        if m:
            clause = m.group(0)
        element_id = f"ele_{doc_id}_{len(elements):06d}"
        row = {
            "schema_version": "normalized_element.v2",
            "element_id": element_id,
            "doc_id": doc_id,
            "source_profile": doc_record.get("source_profile"),
            "source_ref": ref,
            "element_type": element_type,
            "label": label,
            "order": len(elements),
            "parent_ref": (obj.get("parent") or {}).get("$ref"),
            "group_ref": parent_group_ref,
            "text": text,
            "orig_text": orig,
            "formula_text": text if element_type == "formula" else None,
            "level": level,
            "formatting": obj.get("formatting") or {},
            "section_path": [s for s in section_stack if s],
            "clause_no": clause,
            "page_start": page_start,
            "page_end": page_end,
            "bbox": bboxes,
            "quality_status": quality_status,
            "quality_flags": flags,
            "content_hash": sha256_text(text),
            "lineage": base_lineage("docling-p0-normalizer.v2"),
        }
        elements.append(row)
        if quality_status in {"exclude", "quarantine"}:
            add_exclusion(row, ";".join(flags) or quality_status)

    def add_table_element(obj: dict[str, Any], ref: str) -> None:
        cells = table_cells(obj)
        non_empty = sum(1 for c in cells if c.get("text"))
        page_start, page_end, bboxes = prov_location(obj)
        table_idx = ref_index(ref)[1] if ref_index(ref) else len([e for e in elements if e.get("element_type") == "table"])
        table_id = f"tbl_{doc_id}_{table_idx:04d}"
        flags = list(known_issue_flags)
        status = "ready" if non_empty else "exclude"
        if not non_empty:
            flags.append("empty_table")
        captions = caption_texts(obj, ref_map, "captions")
        footnotes = caption_texts(obj, ref_map, "footnotes")
        element_id = f"ele_{doc_id}_{len(elements):06d}"
        table_data = obj.get("data") or {}
        row = {
            "schema_version": "normalized_element.v2",
            "element_id": element_id,
            "doc_id": doc_id,
            "source_profile": doc_record.get("source_profile"),
            "source_ref": ref,
            "element_type": "table",
            "label": obj.get("label"),
            "order": len(elements),
            "parent_ref": (obj.get("parent") or {}).get("$ref"),
            "group_ref": None,
            "table_id": table_id,
            "caption": captions,
            "footnotes": footnotes,
            "num_rows": table_data.get("num_rows"),
            "num_cols": table_data.get("num_cols"),
            "non_empty_cell_count": non_empty,
            "section_path": [s for s in section_stack if s],
            "page_start": page_start,
            "page_end": page_end,
            "bbox": bboxes,
            "quality_status": status,
            "quality_flags": flags,
            "content_hash": sha256_text("\n".join(c.get("text", "") for c in cells)),
            "lineage": base_lineage("docling-p0-normalizer.v2"),
        }
        elements.append(row)
        for idx, cell in enumerate(cells):
            cell_row = dict(cell)
            cell_row.update({
                "schema_version": "table_cell.v2",
                "cell_id": f"cell_{table_id}_{idx:06d}",
                "doc_id": doc_id,
                "source_profile": doc_record.get("source_profile"),
                "table_id": table_id,
                "source_element_id": element_id,
                "section_path": row["section_path"],
                "page_start": page_start,
                "page_end": page_end,
                "quality_flags": flags,
            })
            table_rows.append(cell_row)
        if status == "exclude":
            add_exclusion(row, ";".join(flags) or "empty_table")

    def add_picture_element(obj: dict[str, Any], ref: str) -> None:
        page_start, page_end, bboxes = prov_location(obj)
        element_id = f"ele_{doc_id}_{len(elements):06d}"
        captions = caption_texts(obj, ref_map, "captions")
        annotations = annotation_texts(obj)
        desc = picture_description(obj)
        image = image_meta(obj)
        text_parts = captions + ([desc] if desc else []) + annotations
        text = clean_text("\n".join(text_parts))
        flags = list(known_issue_flags)
        if desc or annotations:
            quality_status = "ready"
            flags.append("vlm_description" if desc else "picture_annotation")
        elif captions:
            quality_status = "searchable_low_confidence"
            flags.append("caption_only_picture")
        else:
            quality_status = "quarantine"
            flags.append("needs_vlm")
        if image.get("uri_kind") == "data_uri":
            flags.append("embedded_data_uri_not_copied")
        row = {
            "schema_version": "normalized_element.v2",
            "element_id": element_id,
            "doc_id": doc_id,
            "source_profile": doc_record.get("source_profile"),
            "source_ref": ref,
            "element_type": "figure",
            "label": obj.get("label") or "picture",
            "order": len(elements),
            "parent_ref": (obj.get("parent") or {}).get("$ref"),
            "group_ref": None,
            "text": text,
            "caption": captions,
            "vl_description": desc,
            "annotations_text": annotations,
            "image": image,
            "asset_uri": image.get("uri"),
            "section_path": [s for s in section_stack if s],
            "page_start": page_start,
            "page_end": page_end,
            "bbox": bboxes,
            "quality_status": quality_status,
            "quality_flags": flags,
            "content_hash": sha256_text(text or ref),
            "lineage": base_lineage("docling-p0-picture-normalizer.v2"),
        }
        elements.append(row)
        if quality_status == "quarantine":
            add_exclusion(row, ";".join(flags) or "needs_vlm")

    def visit_ref(ref: str, parent_group_ref: str | None = None) -> None:
        if ref in seen_refs and not parent_group_ref:
            return
        obj = ref_map.get(ref)
        if not obj:
            return
        parsed = ref_index(ref)
        kind = parsed[0] if parsed else ""
        if kind == "groups":
            name = obj.get("name") or ""
            if name.startswith("rich_cell_group"):
                return
            for child in obj.get("children") or []:
                child_ref = child.get("$ref")
                if child_ref:
                    visit_ref(child_ref, ref)
            return
        seen_refs.add(ref)
        if kind == "texts":
            add_text_element(obj, ref, parent_group_ref)
        elif kind == "tables":
            add_table_element(obj, ref)
        elif kind == "pictures":
            add_picture_element(obj, ref)

    for child in (doc.get("body") or {}).get("children") or []:
        child_ref = child.get("$ref")
        if child_ref:
            visit_ref(child_ref)

    for key in ("texts", "tables", "pictures"):
        for idx, obj in enumerate(doc.get(key, []) or []):
            ref = obj.get("self_ref") or f"#/{key}/{idx}"
            if ref in seen_refs:
                continue
            parent_ref = (obj.get("parent") or {}).get("$ref")
            if parent_ref and parent_ref.startswith("#/tables/"):
                continue
            if key == "texts":
                add_text_element(obj, ref)
            elif key == "tables":
                add_table_element(obj, ref)
            elif key == "pictures":
                add_picture_element(obj, ref)

    return doc_record, elements, table_rows, exclusions
