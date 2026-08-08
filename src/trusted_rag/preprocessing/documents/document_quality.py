"""Repeatable integrity, fidelity, provenance, and review sampling for Docling runs."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import random
import re
import statistics
import unicodedata
import warnings as python_warnings
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote
from xml.etree import ElementTree

import yaml

from .docling_config import REPO_ROOT
from .document_inventory import sha256_file


ABSOLUTE_PATH_PATTERN = re.compile(r'(?i)(?:[a-z]:[\\/]|file:///)')
UNICODE_ESCAPE_PATTERN = re.compile(r'(?<!\\)\\u([0-9a-fA-F]{4})')
NUMBER_PATTERN = re.compile(
    r'(?<![\d.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\d.])'
)
LOG_CONTEXT_PATTERN = re.compile(
    r'\[\d+/\d+\]\[([^\]]+)\]\[([^\]]+)\]'
)
MOJIBAKE_MARKERS = ('锟斤拷', '鈥', '銆', '浣犲ソ', 'ï¿½')
W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open('r', encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, start=1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f'invalid JSONL at {path}:{line_number}: {exc}'
                    ) from exc
    return records


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n',
        encoding='utf-8',
        newline='\n',
    )


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
            stream.write('\n')


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if denominator == 0:
        return 1.0
    return round(float(numerator) / float(denominator), 6)


def _normalized_characters(text: str) -> list[str]:
    normalized = unicodedata.normalize('NFC', text)
    return [character.casefold() for character in normalized if character.isalnum()]


def _numbers(text: str) -> list[str]:
    normalized = unicodedata.normalize('NFKC', text)
    return [
        match.group(0).replace(',', '').replace(' ', '')
        for match in NUMBER_PATTERN.finditer(normalized)
    ]


def _multiset_recall(reference: Iterable[str], candidate: Iterable[str]) -> float:
    reference_counter = Counter(reference)
    candidate_counter = Counter(candidate)
    return _ratio(
        sum((reference_counter & candidate_counter).values()),
        sum(reference_counter.values()),
    )


def compare_text(reference: str, candidate: str) -> dict[str, Any]:
    reference_characters = _normalized_characters(reference)
    candidate_characters = _normalized_characters(candidate)
    reference_numbers = _numbers(reference)
    candidate_numbers = _numbers(candidate)
    return {
        'reference_character_count': len(reference_characters),
        'candidate_character_count': len(candidate_characters),
        'character_recall': _multiset_recall(
            reference_characters,
            candidate_characters,
        ),
        'reference_number_count': len(reference_numbers),
        'candidate_number_count': len(candidate_numbers),
        'number_recall': _multiset_recall(
            reference_numbers,
            candidate_numbers,
        ),
    }


def extract_ooxml_baseline(path: Path) -> dict[str, Any]:
    """Extract an independent DOCX text/table baseline using only OOXML."""
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read('word/document.xml')
    root = ElementTree.fromstring(document_xml)
    paragraphs = []
    for paragraph in root.iter(f'{{{W_NS}}}p'):
        paragraphs.append(
            ''.join(
                node.text or ''
                for node in paragraph.iter(f'{{{W_NS}}}t')
            )
        )
    text = '\n'.join(paragraphs)
    table_shapes = []
    for table in root.iter(f'{{{W_NS}}}tbl'):
        rows = table.findall(f'{{{W_NS}}}tr')
        row_widths = [
            len(row.findall(f'{{{W_NS}}}tc'))
            for row in rows
        ]
        table_shapes.append(
            {'rows': len(rows), 'columns': max(row_widths, default=0)}
        )
    return {'text': text, 'page_count': None, 'table_shapes': table_shapes}


def extract_pdf_baseline(path: Path) -> dict[str, Any]:
    """Extract a baseline through pypdfium2, independently of Docling layout."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    page_texts = []
    try:
        for index in range(len(document)):
            page = document[index]
            text_page = page.get_textpage()
            try:
                page_texts.append(text_page.get_text_range())
            finally:
                text_page.close()
                page.close()
    finally:
        document.close()
    return {
        'text': '\n'.join(page_texts),
        'page_count': len(page_texts),
        'table_shapes': None,
    }


def _document_text(document: dict[str, Any]) -> str:
    parts = []
    for node in document.get('texts', []):
        if isinstance(node, dict) and isinstance(node.get('text'), str):
            parts.append(node['text'])
    for table in document.get('tables', []):
        data = table.get('data', {}) if isinstance(table, dict) else {}
        for cell in data.get('table_cells', []):
            if isinstance(cell, dict) and isinstance(cell.get('text'), str):
                parts.append(cell['text'])
    return '\n'.join(parts)


