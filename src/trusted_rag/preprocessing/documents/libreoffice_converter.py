"""Auditable legacy-DOC normalization through LibreOffice.

Python owns discovery, path safety, hashing, orchestration, and manifests.
LibreOffice remains the format-conversion engine and is invoked without a shell.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml


REPO_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_INPUT_ROOT = (
    REPO_ROOT / 'Data' / 'originalData' / 'docANDpdf'
).resolve()
EXPECTED_RUNS_ROOT = (
    REPO_ROOT / 'Data' / 'staging' / 'document_preprocessing' / 'runs'
).resolve()

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class LegacyDocSource:
    doc_id: str
    source_id: str
    source_path: Path
    source_relative_path: str
    source_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class LibreOfficeRunPlan:
    config_path: Path
    config_digest: str
    project: dict[str, Any]
    libreoffice: dict[str, Any]
    input_root: Path
    runs_root: Path
    run_root: Path
    sources: tuple[LegacyDocSource, ...]


class LibreOfficeConversionError(RuntimeError):
    """A source could not be converted into a verified DOCX artifact."""


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(block_size), b''):
            digest.update(block)
    return digest.hexdigest().upper()


def _stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest().upper()


def _ensure_child(path: Path, parent: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise ValueError(
            '{} escaped approved root: {}'.format(label, resolved)
        ) from exc
    return resolved


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError('{} must be a mapping'.format(label))
    return value


def discover_legacy_docs(
    input_root: Path,
    recursive: bool,
    exclude_name_prefixes: Sequence[str],
) -> tuple[LegacyDocSource, ...]:
    iterator = input_root.rglob('*') if recursive else input_root.glob('*')
    paths = sorted(
        (
            path
            for path in iterator
            if path.is_file()
            and path.suffix.lower() == '.doc'
            and not any(
                path.name.startswith(prefix)
                for prefix in exclude_name_prefixes
            )
        ),
        key=lambda path: path.relative_to(input_root).as_posix().casefold(),
    )

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
        relative_path = path.relative_to(REPO_ROOT).as_posix()
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
            LegacyDocSource(
                doc_id=doc_id,
                source_id=source_id,
                source_path=path,
                source_relative_path=relative_path,
                source_sha256=source_hash,
                size_bytes=path.stat().st_size,
            )
        )
    return tuple(sources)


def load_libreoffice_run_plan(config_path: Path) -> LibreOfficeRunPlan:
    config_path = _ensure_child(config_path, REPO_ROOT, 'config path')
    with config_path.open('r', encoding='utf-8') as stream:
        root = yaml.safe_load(stream)
    root = _require_mapping(root, 'configuration root')
    if set(root) != {'project', 'libreoffice'}:
        raise ValueError(
            'configuration root must contain only project and libreoffice sections'
        )

    project = _require_mapping(root['project'], 'project')
    libreoffice = _require_mapping(root['libreoffice'], 'libreoffice')
    if str(project.get('pipeline_version')) != '0.01':
        raise ValueError('project.pipeline_version must be 0.01')
    if str(project.get('include_extension')).lower() != '.doc':
        raise ValueError('project.include_extension must be .doc')
    if project.get('short_source_names') is not True:
        raise ValueError('project.short_source_names must be true on Windows')

    input_root = (REPO_ROOT / str(project['input_root'])).resolve()
    runs_root = (REPO_ROOT / str(project['runs_root'])).resolve()
    if input_root != EXPECTED_INPUT_ROOT:
        raise ValueError(
            'project.input_root must resolve to {}'.format(EXPECTED_INPUT_ROOT)
        )
    if runs_root != EXPECTED_RUNS_ROOT:
        raise ValueError(
            'project.runs_root must resolve to {}'.format(EXPECTED_RUNS_ROOT)
        )
    run_root = _ensure_child(
        runs_root / str(project['run_id']),
        runs_root,
        'run root',
    )

    executable = Path(str(libreoffice['executable'])).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    if libreoffice.get('headless') is not True:
        raise ValueError('libreoffice.headless must be true')
    if str(libreoffice.get('target_format')).lower() != 'docx':
        raise ValueError('libreoffice.target_format must be docx')
    timeout = libreoffice.get('timeout_seconds')
    if timeout is not None and float(timeout) <= 0:
        raise ValueError('libreoffice.timeout_seconds must be null or positive')
    if int(libreoffice.get('automatic_retries', 0)) != 0:
        raise ValueError('libreoffice.automatic_retries must be 0 in v0.01')
    if libreoffice.get('overwrite_existing') is not False:
        raise ValueError('libreoffice.overwrite_existing must be false')

    sources = discover_legacy_docs(
        input_root=input_root,
        recursive=bool(project.get('recursive', True)),
        exclude_name_prefixes=tuple(
            str(value)
            for value in project.get('exclude_name_prefixes', ['~$'])
        ),
    )
    if not sources:
        raise ValueError('no legacy DOC sources were discovered')

    return LibreOfficeRunPlan(
        config_path=config_path,
        config_digest=sha256_file(config_path),
        project=project,
        libreoffice=libreoffice,
        input_root=input_root,
        runs_root=runs_root,
        run_root=run_root,
        sources=sources,
    )


def _run_command(
    arguments: Sequence[str],
    timeout_seconds: float | None,
    runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise LibreOfficeConversionError(
            'LibreOffice exceeded timeout_seconds={}'.format(timeout_seconds)
        ) from exc


def probe_libreoffice(
    executable: Path,
    timeout_seconds: float | None,
    runner: CommandRunner = subprocess.run,
) -> str:
    result = _run_command(
        [str(executable), '--version'],
        timeout_seconds,
        runner,
    )
    if result.returncode != 0:
        raise LibreOfficeConversionError(
            'LibreOffice version probe failed: {}'.format(result.stderr.strip())
        )
    version_text = (result.stdout or result.stderr).strip()
    if not version_text:
        raise LibreOfficeConversionError('LibreOffice version probe was empty')
    return version_text


def convert_one_doc(
    executable: Path,
    staged_doc: Path,
    output_dir: Path,
    profile_dir: Path,
    export_filter: str,
    timeout_seconds: float | None,
    runner: CommandRunner = subprocess.run,
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    output_dir.mkdir(parents=True, exist_ok=False)
    profile_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (staged_doc.stem + '.docx')
    arguments = [
        str(executable),
        '-env:UserInstallation={}'.format(profile_dir.resolve().as_uri()),
        '--headless',
        '--convert-to',
        'docx:{}'.format(export_filter),
        '--outdir',
        str(output_dir),
        str(staged_doc),
    ]
    result = _run_command(arguments, timeout_seconds, runner)
    if result.returncode != 0:
        raise LibreOfficeConversionError(
            'LibreOffice returned {}: {}'.format(
                result.returncode,
                (result.stderr or result.stdout).strip(),
            )
        )
    if not target.is_file() or target.stat().st_size == 0:
        raise LibreOfficeConversionError(
            'LibreOffice returned success but no non-empty DOCX was created: '
            '{}'.format(target)
        )
    return target, result


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(_json_dump(value) + '\n', encoding='utf-8', newline='\n')


def plan_summary(plan: LibreOfficeRunPlan) -> dict[str, Any]:
    timeout = plan.libreoffice.get('timeout_seconds')
    version_text = probe_libreoffice(
        Path(str(plan.libreoffice['executable'])),
        float(timeout) if timeout is not None else None,
    )
    return {
        'check': 'passed',
        'pipeline_version': plan.project['pipeline_version'],
        'config_version': plan.project['config_version'],
        'config_sha256': plan.config_digest,
        'run_id': plan.project['run_id'],
        'run_root': plan.run_root.relative_to(REPO_ROOT).as_posix(),
        'run_root_exists': plan.run_root.exists(),
        'source_count': len(plan.sources),
        'source_size_bytes': sum(source.size_bytes for source in plan.sources),
        'libreoffice_version': version_text,
        'timeout_seconds': timeout,
        'automatic_retries': 0,
    }


def check_config(config_path: Path) -> dict[str, Any]:
    return plan_summary(load_libreoffice_run_plan(config_path))


def _append_manifest(path: Path, record: dict[str, Any]) -> None:
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        stream.write('\n')
        stream.flush()


def run_conversion(
    config_path: Path,
    runner: CommandRunner = subprocess.run,
) -> int:
    plan = load_libreoffice_run_plan(config_path)
    if plan.run_root.exists():
        raise FileExistsError(
            'run directory already exists; choose a new project.run_id: {}'.format(
                plan.run_root
            )
        )

    normalized_root = plan.run_root / 'normalized_sources'
    work_root = plan.run_root / 'work'
    logs_root = plan.run_root / 'logs'
    reports_root = plan.run_root / 'reports'
    profile_root = work_root / 'libreoffice-profile'
    for directory in (normalized_root, work_root, logs_root, reports_root):
        directory.mkdir(parents=True, exist_ok=False)

    timeout_raw = plan.libreoffice.get('timeout_seconds')
    timeout_seconds = float(timeout_raw) if timeout_raw is not None else None
    executable = Path(str(plan.libreoffice['executable']))
    version_text = probe_libreoffice(executable, timeout_seconds, runner)
    shutil.copy2(plan.config_path, reports_root / plan.config_path.name)

    inventory = [
        {
            'doc_id': source.doc_id,
            'source_id': source.source_id,
            'source_path': source.source_relative_path,
            'source_sha256': source.source_sha256,
            'size_bytes': source.size_bytes,
        }
        for source in plan.sources
    ]
    inventory_path = plan.run_root / 'inventory.jsonl'
    with inventory_path.open('w', encoding='utf-8', newline='\n') as stream:
        for item in inventory:
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
            stream.write('\n')

    started_at = datetime.now(timezone.utc).isoformat()
    _write_json(
        plan.run_root / 'run.json',
        {
            'run_id': plan.project['run_id'],
            'pipeline_version': plan.project['pipeline_version'],
            'config_version': plan.project['config_version'],
            'config_path': plan.config_path.relative_to(REPO_ROOT).as_posix(),
            'config_sha256': plan.config_digest,
            'started_at': started_at,
            'libreoffice_version': version_text,
            'execution_policy': {
                'python_orchestration': True,
                'shell': False,
                'timeout_seconds': timeout_seconds,
                'automatic_retries': 0,
            },
        },
    )

    manifest_path = plan.run_root / 'manifest.jsonl'
    status_counts: dict[str, int] = {}
    for index, source in enumerate(plan.sources, start=1):
        started = time.monotonic()
        record: dict[str, Any] = {
            'doc_id': source.doc_id,
            'source_id': source.source_id,
            'source_path': source.source_relative_path,
            'source_sha256_before': source.source_sha256,
            'source_sha256_after': None,
            'source_unchanged': False,
            'normalized_path': None,
            'normalized_sha256': None,
            'status': 'failed',
            'libreoffice_version': version_text,
            'command_stdout': '',
            'command_stderr': '',
            'errors': [],
            'elapsed_seconds': None,
        }
        print(
            '[{}/{}][{}] converting DOC to DOCX'.format(
                index, len(plan.sources), source.doc_id
            ),
            flush=True,
        )
        temporary_output = normalized_root / ('.' + source.doc_id + '.tmp')
        final_output = normalized_root / source.doc_id
        source_work = work_root / source.doc_id
        try:
            source_work.mkdir(parents=True, exist_ok=False)
            staged_doc = source_work / (source.doc_id + '.doc')
            shutil.copy2(source.source_path, staged_doc)
            if sha256_file(staged_doc) != source.source_sha256:
                raise RuntimeError('staged DOC hash does not match source')
            converted_path, result = convert_one_doc(
                executable=executable,
                staged_doc=staged_doc,
                output_dir=temporary_output,
                profile_dir=profile_root,
                export_filter=str(plan.libreoffice['export_filter']),
                timeout_seconds=timeout_seconds,
                runner=runner,
            )
            record['command_stdout'] = result.stdout
            record['command_stderr'] = result.stderr
            normalized_hash = sha256_file(converted_path)
            os.replace(temporary_output, final_output)
            final_docx = final_output / converted_path.name
            record['normalized_path'] = final_docx.relative_to(REPO_ROOT).as_posix()
            record['normalized_sha256'] = normalized_hash
            record['status'] = 'success'
        except Exception as exc:
            record['errors'].append(
                {'type': type(exc).__name__, 'message': str(exc)}
            )
        finally:
            try:
                source_hash_after = sha256_file(source.source_path)
                record['source_sha256_after'] = source_hash_after
                record['source_unchanged'] = (
                    source_hash_after == source.source_sha256
                )
                if not record['source_unchanged']:
                    record['status'] = 'failed'
                    record['errors'].append(
                        {
                            'type': 'SourceMutationError',
                            'message': 'source SHA-256 changed during conversion',
                        }
                    )
            except Exception as exc:
                record['status'] = 'failed'
                record['errors'].append(
                    {'type': type(exc).__name__, 'message': str(exc)}
                )
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            _append_manifest(manifest_path, record)
            status = str(record['status'])
            status_counts[status] = status_counts.get(status, 0) + 1
            print(
                '[{}/{}][{}] status={} elapsed={:.3f}s'.format(
                    index,
                    len(plan.sources),
                    source.doc_id,
                    status,
                    record['elapsed_seconds'],
                ),
                flush=True,
            )

    summary = {
        'run_id': plan.project['run_id'],
        'pipeline_version': plan.project['pipeline_version'],
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'source_count': len(plan.sources),
        'status_counts': status_counts,
        'all_sources_unchanged': all(
            sha256_file(source.source_path) == source.source_sha256
            for source in plan.sources
        ),
    }
    _write_json(reports_root / 'summary.json', summary)
    print(_json_dump(summary))
    return 0 if status_counts.get('failed', 0) == 0 else 2
