from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_INPUT_ROOT = (
    REPO_ROOT / 'Data' / 'originalData' / 'docANDpdf'
).resolve()
EXPECTED_RUNS_ROOT = (
    REPO_ROOT / 'Data' / 'staging' / 'document_preprocessing' / 'runs'
).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest().upper()


def ensure_child(path: Path, parent: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise ValueError(
            '{} escaped approved root: {}'.format(label, resolved)
        ) from exc
    return resolved


def load_config(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError('Configuration root must be a mapping')
    return config


def run_command(
    command: list[str],
    log_path: Path,
    timeout_seconds: int,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    started = time.monotonic()
    environment = os.environ.copy()
    environment['PYTHONUTF8'] = '1'
    if environment_overrides:
        environment.update(environment_overrides)
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    elapsed = time.monotonic() - started
    stdout = result.stdout.decode('utf-8', errors='replace')
    stderr = result.stderr.decode('utf-8', errors='replace')
    log_text = (
        'COMMAND\n{}\n\nEXIT_CODE\n{}\n\nELAPSED_SECONDS\n{:.3f}'
        '\n\nSTDOUT\n{}\n\nSTDERR\n{}\n'
    ).format(
        subprocess.list2cmdline(command),
        result.returncode,
        elapsed,
        stdout,
        stderr,
    )
    log_path.write_text(log_text, encoding='utf-8', newline='\n')
    return result


def run_streaming_command(
    command: list[str],
    log_path: Path,
    timeout_seconds: int,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    started = time.monotonic()
    environment = os.environ.copy()
    environment['PYTHONUTF8'] = '1'
    if environment_overrides:
        environment.update(environment_overrides)

    with log_path.open('w', encoding='utf-8', newline='\n') as log_stream:
        log_stream.write(
            'COMMAND\n{}\n\nLIVE_OUTPUT\n'.format(
                subprocess.list2cmdline(command)
            )
        )
        log_stream.flush()
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        def copy_output() -> None:
            assert process.stdout is not None
            for raw_line in iter(process.stdout.readline, b''):
                line = raw_line.decode('utf-8', errors='replace')
                sys.stdout.write(line)
                sys.stdout.flush()
                log_stream.write(line)
                log_stream.flush()

        reader = threading.Thread(target=copy_output, daemon=True)
        reader.start()
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reader.join(timeout=10)
            elapsed = time.monotonic() - started
            log_stream.write(
                '\nTIMEOUT_SECONDS\n{}\n\nELAPSED_SECONDS\n{:.3f}\n'.format(
                    timeout_seconds,
                    elapsed,
                )
            )
            log_stream.flush()
            raise
        reader.join(timeout=10)
        elapsed = time.monotonic() - started
        log_stream.write(
            '\nEXIT_CODE\n{}\n\nELAPSED_SECONDS\n{:.3f}\n'.format(
                return_code,
                elapsed,
            )
        )
        log_stream.flush()
    return subprocess.CompletedProcess(
        command,
        return_code,
        stdout=b'',
        stderr=b'',
    )


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return 'not-installed'


def build_docling_command(
    executable: Path,
    source: Path,
    source_format: str,
    output_dir: Path,
    settings: dict[str, Any],
) -> list[str]:
    command = [
        str(executable),
        'convert',
        str(source),
        '--from',
        source_format,
    ]
    for output_format in settings['outputs']:
        command.extend(['--to', str(output_format)])
    command.extend(
        [
            '--pipeline',
            str(settings['pipeline']),
            '--no-ocr',
            '--tables',
            '--table-mode',
            str(settings['table_mode']),
            '--enrich-formula',
            '--enrich-picture-classes',
            '--no-enrich-picture-description',
            '--no-enrich-chart-extraction',
            '--no-enrich-code',
            '--image-export-mode',
            str(settings['image_export_mode']),
            '--no-enable-remote-services',
            '--no-allow-external-plugins',
            '--no-abort-on-error',
            '--output',
            str(output_dir),
            '--document-timeout',
            str(settings['document_timeout_seconds']),
            '--num-threads',
            str(settings['num_threads']),
            '--device',
            str(settings['device']),
            '--page-batch-size',
            str(settings['page_batch_size']),
            '--profiling',
            '--save-profiling',
            '-v',
        ]
    )
    if source_format == 'pdf':
        command.extend(
            ['--pdf-backend', str(settings['pdf_backend'])]
        )
    return command


def artifact_records(output_dir: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(output_dir.rglob('*')):
        if not path.is_file():
            continue
        records.append(
            {
                'path': path.relative_to(REPO_ROOT).as_posix(),
                'size_bytes': path.stat().st_size,
                'sha256': sha256_file(path),
            }
        )
    return records


def quality_summary(output_dir: Path, source_format: str) -> dict[str, Any]:
    markdown_files = sorted(output_dir.glob('*.md'))
    json_files = sorted(output_dir.glob('*.json'))
    html_files = sorted(output_dir.glob('*.html'))
    markdown_chars = 0
    if markdown_files:
        markdown_chars = len(
            markdown_files[0].read_text(encoding='utf-8', errors='replace')
        )
    docling_counts: dict[str, int] = {}
    if json_files:
        try:
            data = json.loads(
                json_files[0].read_text(encoding='utf-8')
            )
            for key in ('texts', 'tables', 'pictures', 'pages'):
                value = data.get(key, [])
                docling_counts[key] = (
                    len(value) if isinstance(value, (list, dict)) else 0
                )
        except (json.JSONDecodeError, OSError):
            docling_counts['json_parse_error'] = 1
    needs_ocr_review = (
        source_format == 'pdf' and markdown_chars < 200
    )
    return {
        'markdown_present': bool(markdown_files),
        'json_present': bool(json_files),
        'html_present': bool(html_files),
        'markdown_chars': markdown_chars,
        'docling_counts': docling_counts,
        'needs_ocr_review': needs_ocr_review,
    }


def normalize_json_file(
    path: Path,
    ensure_ascii: bool,
    indent: int,
) -> None:
    original = json.loads(path.read_text(encoding='utf-8'))
    normalized_text = json.dumps(
        original,
        ensure_ascii=ensure_ascii,
        indent=indent,
    ) + '\n'
    temporary_path = path.with_name(path.name + '.tmp')
    temporary_path.write_text(
        normalized_text,
        encoding='utf-8',
        newline='\n',
    )
    verified = json.loads(temporary_path.read_text(encoding='utf-8'))
    if verified != original:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(
            'JSON semantic verification failed: {}'.format(path)
        )
    os.replace(temporary_path, path)


def normalize_json_outputs(
    output_dir: Path,
    settings: dict[str, Any],
) -> list[str]:
    normalized_paths = []
    for path in sorted(output_dir.glob('*.json')):
        normalize_json_file(
            path,
            ensure_ascii=bool(settings['json_ensure_ascii']),
            indent=int(settings['json_indent']),
        )
        normalized_paths.append(path.relative_to(REPO_ROOT).as_posix())
    return normalized_paths


def write_json(path: Path, value: Any) -> None:
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    path.write_text(text + '\n', encoding='utf-8', newline='\n')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config',
        default='configs/preprocessing/sample_standard_advanced.yaml',
    )
    parser.add_argument(
        '--resume-failed',
        action='store_true',
        help='Reuse an existing run and process only failed samples.',
    )
    parser.add_argument(
        '--normalize-existing-json',
        action='store_true',
        help='Normalize all JSON outputs in an existing run and refresh hashes.',
    )
    parser.add_argument(
        '--check-only',
        action='store_true',
        help='Validate the configured run without creating files or starting tools.',
    )
    arguments = parser.parse_args()
    config_path = ensure_child(
        REPO_ROOT / arguments.config,
        REPO_ROOT,
        'config path',
    )
    config = load_config(config_path)

    input_root = (REPO_ROOT / config['input_root']).resolve()
    if input_root != EXPECTED_INPUT_ROOT:
        raise ValueError(
            'Input root must be exactly {}'.format(EXPECTED_INPUT_ROOT)
        )
    runs_root = (REPO_ROOT / config['runs_root']).resolve()
    if runs_root != EXPECTED_RUNS_ROOT:
        raise ValueError(
            'Runs root must be exactly {}'.format(EXPECTED_RUNS_ROOT)
        )
    run_root = ensure_child(
        runs_root / config['run_id'],
        runs_root,
        'run root',
    )
    reuse_existing_run = (
        arguments.resume_failed or arguments.normalize_existing_json
    )
    if run_root.exists() and not reuse_existing_run:
        raise FileExistsError(
            'Run directory already exists: {}'.format(run_root)
        )
    if not run_root.exists() and reuse_existing_run:
        raise FileNotFoundError(
            'Cannot reuse missing run directory: {}'.format(run_root)
        )

    soffice_path = Path(config['soffice_path']).resolve()
    docling_path = Path(config['docling_path']).resolve()
    if not soffice_path.is_file():
        raise FileNotFoundError(soffice_path)
    if not docling_path.is_file():
        raise FileNotFoundError(docling_path)

    if arguments.check_only:
        sample_checks = []
        for sample in config['samples']:
            source = ensure_child(
                input_root / sample['path'],
                input_root,
                'sample source',
            )
            if not source.is_file():
                raise FileNotFoundError(source)
            sample_checks.append(
                {
                    'sample_id': str(sample['id']),
                    'source_format': source.suffix.lower().lstrip('.'),
                    'source_size_bytes': source.stat().st_size,
                    'source_path': source.relative_to(REPO_ROOT).as_posix(),
                }
            )
        print(
            json.dumps(
                {
                    'check': 'passed',
                    'run_id': config['run_id'],
                    'run_root': run_root.relative_to(REPO_ROOT).as_posix(),
                    'run_root_exists': run_root.exists(),
                    'settings': config['settings'],
                    'samples': sample_checks,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    normalized_root = run_root / 'normalized_sources'
    candidate_root = run_root / 'candidate'
    log_root = run_root / 'logs'
    report_root = run_root / 'reports'
    for directory in (
        normalized_root,
        candidate_root,
        log_root,
        report_root,
    ):
        directory.mkdir(
            parents=True,
            exist_ok=reuse_existing_run,
        )

    manifest_path = run_root / 'manifest.jsonl'
    if arguments.normalize_existing_json:
        normalized_files = []
        for output_dir in sorted(candidate_root.iterdir()):
            if not output_dir.is_dir():
                continue
            normalized_files.extend(
                normalize_json_outputs(output_dir, config['settings'])
            )
        manifest_records = []
        for line in manifest_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            output_dir = candidate_root / str(record['sample_id'])
            if output_dir.is_dir():
                record['artifacts'] = artifact_records(output_dir)
                if any(output_dir.glob('*.json')):
                    quality = record.setdefault('quality', {})
                    quality['json_serialization'] = {
                        'encoding': 'utf-8',
                        'ensure_ascii': bool(
                            config['settings']['json_ensure_ascii']
                        ),
                        'indent': int(config['settings']['json_indent']),
                        'semantic_verification': 'passed',
                    }
            manifest_records.append(record)
        with manifest_path.open(
            'w', encoding='utf-8', newline='\n'
        ) as stream:
            for record in manifest_records:
                stream.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                    ) + '\n'
                )
        print(
            json.dumps(
                {
                    'run_id': config['run_id'],
                    'normalized_json_count': len(normalized_files),
                    'ensure_ascii': bool(
                        config['settings']['json_ensure_ascii']
                    ),
                    'encoding': 'utf-8',
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    config_digest = sha256_file(config_path)
    run_metadata = {
        'run_id': config['run_id'],
        'started_at': datetime.now(timezone.utc).isoformat(),
        'config_path': config_path.relative_to(REPO_ROOT).as_posix(),
        'config_sha256': config_digest,
        'input_root': input_root.relative_to(REPO_ROOT).as_posix(),
        'pipeline_version': config['pipeline_version'],
        'schema_version': config['schema_version'],
        'encoding': config['encoding'],
        'tools': {
            'python': sys.version,
            'docling': package_version('docling'),
            'docling-core': package_version('docling-core'),
            'docling-parse': package_version('docling-parse'),
            'torch': package_version('torch'),
            'torchvision': package_version('torchvision'),
        },
        'settings': config['settings'],
    }
    prior_records: dict[str, dict[str, Any]] = {}
    if arguments.resume_failed:
        for line in manifest_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            prior_record = json.loads(line)
            prior_records[str(prior_record['sample_id'])] = prior_record
        selected_samples = [
            sample
            for sample in config['samples']
            if prior_records.get(str(sample['id']), {}).get('status')
            == 'failed'
        ]
        prior_run_metadata = json.loads(
            (run_root / 'run.json').read_text(encoding='utf-8')
        )
        resume_events = prior_run_metadata.setdefault('resume_events', [])
        resume_events.append(
            {
                'started_at': run_metadata['started_at'],
                'sample_ids': [str(sample['id']) for sample in selected_samples],
                'settings': config['settings'],
            }
        )
        prior_run_metadata['settings'] = config['settings']
        write_json(run_root / 'run.json', prior_run_metadata)
    else:
        selected_samples = config['samples']
        version_log = log_root / 'libreoffice-version.log'
        version_result = run_command(
            [str(soffice_path), '--version'],
            version_log,
            60,
        )
        run_metadata['tools']['libreoffice_exit_code'] = (
            version_result.returncode
        )
        write_json(run_root / 'run.json', run_metadata)

    manifest_records: list[dict[str, Any]] = []
    for sample in selected_samples:
        sample_id = str(sample['id'])
        source = ensure_child(
            input_root / sample['path'],
            input_root,
            'sample source',
        )
        record: dict[str, Any] = {
            'sample_id': sample_id,
            'purpose': sample['purpose'],
            'source_path': source.relative_to(REPO_ROOT).as_posix(),
            'source_format': source.suffix.lower().lstrip('.'),
            'source_size_bytes': (
                source.stat().st_size if source.is_file() else None
            ),
            'source_sha256_before': None,
            'source_sha256_after': None,
            'source_unchanged': False,
            'normalized_source_path': None,
            'status': 'failed',
            'error': None,
            'artifacts': [],
            'quality': {},
        }
        try:
            if not source.is_file():
                raise FileNotFoundError(source)
            record['source_sha256_before'] = sha256_file(source)
            parse_source = source
            parse_format = record['source_format']
            if parse_format == 'doc':
                conversion_dir = normalized_root / sample_id
                conversion_dir.mkdir(parents=True, exist_ok=False)
                conversion_log = log_root / (
                    sample_id + '-libreoffice.log'
                )
                conversion_result = run_command(
                    [
                        str(soffice_path),
                        '--headless',
                        '--convert-to',
                        'docx',
                        '--outdir',
                        str(conversion_dir),
                        str(source),
                    ],
                    conversion_log,
                    300,
                )
                if conversion_result.returncode != 0:
                    raise RuntimeError(
                        'LibreOffice exit code {}'.format(
                            conversion_result.returncode
                        )
                    )
                converted = list(conversion_dir.glob('*.docx'))
                if len(converted) != 1:
                    raise RuntimeError(
                        'Expected one converted DOCX, found {}'.format(
                            len(converted)
                        )
                    )
                parse_source = converted[0]
                parse_format = 'docx'
                record['normalized_source_path'] = (
                    parse_source.relative_to(REPO_ROOT).as_posix()
                )
            elif (
                parse_format == 'pdf'
                and config['settings'].get('stage_pdf_with_short_name', False)
            ):
                staging_dir = normalized_root / sample_id
                staging_dir.mkdir(parents=True, exist_ok=False)
                short_source = staging_dir / (sample_id + '.pdf')
                shutil.copy2(source, short_source)
                if sha256_file(short_source) != record['source_sha256_before']:
                    raise RuntimeError(
                        'Staged PDF hash does not match source: {}'.format(
                            short_source
                        )
                    )
                parse_source = short_source
                record['normalized_source_path'] = (
                    parse_source.relative_to(REPO_ROOT).as_posix()
                )

            output_dir = candidate_root / sample_id
            output_dir.mkdir(
                parents=True,
                exist_ok=arguments.resume_failed,
            )
            if any(output_dir.iterdir()):
                raise RuntimeError(
                    'Resume output directory is not empty: {}'.format(
                        output_dir
                    )
                )
            docling_log = log_root / (sample_id + '-docling.log')
            command = build_docling_command(
                docling_path,
                parse_source,
                parse_format,
                output_dir,
                config['settings'],
            )
            docling_result = run_streaming_command(
                command,
                docling_log,
                int(config['settings']['document_timeout_seconds']) + 120,
                {
                    'DOCLING_INFERENCE_COMPILE_TORCH_MODELS': str(
                        config['settings']['compile_torch_models']
                    ).lower(),
                },
            )
            if docling_result.returncode != 0:
                raise RuntimeError(
                    'Docling exit code {}'.format(
                        docling_result.returncode
                    )
                )
            normalized_json_paths = normalize_json_outputs(
                output_dir,
                config['settings'],
            )
            record['artifacts'] = artifact_records(output_dir)
            record['quality'] = quality_summary(
                output_dir,
                record['source_format'],
            )
            record['quality']['json_serialization'] = {
                'encoding': 'utf-8',
                'ensure_ascii': bool(
                    config['settings']['json_ensure_ascii']
                ),
                'indent': int(config['settings']['json_indent']),
                'semantic_verification': 'passed',
                'normalized_files': normalized_json_paths,
            }
            if not record['quality']['markdown_present']:
                raise RuntimeError('Markdown artifact is missing')
            if not record['quality']['json_present']:
                raise RuntimeError('JSON artifact is missing')
            record['status'] = (
                'warning'
                if record['quality']['needs_ocr_review']
                else 'success'
            )
        except Exception as exc:
            record['error'] = '{}: {}'.format(
                type(exc).__name__,
                exc,
            )
        finally:
            if source.is_file():
                record['source_sha256_after'] = sha256_file(source)
                record['source_unchanged'] = (
                    record['source_sha256_before']
                    == record['source_sha256_after']
                )
            manifest_records.append(record)
            print(
                '{} {} {}'.format(
                    sample_id,
                    record['status'],
                    record['error'] or '',
                ),
                flush=True,
            )

    if arguments.resume_failed:
        refreshed_records = {
            str(record['sample_id']): record
            for record in manifest_records
        }
        manifest_records = [
            refreshed_records.get(
                str(sample['id']),
                prior_records[str(sample['id'])],
            )
            for sample in config['samples']
        ]
    with manifest_path.open('w', encoding='utf-8', newline='\n') as stream:
        for record in manifest_records:
            stream.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True)
                + '\n'
            )

    status_counts: dict[str, int] = {}
    for record in manifest_records:
        status = str(record['status'])
        status_counts[status] = status_counts.get(status, 0) + 1
    summary = {
        'run_id': config['run_id'],
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'sample_count': len(manifest_records),
        'status_counts': status_counts,
        'all_sources_unchanged': all(
            bool(record['source_unchanged'])
            for record in manifest_records
        ),
        'ocr_review_samples': [
            record['sample_id']
            for record in manifest_records
            if record.get('quality', {}).get('needs_ocr_review')
        ],
    }
    write_json(report_root / 'summary.json', summary)
    markdown_lines = [
        '# Sample preprocessing summary',
        '',
        '- Run: {}'.format(summary['run_id']),
        '- Samples: {}'.format(summary['sample_count']),
        '- Status: {}'.format(summary['status_counts']),
        '- Sources unchanged: {}'.format(
            summary['all_sources_unchanged']
        ),
        '- OCR review: {}'.format(
            summary['ocr_review_samples'] or 'none'
        ),
        '',
        '## Documents',
        '',
    ]
    for record in manifest_records:
        markdown_lines.append(
            '- {}: {} — {}'.format(
                record['sample_id'],
                record['status'],
                record['source_path'],
            )
        )
    (report_root / 'summary.md').write_text(
        '\n'.join(markdown_lines) + '\n',
        encoding='utf-8',
        newline='\n',
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status_counts.get('failed', 0) == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