def resolve_json_pointer(root: Any, pointer: str) -> Any:
    if pointer == '#':
        return root
    if not pointer.startswith('#/'):
        raise KeyError(pointer)
    value = root
    for encoded in pointer[2:].split('/'):
        key = encoded.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            value = value[int(key)]
        else:
            value = value[key]
    return value


def _walk(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield None, item
            yield from _walk(item)


def _pointer_metrics(document: dict[str, Any]) -> dict[str, int]:
    references = [
        value
        for key, value in _walk(document)
        if key == '$ref' and isinstance(value, str)
    ]
    broken = 0
    for reference in references:
        try:
            resolve_json_pointer(document, reference)
        except (KeyError, IndexError, ValueError, TypeError):
            broken += 1
    return {'json_pointer_count': len(references), 'broken_pointer_count': broken}


def _body_reachability_metrics(document: dict[str, Any]) -> dict[str, int]:
    reachable: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get('$ref')
            if isinstance(reference, str):
                if reference in reachable:
                    return
                reachable.add(reference)
                try:
                    visit(resolve_json_pointer(document, reference))
                except (KeyError, IndexError, ValueError, TypeError):
                    return
                return
            for key, item in value.items():
                if key != 'parent':
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(document.get('body', {}))
    expected: set[str] = set()
    for collection in (
        'groups', 'texts', 'pictures', 'tables',
        'key_value_items', 'form_items',
    ):
        for index, node in enumerate(document.get(collection, [])):
            if isinstance(node, dict) and node.get('content_layer') == 'body':
                expected.add(f'#/{collection}/{index}')
    return {
        'body_node_count': len(expected),
        'unreachable_body_node_count': len(expected - reachable),
    }


def _resource_metrics(document: dict[str, Any], document_dir: Path) -> dict[str, Any]:
    uris = [
        value
        for key, value in _walk(document)
        if key == 'uri' and isinstance(value, str)
    ]
    missing = 0
    escaped = 0
    absolute = 0
    resolved_root = document_dir.resolve()
    for uri in uris:
        if uri.startswith(('data:', 'http://', 'https://')):
            continue
        if ABSOLUTE_PATH_PATTERN.search(uri):
            absolute += 1
            continue
        candidate = (document_dir / uri).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            escaped += 1
            continue
        if not candidate.is_file():
            missing += 1
    return {
        'resource_reference_count': len(uris),
        'missing_resource_count': missing,
        'escaped_resource_count': escaped,
        'absolute_resource_path_count': absolute,
    }


def _serialized_resource_metrics(
    markdown: str,
    html_text: str,
    document_dir: Path,
) -> dict[str, int]:
    references = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', markdown)
    references.extend(
        html.unescape(value)
        for value in re.findall(
            r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']',
            html_text,
            flags=re.IGNORECASE,
        )
    )
    missing = escaped = absolute = 0
    resolved_root = document_dir.resolve()
    for reference in references:
        reference = unquote(reference)
        if reference.startswith(('data:', 'http://', 'https://')):
            continue
        if ABSOLUTE_PATH_PATTERN.search(reference):
            absolute += 1
            continue
        candidate = (document_dir / reference.replace('\\', '/')).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            escaped += 1
            continue
        if not candidate.is_file():
            missing += 1
    return {
        'serialized_resource_reference_count': len(references),
        'serialized_missing_resource_count': missing,
        'serialized_escaped_resource_count': escaped,
        'serialized_absolute_resource_count': absolute,
    }


def _table_metrics(document: dict[str, Any]) -> dict[str, int]:
    invalid_tables = 0
    invalid_cells = 0
    cell_count = 0
    for table in document.get('tables', []):
        data = table.get('data', {}) if isinstance(table, dict) else {}
        rows = data.get('num_rows', 0)
        columns = data.get('num_cols', 0)
        if not isinstance(rows, int) or not isinstance(columns, int) or rows <= 0 or columns <= 0:
            invalid_tables += 1
            continue
        cells = data.get('table_cells', [])
        cell_count += len(cells)
        for cell in cells:
            try:
                row_start = int(cell['start_row_offset_idx'])
                row_end = int(cell['end_row_offset_idx'])
                column_start = int(cell['start_col_offset_idx'])
                column_end = int(cell['end_col_offset_idx'])
                valid = (
                    0 <= row_start < row_end <= rows
                    and 0 <= column_start < column_end <= columns
                )
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                invalid_cells += 1
    return {
        'table_count': len(document.get('tables', [])),
        'table_cell_count': cell_count,
        'invalid_table_count': invalid_tables,
        'invalid_table_cell_count': invalid_cells,
    }


def _pdf_evidence_metrics(document: dict[str, Any]) -> dict[str, Any]:
    pages = document.get('pages', {})
    page_numbers = {int(key) for key in pages}
    invalid_page_references = 0
    invalid_bboxes = 0
    boundary_bboxes = 0
    counts: dict[str, int] = defaultdict(int)
    evidenced: dict[str, int] = defaultdict(int)
    for kind in ('texts', 'tables', 'pictures'):
        for node in document.get(kind, []):
            counts[kind] += 1
            provenance = node.get('prov', []) if isinstance(node, dict) else []
            if provenance:
                evidenced[kind] += 1
            for item in provenance:
                page_no = item.get('page_no')
                if not isinstance(page_no, int) or page_no not in page_numbers:
                    invalid_page_references += 1
                    continue
                bbox = item.get('bbox', {})
                page_size = pages[str(page_no)].get('size', {})
                width = page_size.get('width')
                height = page_size.get('height')
                coordinates = [bbox.get(key) for key in ('l', 't', 'r', 'b')]
                if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in coordinates):
                    invalid_bboxes += 1
                    continue
                if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
                    invalid_bboxes += 1
                    continue
                tolerance = 0.05
                if any(
                    value < -tolerance or value > limit + tolerance
                    for value, limit in zip(coordinates, (width, height, width, height))
                ):
                    invalid_bboxes += 1
                if any(
                    abs(value) <= tolerance or abs(value - limit) <= tolerance
                    for value, limit in zip(coordinates, (width, height, width, height))
                ):
                    boundary_bboxes += 1
    return {
        'docling_page_count': len(pages),
        'invalid_page_reference_count': invalid_page_references,
        'invalid_bbox_count': invalid_bboxes,
        'boundary_bbox_count': boundary_bboxes,
        'text_evidence_rate': _ratio(evidenced['texts'], counts['texts']),
        'table_evidence_rate': _ratio(evidenced['tables'], counts['tables']),
        'picture_evidence_rate': _ratio(evidenced['pictures'], counts['pictures']),
    }


