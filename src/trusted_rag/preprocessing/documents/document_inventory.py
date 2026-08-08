"""Deterministic, read-only discovery for the document source corpus."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class DocumentSource:
    doc_id: str
    source_id: str
    source_path: Path
    relative_path: str
    source_format: str
    source_sha256: str
    size_bytes: int
    modified_at: str
    purpose: str = ''


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(block_size), b''):
            digest.update(block)
    return digest.hexdigest().upper()


def _stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest().upper()


def describe_documents(
    paths: Sequence[Path],
    input_root: Path,
) -> list[DocumentSource]:
    prefixes: dict[str, int] = {}
    parsed_prefixes: dict[Path, str | None] = {}
    for path in paths:
        match = re.match(r'^(\d+)_', path.stem)
        prefix = match.group(1) if match else None
        parsed_prefixes[path] = prefix
        if prefix is not None:
            prefixes[prefix] = prefixes.get(prefix, 0) + 1

    sources = []
    seen_ids = set()
    for path in paths:
        relative_path = path.relative_to(input_root).as_posix()
        source_hash = sha256_file(path)
        source_id = _stable_digest(
            '{}\n{}'.format(relative_path.casefold(), source_hash)
        )
        prefix = parsed_prefixes[path]
        if prefix is not None and prefixes[prefix] == 1:
            doc_id = prefix
        elif prefix is not None:
            doc_id = '{}-{}'.format(prefix, source_id[:8].lower())
        else:
            doc_id = source_id[:12].lower()
        if doc_id in seen_ids:
            raise ValueError('duplicate derived doc_id: {}'.format(doc_id))
        seen_ids.add(doc_id)
        sources.append(
            DocumentSource(
                doc_id=doc_id,
                source_id=source_id,
                source_path=path,
                relative_path=relative_path,
                source_format=path.suffix.lower().lstrip('.'),
                source_sha256=source_hash,
                size_bytes=path.stat().st_size,
                modified_at=datetime.fromtimestamp(
                    path.stat().st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
            )
        )
    return sources


def discover_documents(
    input_root: Path,
    recursive: bool,
    include_extensions: Sequence[str],
    exclude_name_prefixes: Sequence[str],
    order_by: str,
) -> tuple[DocumentSource, ...]:
    extensions = {
        value.lower().lstrip('.') for value in include_extensions
    }
    if not extensions or not extensions <= {'doc', 'docx', 'pdf'}:
        raise ValueError(
            'include_extensions must be a non-empty subset of doc, docx, pdf'
        )
    iterator = input_root.rglob('*') if recursive else input_root.glob('*')
    paths = [
        path
        for path in iterator
        if path.is_file()
        and path.suffix.lower().lstrip('.') in extensions
        and not any(
            path.name.startswith(prefix) for prefix in exclude_name_prefixes
        )
    ]
    paths.sort(key=lambda path: path.relative_to(input_root).as_posix().casefold())
    sources = describe_documents(paths, input_root)

    if order_by == 'relative_path':
        sources.sort(key=lambda source: source.relative_path.casefold())
    elif order_by == 'numeric_id':
        sources.sort(
            key=lambda source: (
                int(source.doc_id) if source.doc_id.isdigit() else 10**18,
                source.relative_path.casefold(),
            )
        )
    elif order_by == 'size_ascending':
        sources.sort(
            key=lambda source: (
                source.size_bytes,
                source.relative_path.casefold(),
            )
        )
    else:
        raise ValueError(
            'order_by must be relative_path, numeric_id, or size_ascending'
        )
    return tuple(sources)
