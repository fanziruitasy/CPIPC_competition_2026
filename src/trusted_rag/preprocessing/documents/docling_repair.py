"""Deterministic postprocessing repairs for staging Docling runs."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .docling_config import EXPECTED_RUNS_ROOT, REPO_ROOT, _ensure_child
from .document_inventory import sha256_file


def _rewrite_stale_uri(
    value: Any,
    run_root: Path,
    doc_id: str,
    json_dir: Path,
    findings: list[dict[str, str]],
) -> Any:
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            if key == 'uri' and isinstance(item, str):
                normalized = item.replace('\\', '/')
                match = re.search(
                    r'/candidate/\.?'
                    + re.escape(doc_id)
                    + r'(?:\.tmp)?/(.+)$',
                    normalized,
                    flags=re.IGNORECASE,
                )
                if match:
                    relative_uri = match.group(1)
                    target = (json_dir / relative_uri).resolve()
                    try:
                        target.relative_to(json_dir.resolve())
                    except ValueError as exc:
                        raise ValueError(
                            'rewritten URI escaped document directory: {}'.format(
                                item
                            )
                        ) from exc
                    if not target.is_file():
                        raise FileNotFoundError(
                            'rewritten URI target is missing: {}'.format(target)
                        )
                    findings.append(
                        {
                            'doc_id': doc_id,
                            'old_uri': item,
                            'new_uri': relative_uri,
                        }
                    )
                    item = relative_uri
                elif Path(item).is_absolute():
                    try:
                        Path(item).resolve().relative_to(run_root.resolve())
                    except ValueError:
                        pass
                    else:
                        raise ValueError(
                            'unrecognized absolute run URI: {}'.format(item)
                        )
            rewritten[key] = _rewrite_stale_uri(
                item,
                run_root,
                doc_id,
                json_dir,
                findings,
            )
        return rewritten
    if isinstance(value, list):
        return [
            _rewrite_stale_uri(
                item,
                run_root,
                doc_id,
                json_dir,
                findings,
            )
            for item in value
        ]
    return value


def repair_referenced_uris(run_id: str) -> dict[str, Any]:
    run_root = _ensure_child(
        EXPECTED_RUNS_ROOT / run_id,
        EXPECTED_RUNS_ROOT,
        'Docling run',
    )
    manifest_path = run_root / 'manifest.jsonl'
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    records = [
        json.loads(line)
        for line in manifest_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]
    findings: list[dict[str, str]] = []
    changed_document_ids: set[str] = set()
    rewritten_text_reference_count = 0
    for record in records:
        doc_id = str(record['doc_id'])
        json_dir = run_root / 'candidate' / doc_id
        json_path = json_dir / (doc_id + '.json')
        if not json_path.is_file():
            continue
        original = json.loads(json_path.read_text(encoding='utf-8'))
        document_findings: list[dict[str, str]] = []
        rewritten = _rewrite_stale_uri(
            original,
            run_root,
            doc_id,
            json_dir,
            document_findings,
        )
        changed_paths: list[Path] = []
        if document_findings:
            temporary = json_path.with_name(json_path.name + '.repair.tmp')
            temporary.write_text(
                json.dumps(rewritten, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
                newline='\n',
            )
            json.loads(temporary.read_text(encoding='utf-8'))
            os.replace(temporary, json_path)
            changed_paths.append(json_path)
        stale_prefixes = (
            str(run_root / 'candidate' / ('.' + doc_id + '.tmp')) + '\\',
            (run_root / 'candidate' / ('.' + doc_id + '.tmp')).as_posix() + '/',
        )
        for suffix in ('.md', '.html'):
            text_path = json_dir / (doc_id + suffix)
            if not text_path.is_file():
                continue
            original_text = text_path.read_text(encoding='utf-8')
            rewritten_text = original_text
            for prefix in stale_prefixes:
                occurrences = rewritten_text.count(prefix)
                rewritten_text_reference_count += occurrences
                rewritten_text = rewritten_text.replace(prefix, '')
            if suffix == '.html':
                image_source_pattern = re.compile(
                    r'(?i)(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])'
                )

                def rewrite_image_source(match: re.Match[str]) -> str:
                    nonlocal rewritten_text_reference_count
                    decoded = unquote(unescape(match.group(2))).replace('\\', '/')
                    reference_match = re.search(
                        r'/candidate/\.'
                        + re.escape(doc_id)
                        + r'\.tmp/(.+)$',
                        decoded,
                        flags=re.IGNORECASE,
                    )
                    if not reference_match:
                        return match.group(0)
                    relative = reference_match.group(1)
                    target = (json_dir / relative).resolve()
                    try:
                        target.relative_to(json_dir.resolve())
                    except ValueError as exc:
                        raise ValueError(
                            'rewritten HTML resource escaped document directory'
                        ) from exc
                    if not target.is_file():
                        raise FileNotFoundError(target)
                    rewritten_text_reference_count += 1
                    return match.group(1) + relative + match.group(3)

                rewritten_text = image_source_pattern.sub(
                    rewrite_image_source,
                    rewritten_text,
                )
            if rewritten_text != original_text:
                text_path.write_text(rewritten_text, encoding='utf-8', newline='\n')
                changed_paths.append(text_path)
        for changed_path in changed_paths:
            relative_path = changed_path.relative_to(REPO_ROOT).as_posix()
            matched_artifact = False
            for artifact in record.get('artifacts', []):
                if artifact.get('path') == relative_path:
                    artifact['size_bytes'] = changed_path.stat().st_size
                    artifact['sha256'] = sha256_file(changed_path)
                    matched_artifact = True
                    break
            if not matched_artifact:
                raise RuntimeError(
                    'manifest has no artifact entry for {}'.format(relative_path)
                )
        if changed_paths:
            changed_document_ids.add(doc_id)
        findings.extend(document_findings)

    manifest_temporary = manifest_path.with_name(manifest_path.name + '.repair.tmp')
    with manifest_temporary.open('w', encoding='utf-8', newline='\n') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            stream.write('\n')
    os.replace(manifest_temporary, manifest_path)

    report = {
        'run_id': run_id,
        'repair': 'rewrite_stale_absolute_artifact_references',
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'changed_documents': len(changed_document_ids),
        'rewritten_uri_count': len(findings),
        'rewritten_text_reference_count': rewritten_text_reference_count,
        'source_data_modified': False,
        'findings': findings,
    }
    report_path = run_root / 'reports' / 'uri-repair.json'
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
        newline='\n',
    )
    return report