def _log_warnings(log_path: Path) -> dict[str, list[str]]:
    warnings: dict[str, list[str]] = defaultdict(list)
    current_doc_id: str | None = None
    if not log_path.is_file():
        return warnings
    for line in log_path.read_text(encoding='utf-8', errors='replace').splitlines():
        context = LOG_CONTEXT_PATTERN.search(line)
        if context:
            current_doc_id = context.group(1)
        if '\tWARNING\t' in line and current_doc_id:
            warnings[current_doc_id].append(line.split('\t', 3)[-1])
    return warnings


def _artifact_hash_metrics(record: dict[str, Any]) -> tuple[int, int, int]:
    checked = mismatched = missing = 0
    for artifact in record.get('artifacts', []):
        checked += 1
        path = REPO_ROOT / artifact['path']
        if not path.is_file():
            missing += 1
        elif (
            path.stat().st_size != int(artifact['size_bytes'])
            or sha256_file(path) != artifact['sha256']
        ):
            mismatched += 1
    return checked, mismatched, missing


def _add_finding(
    findings: list[dict[str, Any]],
    severity: str,
    code: str,
    message: str,
    doc_id: str | None = None,
) -> None:
    findings.append(
        {'severity': severity, 'code': code, 'doc_id': doc_id or '', 'message': message}
    )


def _below_threshold(
    metrics: dict[str, Any],
    key: str,
    threshold: float,
    minimum_count_key: str | None = None,
    minimum_count: int = 0,
) -> bool:
    if minimum_count_key and int(metrics.get(minimum_count_key, 0)) < minimum_count:
        return False
    return float(metrics.get(key, 1.0)) < threshold


def _evaluate_document(
    record: dict[str, Any],
    run_root: Path,
    thresholds: dict[str, Any],
    warnings: dict[str, list[str]],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    doc_id = str(record['doc_id'])
    source_format = str(record['source_format'])
    document_dir = run_root / 'candidate' / doc_id
    json_path = document_dir / f'{doc_id}.json'
    markdown_path = document_dir / f'{doc_id}.md'
    html_path = document_dir / f'{doc_id}.html'
    metrics: dict[str, Any] = {
        'doc_id': doc_id,
        'source_format': source_format,
        'source_path': record['source_path'],
        'source_size_bytes': record['size_bytes'],
        'status': record['status'],
        'elapsed_seconds': record['elapsed_seconds'],
        'required_artifacts_present': all(
            path.is_file() for path in (json_path, markdown_path, html_path)
        ),
        'source_unchanged': bool(record.get('source_unchanged')),
    }
    if record['status'] != 'success':
        _add_finding(findings, 'FAIL', 'DOCUMENT_NOT_SUCCESS', str(record['status']), doc_id)
    if not metrics['required_artifacts_present']:
        _add_finding(findings, 'FAIL', 'REQUIRED_ARTIFACT_MISSING', 'MD/JSON/HTML 不完整', doc_id)
        return metrics

    checked, mismatched, missing = _artifact_hash_metrics(record)
    metrics.update(
        artifact_count=checked,
        artifact_hash_mismatch_count=mismatched,
        artifact_missing_count=missing,
    )
    if mismatched or missing:
        _add_finding(
            findings,
            'FAIL',
            'ARTIFACT_INTEGRITY',
            f'缺失 {missing}，哈希/大小不符 {mismatched}',
            doc_id,
        )

    raw_json = json_path.read_text(encoding='utf-8')
    raw_markdown = markdown_path.read_text(encoding='utf-8')
    raw_html = html_path.read_text(encoding='utf-8')
    combined = '\n'.join((raw_json, raw_markdown, raw_html))
    metrics['replacement_character_count'] = combined.count('\ufffd')
    metrics['unicode_escape_count'] = sum(
        int(codepoint, 16) >= 32
        for codepoint in UNICODE_ESCAPE_PATTERN.findall(raw_json)
    )
    windows_path_pattern = re.compile(r'(?i)(?<![a-z])[a-z]:[\\/]')
    metrics['absolute_path_leak_count'] = (
        len(windows_path_pattern.findall(combined))
        + combined.casefold().count('file:///')
    )
    metrics['mojibake_marker_count'] = sum(combined.count(marker) for marker in MOJIBAKE_MARKERS)
    for key, code in (
        ('replacement_character_count', 'REPLACEMENT_CHARACTER'),
        ('unicode_escape_count', 'UNICODE_ESCAPE'),
        ('absolute_path_leak_count', 'ABSOLUTE_PATH_LEAK'),
    ):
        if metrics[key]:
            _add_finding(findings, 'FAIL', code, f'{key}={metrics[key]}', doc_id)
    if metrics['mojibake_marker_count']:
        _add_finding(findings, 'WARN', 'POSSIBLE_MOJIBAKE', f"count={metrics['mojibake_marker_count']}", doc_id)

    try:
        document = json.loads(raw_json)
        metrics['json_parse_valid'] = True
    except json.JSONDecodeError as exc:
        metrics['json_parse_valid'] = False
        _add_finding(findings, 'FAIL', 'JSON_PARSE', str(exc), doc_id)
        return metrics

    try:
        from docling_core.types.doc import DoclingDocument

        with python_warnings.catch_warnings():
            python_warnings.simplefilter('ignore', UserWarning)
            DoclingDocument.model_validate(document)
        metrics['docling_schema_valid'] = True
    except Exception as exc:
        metrics['docling_schema_valid'] = False
        _add_finding(findings, 'FAIL', 'DOCLING_SCHEMA', str(exc)[:500], doc_id)

    metrics.update(_pointer_metrics(document))
    metrics.update(_body_reachability_metrics(document))
    metrics.update(_resource_metrics(document, document_dir))
    metrics.update(
        _serialized_resource_metrics(raw_markdown, raw_html, document_dir)
    )
    metrics.update(_table_metrics(document))
    metrics['text_node_count'] = len(document.get('texts', []))
    metrics['picture_count'] = len(document.get('pictures', []))
    if metrics['broken_pointer_count']:
        _add_finding(findings, 'FAIL', 'BROKEN_JSON_POINTER', f"count={metrics['broken_pointer_count']}", doc_id)
    if metrics['unreachable_body_node_count']:
        _add_finding(findings, 'FAIL', 'UNREACHABLE_BODY_NODE', f"count={metrics['unreachable_body_node_count']}", doc_id)
    if metrics['missing_resource_count'] or metrics['escaped_resource_count'] or metrics['absolute_resource_path_count']:
        _add_finding(findings, 'FAIL', 'RESOURCE_INTEGRITY', '资源缺失、越界或使用绝对路径', doc_id)
    if metrics['serialized_missing_resource_count'] or metrics['serialized_escaped_resource_count'] or metrics['serialized_absolute_resource_count']:
        _add_finding(findings, 'FAIL', 'SERIALIZED_RESOURCE_INTEGRITY', 'Markdown/HTML 资源缺失、越界或使用绝对路径', doc_id)
    if metrics['invalid_table_count'] or metrics['invalid_table_cell_count']:
        _add_finding(findings, 'FAIL', 'TABLE_STRUCTURE_INVALID', '表格维度或单元格跨度越界', doc_id)

    parsed_text = _document_text(document)
    metrics['parsed_character_count'] = len(_normalized_characters(parsed_text))
    if metrics['parsed_character_count'] == 0:
        _add_finding(findings, 'FAIL', 'EMPTY_TEXT', '结构化文本为空', doc_id)
    metrics.update(
        {
            f'markdown_{key}': value
            for key, value in compare_text(parsed_text, raw_markdown).items()
        }
    )

    baseline_path = REPO_ROOT / (
        record['source_path'] if source_format == 'pdf' else record['normalized_source_path']
    )
    try:
        baseline = (
            extract_pdf_baseline(baseline_path)
            if source_format == 'pdf'
            else extract_ooxml_baseline(baseline_path)
        )
        metrics.update(
            {
                f'source_{key}': value
                for key, value in compare_text(baseline['text'], parsed_text).items()
            }
        )
        metrics['baseline_error'] = None
    except Exception as exc:
        baseline = {'text': '', 'page_count': None, 'table_shapes': None}
        metrics['baseline_error'] = f'{type(exc).__name__}: {exc}'
        _add_finding(findings, 'FAIL', 'BASELINE_EXTRACTION', metrics['baseline_error'], doc_id)

    warning_config = thresholds['warning_thresholds']
    comparisons = (
        ('source_character_recall', 'source_reference_character_count', 'source_character_recall_min', 'SOURCE_CHARACTER_RECALL'),
        ('source_number_recall', 'source_reference_number_count', 'source_number_recall_min', 'SOURCE_NUMBER_RECALL'),
        ('markdown_character_recall', 'markdown_reference_character_count', 'markdown_character_recall_min', 'MARKDOWN_CHARACTER_RECALL'),
        ('markdown_number_recall', 'markdown_reference_number_count', 'markdown_number_recall_min', 'MARKDOWN_NUMBER_RECALL'),
    )
    for metric_key, count_key, threshold_key, code in comparisons:
        minimum = (
            int(warning_config['minimum_baseline_characters'])
            if 'character' in metric_key
            else int(warning_config['minimum_baseline_numbers'])
        )
        if _below_threshold(metrics, metric_key, float(warning_config[threshold_key]), count_key, minimum):
            _add_finding(findings, 'WARN', code, f"{metric_key}={metrics[metric_key]:.4f}", doc_id)

    if source_format == 'pdf':
        metrics.update(_pdf_evidence_metrics(document))
        metrics['independent_page_count'] = baseline['page_count']
        metrics['pdf_page_count_match'] = metrics['docling_page_count'] == baseline['page_count']
        if not metrics['pdf_page_count_match']:
            _add_finding(findings, 'FAIL', 'PDF_PAGE_COUNT_MISMATCH', f"Docling={metrics['docling_page_count']}, baseline={baseline['page_count']}", doc_id)
        if metrics['invalid_page_reference_count'] or metrics['invalid_bbox_count']:
            _add_finding(findings, 'FAIL', 'PDF_PROVENANCE_INVALID', '页码或边界框无效', doc_id)
        for metric_key, threshold_key, code in (
            ('text_evidence_rate', 'pdf_text_evidence_rate_min', 'PDF_TEXT_EVIDENCE'),
            ('table_evidence_rate', 'pdf_table_evidence_rate_min', 'PDF_TABLE_EVIDENCE'),
        ):
            if float(metrics[metric_key]) < float(warning_config[threshold_key]):
                _add_finding(findings, 'WARN', code, f"{metric_key}={metrics[metric_key]:.4f}", doc_id)
    else:
        baseline_tables = baseline.get('table_shapes') or []
        metrics['baseline_table_count'] = len(baseline_tables)
        metrics['word_table_count_recall'] = _ratio(
            min(metrics['table_count'], len(baseline_tables)),
            len(baseline_tables),
        )
        if metrics['word_table_count_recall'] < float(warning_config['word_table_count_recall_min']):
            _add_finding(findings, 'WARN', 'WORD_TABLE_COUNT_RECALL', f"recall={metrics['word_table_count_recall']:.4f}", doc_id)

    metrics['parser_warning_count'] = len(warnings.get(doc_id, []))
    if metrics['parser_warning_count']:
        _add_finding(findings, 'WARN', 'PARSER_LOG_WARNING', ' | '.join(warnings[doc_id])[:1000], doc_id)
    return metrics


def _select_samples(
    document_metrics: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    sample_per_format: int,
    seed: int,
) -> list[dict[str, Any]]:
    warning_counts = Counter(
        finding['doc_id'] for finding in findings if finding['doc_id']
    )
    rng = random.Random(seed)
    samples = []
    for source_format in ('doc', 'docx', 'pdf'):
        candidates = [item for item in document_metrics if item['source_format'] == source_format]
        selected: dict[str, set[str]] = defaultdict(set)

        def choose(item: dict[str, Any], reason: str) -> None:
            if item and len(selected) < sample_per_format:
                selected[item['doc_id']].add(reason)

        risky = sorted(candidates, key=lambda item: (-warning_counts[item['doc_id']], item['doc_id']))
        if risky and warning_counts[risky[0]['doc_id']]:
            choose(risky[0], 'quality_finding')
        choose(max(candidates, key=lambda item: item['source_size_bytes']), 'largest_source')
        choose(max(candidates, key=lambda item: item.get('table_count', 0)), 'table_heavy')
        choose(max(candidates, key=lambda item: item.get('docling_page_count', item.get('text_node_count', 0))), 'long_or_structurally_dense')
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        for item in shuffled:
            choose(item, 'seeded_random')
            if len(selected) >= sample_per_format:
                break
        by_id = {item['doc_id']: item for item in candidates}
        for doc_id, reasons in selected.items():
            item = by_id[doc_id]
            page_count = item.get('docling_page_count', 0)
            pages = []
            if page_count:
                pages = sorted({1, max(1, (page_count + 1) // 2), page_count})
            samples.append(
                {
                    'doc_id': doc_id,
                    'source_format': source_format,
                    'source_path': item['source_path'],
                    'selection_reasons': sorted(reasons),
                    'review_pages': pages,
                    'finding_count': warning_counts[doc_id],
                }
            )
    return samples


def _aggregate_metrics(
    records: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    expected: dict[str, int],
) -> dict[str, Any]:
    def sum_key(key: str) -> int:
        return sum(int(item.get(key, 0)) for item in documents)

    def rates(key: str) -> list[float]:
        return [float(item[key]) for item in documents if key in item]

    format_counts = Counter(record['source_format'] for record in records)
    pdf_documents = [item for item in documents if item['source_format'] == 'pdf']
    artifact_total = sum_key('artifact_count')
    artifact_bad = sum_key('artifact_hash_mismatch_count') + sum_key('artifact_missing_count')
    result = {
        'inventory_count': len(records),
        'expected_inventory_count': int(expected['total']),
        'format_counts': dict(sorted(format_counts.items())),
        'expected_format_counts': {key: int(value) for key, value in expected.items() if key != 'total'},
        'success_count': sum(record['status'] == 'success' for record in records),
        'success_rate': _ratio(sum(record['status'] == 'success' for record in records), len(records)),
        'required_artifact_rate': _ratio(sum(bool(item.get('required_artifacts_present')) for item in documents), len(records)),
        'source_unchanged_rate': _ratio(sum(bool(record.get('source_unchanged')) for record in records), len(records)),
        'artifact_count': artifact_total,
        'artifact_checksum_rate': _ratio(artifact_total - artifact_bad, artifact_total),
        'json_parse_rate': _ratio(sum(bool(item.get('json_parse_valid')) for item in documents), len(records)),
        'docling_schema_rate': _ratio(sum(bool(item.get('docling_schema_valid')) for item in documents), len(records)),
        'json_pointer_integrity_rate': _ratio(sum_key('json_pointer_count') - sum_key('broken_pointer_count'), sum_key('json_pointer_count')),
        'body_reachability_rate': _ratio(sum_key('body_node_count') - sum_key('unreachable_body_node_count'), sum_key('body_node_count')),
        'resource_integrity_rate': _ratio(sum_key('resource_reference_count') - sum_key('missing_resource_count') - sum_key('escaped_resource_count') - sum_key('absolute_resource_path_count'), sum_key('resource_reference_count')),
        'serialized_resource_integrity_rate': _ratio(sum_key('serialized_resource_reference_count') - sum_key('serialized_missing_resource_count') - sum_key('serialized_escaped_resource_count') - sum_key('serialized_absolute_resource_count'), sum_key('serialized_resource_reference_count')),
        'absolute_path_leak_count': sum_key('absolute_path_leak_count'),
        'replacement_character_count': sum_key('replacement_character_count'),
        'unicode_escape_count': sum_key('unicode_escape_count'),
        'table_count': sum_key('table_count'),
        'table_cell_count': sum_key('table_cell_count'),
        'picture_count': sum_key('picture_count'),
        'text_node_count': sum_key('text_node_count'),
        'pdf_page_count': sum_key('docling_page_count'),
        'pdf_page_count_match_rate': _ratio(sum(bool(item.get('pdf_page_count_match')) for item in pdf_documents), len(pdf_documents)),
        'parser_warning_count': sum_key('parser_warning_count'),
        'finding_counts': dict(Counter(item['severity'] for item in findings)),
    }
    for key in (
        'source_character_recall',
        'source_number_recall',
        'markdown_character_recall',
        'markdown_number_recall',
        'text_evidence_rate',
        'table_evidence_rate',
        'word_table_count_recall',
    ):
        values = rates(key)
        if values:
            result[f'{key}_min'] = round(min(values), 6)
            result[f'{key}_median'] = round(statistics.median(values), 6)
    return result


def _apply_hard_gates(
    metrics: dict[str, Any],
    config: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    expected = config['expected_counts']
    if metrics['inventory_count'] != int(expected['total']) or metrics['format_counts'] != {key: int(value) for key, value in expected.items() if key != 'total'}:
        _add_finding(findings, 'FAIL', 'INVENTORY_COUNT', '实际清单与配置预期不一致')
    for key, expected_value in config['hard_gates'].items():
        actual = metrics[key]
        if actual != expected_value:
            _add_finding(findings, 'FAIL', 'HARD_GATE_' + key.upper(), f'expected={expected_value}, actual={actual}')


def _write_findings(path: Path, findings: list[dict[str, Any]]) -> None:
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=('severity', 'code', 'doc_id', 'message'))
        writer.writeheader()
        writer.writerows(findings)


def _write_human_review(path: Path, samples: list[dict[str, Any]], dimensions: list[str]) -> None:
    fields = [
        'doc_id', 'source_format', 'source_path', 'selection_reasons', 'review_pages',
        *dimensions, 'highest_severity', 'problem_description', 'reviewer', 'reviewed_at', 'review_status',
    ]
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for sample in samples:
            row = {field: '' for field in fields}
            row.update(
                doc_id=sample['doc_id'],
                source_format=sample['source_format'],
                source_path=sample['source_path'],
                selection_reasons=';'.join(sample['selection_reasons']),
                review_pages=';'.join(str(value) for value in sample['review_pages']),
                review_status='PENDING',
            )
            writer.writerow(row)


def _render_report(summary: dict[str, Any], samples: list[dict[str, Any]], findings: list[dict[str, Any]]) -> str:
    metrics = summary['metrics']
    lines = [
        f"# 文档解析质量检测报告：{summary['run_id']}",
        '',
        f"- 检测时间：{summary['evaluated_at']}",
        f"- 自动检测结论：**{summary['automated_status']}**",
        f"- 发布建议：**{summary['release_recommendation']}**",
        f"- 文档：{metrics['success_count']}/{metrics['inventory_count']} 成功；DOC {metrics['format_counts'].get('doc', 0)}、DOCX {metrics['format_counts'].get('docx', 0)}、PDF {metrics['format_counts'].get('pdf', 0)}",
        '',
        '## 核心指标',
        '',
        '| 指标 | 结果 |',
        '|---|---:|',
        f"| 成功率 | {metrics['success_rate']:.2%} |",
        f"| MD/JSON/HTML 覆盖率 | {metrics['required_artifact_rate']:.2%} |",
        f"| 源文件未变化率 | {metrics['source_unchanged_rate']:.2%} |",
        f"| 产物哈希一致率 | {metrics['artifact_checksum_rate']:.2%} |",
        f"| JSON 可解析率 / Docling 模型校验率 | {metrics['json_parse_rate']:.2%} / {metrics['docling_schema_rate']:.2%} |",
        f"| JSON Pointer / 正文可达 / JSON资源 / MD·HTML资源完整率 | {metrics['json_pointer_integrity_rate']:.2%} / {metrics['body_reachability_rate']:.2%} / {metrics['resource_integrity_rate']:.2%} / {metrics['serialized_resource_integrity_rate']:.2%} |",
        f"| PDF 页数一致率 | {metrics['pdf_page_count_match_rate']:.2%} |",
        f"| 文本节点 / 表格 / 单元格 / 图片 / PDF 页 | {metrics['text_node_count']} / {metrics['table_count']} / {metrics['table_cell_count']} / {metrics['picture_count']} / {metrics['pdf_page_count']} |",
        f"| 绝对路径泄露 / 替换字符 / 非控制字符 Unicode 转义 | {metrics['absolute_path_leak_count']} / {metrics['replacement_character_count']} / {metrics['unicode_escape_count']} |",
        '',
        '## 保真度摘要',
        '',
        '| 指标 | 最小值 | 中位数 |',
        '|---|---:|---:|',
    ]
    for key, label in (
        ('source_character_recall', '独立源文本字符召回'),
        ('source_number_recall', '独立源文本数字召回'),
        ('markdown_character_recall', 'JSON→Markdown 字符召回'),
        ('markdown_number_recall', 'JSON→Markdown 数字召回'),
        ('text_evidence_rate', 'PDF 文本证据覆盖'),
        ('table_evidence_rate', 'PDF 表格证据覆盖'),
        ('word_table_count_recall', 'Word 表格数量召回'),
    ):
        if f'{key}_min' in metrics:
            lines.append(f"| {label} | {metrics[f'{key}_min']:.2%} | {metrics[f'{key}_median']:.2%} |")
    lines.extend(['', '## 问题与告警', ''])
    if not findings:
        lines.append('未发现自动检测问题。')
    else:
        lines.extend(['| 级别 | 代码 | 文档 | 说明 |', '|---|---|---|---|'])
        for finding in findings[:200]:
            message = str(finding['message']).replace('|', '\\|').replace('\n', ' ')
            lines.append(f"| {finding['severity']} | {finding['code']} | {finding['doc_id'] or '-'} | {message} |")
        if len(findings) > 200:
            lines.append(f"\n完整问题共 {len(findings)} 条，见 `findings.csv`。")
    lines.extend([
        '',
        '## 人工复核抽样',
        '',
        '每类格式固定抽取 8 份，优先覆盖有告警、最大文件、表格密集、长文档，再用固定随机种子补足。PDF 检查首页、中页和末页。',
        '',
        '| 文档 | 格式 | 抽样原因 | PDF 页 | 自动问题数 |',
        '|---|---|---|---:|---:|',
    ])
    for sample in samples:
        lines.append(
            f"| {sample['doc_id']} | {sample['source_format']} | {', '.join(sample['selection_reasons'])} | {', '.join(map(str, sample['review_pages'])) or '-'} | {sample['finding_count']} |"
        )
    lines.extend([
        '',
        '人工评分表见 `human_review.csv`。全部样本完成复核且无 P0/P1 后，才建议发布为下游向量库正式输入。',
        '',
        '## 方法说明',
        '',
        '- L0 完整性硬门槛：清单、状态、源哈希、必需产物、产物哈希、UTF-8/JSON、引用、资源、路径泄露和 PDF 页数。',
        '- L1 无标注保真检测：通过独立 OOXML/PDFium 提取计算字符与数字多重集召回，并核查表格维度、PDF 页码/边界框和证据覆盖。',
        '- L2 金标回归：后续把人工确认样本固化，计算编辑距离、阅读顺序、TEDS/GriTS 等有标注指标。',
        '- L3 人工抽检：按格式和风险分层抽样，检查文本、阅读顺序、层级、列表、表格、数字、图片/公式和证据追踪。',
        '- L4 RAG 端到端：分块后增加证据可检索率、引用正确率、答案忠实度测试。',
        '',
        '> 注意：无标注字符召回是异常筛查指标，不等价于逐字金标准确率；字符多重集不评价阅读顺序，因此必须配合人工复核和后续金标集。',
    ])
    return '\n'.join(lines) + '\n'


def evaluate_run(config_path: Path) -> dict[str, Any]:
    raw_config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config = raw_config['quality']
    run_root = REPO_ROOT / config['runs_root'] / config['run_id']
    if not run_root.is_dir():
        raise FileNotFoundError(run_root)
    quality_root = run_root / 'quality'
    quality_root.mkdir(exist_ok=True)
    records = _read_jsonl(run_root / 'manifest.jsonl')
    warnings = _log_warnings(run_root / 'logs' / 'run.log')
    findings: list[dict[str, Any]] = []
    documents = [
        _evaluate_document(record, run_root, config, warnings, findings)
        for record in records
    ]
    metrics = _aggregate_metrics(records, documents, findings, config['expected_counts'])
    _apply_hard_gates(metrics, config, findings)
    metrics['finding_counts'] = dict(Counter(item['severity'] for item in findings))
    automated_status = 'FAIL' if any(item['severity'] == 'FAIL' for item in findings) else ('PASS_WITH_WARNINGS' if findings else 'PASS')
    samples = _select_samples(
        documents,
        findings,
        int(config['human_review']['sample_per_format']),
        int(config['human_review']['random_seed']),
    )
    summary = {
        'run_id': config['run_id'],
        'quality_config_version': config['config_version'],
        'methodology_version': config['methodology_version'],
        'evaluated_at': datetime.now(timezone.utc).isoformat(),
        'automated_status': automated_status,
        'human_review_status': 'PENDING',
        'release_recommendation': 'HOLD' if automated_status == 'FAIL' else 'HOLD_FOR_HUMAN_REVIEW',
        'metrics': metrics,
    }
    _write_json(quality_root / 'run_metrics.json', summary)
    _write_json(quality_root / 'qa_summary.json', summary)
    _write_jsonl(quality_root / 'document_metrics.jsonl', documents)
    _write_findings(quality_root / 'findings.csv', findings)
    _write_json(quality_root / 'sample_manifest.json', {'samples': samples})
    _write_human_review(
        quality_root / 'human_review.csv',
        samples,
        list(config['human_review']['required_dimensions']),
    )
    (quality_root / 'quality_config.yaml').write_text(
        config_path.read_text(encoding='utf-8'), encoding='utf-8', newline='\n'
    )
    report_text = _render_report(summary, samples, findings)
    (quality_root / 'quality_report.md').write_text(
        report_text, encoding='utf-8', newline='\n'
    )
    (quality_root / 'qa_summary.md').write_text(
        report_text, encoding='utf-8', newline='\n'
    )
    return summary
