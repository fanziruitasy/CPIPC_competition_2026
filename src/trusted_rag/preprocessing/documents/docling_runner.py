"""Docling Python adapter and full-corpus v0.01 orchestration."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
from docling.backend.msword_backend import MsWordDocumentBackend
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.backend_options import PdfBackendOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.settings import settings as docling_settings
from docling.document_converter import (
    DocumentConverter,
    PdfFormatOption,
    WordFormatOption,
)
from docling.pipeline.simple_pipeline import SimplePipeline
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
from docling_core.types.doc import ImageRefMode
from pydantic import BaseModel

from .docling_config import (
    REPO_ROOT,
    RunPlan,
    docling_schema_snapshot,
    dumps_json,
    load_run_plan,
    plan_summary,
)
from .document_inventory import DocumentSource, sha256_file


LOG = logging.getLogger(__name__)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n',
        encoding='utf-8',
        newline='\n',
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json', serialize_as_any=True)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _configure_logging(log_path: Path) -> None:
    formatter = logging.Formatter(
        '%(asctime)s\t%(levelname)s\t%(name)s\t%(message)s'
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding='utf-8')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(console)
    root_logger.addHandler(file_handler)


def _apply_hardware_settings(plan: RunPlan) -> dict[str, Any]:
    hardware = plan.project['hardware']
    cpu_threads = int(hardware['cpu_threads'])
    interop_threads = int(hardware.get('torch_interop_threads', 2))
    os.environ['OMP_NUM_THREADS'] = str(cpu_threads)
    os.environ['MKL_NUM_THREADS'] = str(cpu_threads)
    os.environ['NUMEXPR_NUM_THREADS'] = str(cpu_threads)

    import torch

    torch.set_num_threads(cpu_threads)
    try:
        torch.set_num_interop_threads(interop_threads)
    except RuntimeError:
        pass
    cuda_available = torch.cuda.is_available()
    if bool(hardware.get('cuda_required', False)) and not cuda_available:
        raise RuntimeError('CUDA is required by project.hardware.cuda_required')
    return {
        'logical_cpu_count': os.cpu_count(),
        'configured_cpu_threads': cpu_threads,
        'torch_interop_threads': interop_threads,
        'cuda_available': cuda_available,
        'cuda_device': (
            torch.cuda.get_device_name(0) if cuda_available else None
        ),
        'cuda_device_count': torch.cuda.device_count(),
        'cuda_memory_allocated_bytes': (
            torch.cuda.memory_allocated(0) if cuda_available else 0
        ),
        'torch_version': torch.__version__,
    }


def _apply_docling_runtime(plan: RunPlan) -> None:
    runtime = plan.runtime_settings
    docling_settings.perf = runtime.perf.model_copy(deep=True)
    docling_settings.debug = runtime.debug.model_copy(deep=True)
    docling_settings.inference = runtime.inference.model_copy(deep=True)
    docling_settings.cache_dir = runtime.cache_dir
    docling_settings.artifacts_path = runtime.artifacts_path


def _build_converter(plan: RunPlan) -> DocumentConverter:
    converter_config = plan.docling['document_converter']
    allowed_format_names = list(converter_config['allowed_formats'])
    allowed_formats = [InputFormat(value) for value in allowed_format_names]
    format_options: dict[InputFormat, Any] = {}

    if InputFormat.PDF in allowed_formats:
        pdf_config = converter_config['pdf_format_option']
        backend_name = str(pdf_config['backend'])
        backend_by_name = {
            'pypdfium2': PyPdfiumDocumentBackend,
            'docling_parse': DoclingParseDocumentBackend,
        }
        backend_options_raw = pdf_config.get('backend_options')
        backend_options = (
            PdfBackendOptions.model_validate(backend_options_raw)
            if backend_options_raw is not None
            else None
        )
        format_options[InputFormat.PDF] = PdfFormatOption(
            pipeline_options=plan.pdf_pipeline_options,
            pipeline_cls=StandardPdfPipeline,
            backend=backend_by_name[backend_name],
            backend_options=backend_options,
        )

    if InputFormat.DOCX in allowed_formats:
        format_options[InputFormat.DOCX] = WordFormatOption(
            pipeline_options=plan.word_pipeline_options,
            pipeline_cls=SimplePipeline,
            backend=MsWordDocumentBackend,
            backend_options=None,
        )

    return DocumentConverter(
        allowed_formats=allowed_formats,
        format_options=format_options,
    )


def _rewrite_local_artifact_uris(
    value: Any,
    temporary_dir: Path,
) -> Any:
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            if key == 'uri' and isinstance(item, str):
                candidate = Path(item)
                if candidate.is_absolute():
                    try:
                        item = candidate.resolve().relative_to(
                            temporary_dir.resolve()
                        ).as_posix()
                    except ValueError:
                        pass
            rewritten[key] = _rewrite_local_artifact_uris(item, temporary_dir)
        return rewritten
    if isinstance(value, list):
        return [
            _rewrite_local_artifact_uris(item, temporary_dir)
            for item in value
        ]
    return value


def _normalize_json(
    path: Path,
    plan: RunPlan,
    temporary_dir: Path,
) -> None:
    serialization = plan.project['json_serialization']
    original = json.loads(path.read_text(encoding='utf-8'))
    original = _rewrite_local_artifact_uris(original, temporary_dir)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(
        json.dumps(
            original,
            ensure_ascii=bool(serialization['ensure_ascii']),
            indent=int(serialization['indent']),
        ) + '\n',
        encoding='utf-8',
        newline='\n',
    )
    verified = json.loads(temporary.read_text(encoding='utf-8'))
    if verified != original:
        temporary.unlink(missing_ok=True)
        raise RuntimeError('JSON semantic verification failed: {}'.format(path))
    os.replace(temporary, path)


def _normalize_text_artifact_references(path: Path, temporary_dir: Path) -> None:
    """Make Docling's referenced image paths portable before atomic publish."""
    text = path.read_text(encoding='utf-8')
    rewritten = text
    for prefix in (
        str(temporary_dir) + '\\',
        temporary_dir.as_posix() + '/',
    ):
        rewritten = rewritten.replace(prefix, '')
    if path.suffix.lower() == '.html':
        image_source_pattern = re.compile(
            r'(?i)(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])'
        )

        def rewrite_image_source(match: re.Match[str]) -> str:
            decoded = unquote(match.group(2))
            candidate = Path(decoded)
            if candidate.is_absolute():
                try:
                    decoded = candidate.resolve().relative_to(
                        temporary_dir.resolve()
                    ).as_posix()
                except ValueError:
                    pass
            return match.group(1) + decoded + match.group(3)

        rewritten = image_source_pattern.sub(rewrite_image_source, rewritten)
    if rewritten != text:
        path.write_text(rewritten, encoding='utf-8', newline='\n')


def _artifact_records(
    temporary_dir: Path,
    published_dir: Path,
) -> list[dict[str, Any]]:
    records = []
    for path in sorted(temporary_dir.rglob('*')):
        if not path.is_file():
            continue
        relative_path = path.relative_to(temporary_dir)
        records.append(
            {
                'path': (
                    published_dir / relative_path
                ).relative_to(REPO_ROOT).as_posix(),
                'size_bytes': path.stat().st_size,
                'sha256': sha256_file(path),
            }
        )
    return records


def _export_document(
    document: Any,
    doc_id: str,
    temporary_dir: Path,
    plan: RunPlan,
) -> None:
    export = plan.docling['export']
    image_mode = ImageRefMode(str(export['image_mode']))
    assets_dir = temporary_dir / (doc_id + '_artifacts')
    outputs = set(plan.project['outputs'])
    document.name = doc_id

    if 'md' in outputs:
        document.save_as_markdown(
            filename=temporary_dir / (doc_id + '.md'),
            artifacts_dir=assets_dir,
            image_mode=image_mode,
            **export.get('markdown', {}),
        )
    if 'json' in outputs:
        json_path = temporary_dir / (doc_id + '.json')
        document.save_as_json(
            filename=json_path,
            artifacts_dir=assets_dir,
            image_mode=image_mode,
            **export.get('json', {}),
        )
        _normalize_json(json_path, plan, temporary_dir)
    if 'html' in outputs:
        document.save_as_html(
            filename=temporary_dir / (doc_id + '.html'),
            artifacts_dir=assets_dir,
            image_mode=image_mode,
            **export.get('html', {}),
        )
    for suffix in ('.md', '.html'):
        path = temporary_dir / (doc_id + suffix)
        if path.is_file():
            _normalize_text_artifact_references(path, temporary_dir)


def _stage_source(
    source: DocumentSource,
    plan: RunPlan,
    normalized_root: Path,
) -> tuple[Path, dict[str, Any] | None]:
    source_root = normalized_root / source.doc_id
    source_root.mkdir(parents=True, exist_ok=False)
    conversion_chain = None

    if source.source_format == 'doc':
        artifact = plan.legacy_doc_artifacts[source.doc_id]
        parser_source = artifact.normalized_path
        expected_hash = artifact.normalized_sha256
        suffix = '.docx'
        conversion_chain = {
            'from': 'doc',
            'to': 'docx',
            'conversion_run_id': artifact.conversion_run_id,
            'libreoffice_version': artifact.libreoffice_version,
            'normalized_sha256': artifact.normalized_sha256,
        }
    else:
        parser_source = source.source_path
        expected_hash = source.source_sha256
        suffix = source.source_path.suffix.lower()

    staged_source = source_root / (source.doc_id + suffix)
    shutil.copy2(parser_source, staged_source)
    if sha256_file(staged_source) != expected_hash:
        raise RuntimeError(
            'staged parser input hash does not match source: {}'.format(
                parser_source
            )
        )
    return staged_source, conversion_chain


def _source_inventory_record(source: DocumentSource) -> dict[str, Any]:
    return {
        'doc_id': source.doc_id,
        'source_id': source.source_id,
        'source_path': source.source_path.relative_to(REPO_ROOT).as_posix(),
        'source_format': source.source_format,
        'parser_input_format': (
            'pdf' if source.source_format == 'pdf' else 'docx'
        ),
        'source_sha256': source.source_sha256,
        'size_bytes': source.size_bytes,
        'modified_at': source.modified_at,
        'purpose': source.purpose,
    }


def _append_manifest(path: Path, record: dict[str, Any]) -> None:
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        stream.write('\n')
        stream.flush()


def check_config(config_path: Path) -> dict[str, Any]:
    plan = load_run_plan(config_path)
    summary = plan_summary(plan)
    summary['hardware_probe'] = _apply_hardware_settings(plan)
    schema = docling_schema_snapshot()
    summary['docling_schema_fields'] = {
        'pdf': len(schema['pdf_pipeline_options']['properties']),
        'word': len(schema['word_pipeline_options']['properties']),
    }
    return summary


def run_documents(config_path: Path) -> int:
    plan = load_run_plan(config_path)
    if plan.run_root.exists():
        raise FileExistsError(
            'run directory already exists; choose a new project.run_id: {}'.format(
                plan.run_root
            )
        )

    run_started = time.monotonic()
    normalized_root = plan.run_root / 'normalized_sources'
    candidate_root = plan.run_root / 'candidate'
    logs_root = plan.run_root / 'logs'
    reports_root = plan.run_root / 'reports'
    for directory in (
        normalized_root,
        candidate_root,
        logs_root,
        reports_root,
    ):
        directory.mkdir(parents=True, exist_ok=False)

    _configure_logging(logs_root / 'run.log')
    hardware = _apply_hardware_settings(plan)
    _apply_docling_runtime(plan)
    LOG.info('Hardware: %s', dumps_json(hardware))
    LOG.info(
        'Creating one reusable DocumentConverter for %d sources',
        len(plan.sources),
    )

    shutil.copy2(plan.config_path, reports_root / plan.config_path.name)
    _write_json(reports_root / 'docling-schema.json', docling_schema_snapshot())
    _write_json(
        reports_root / 'resolved-docling-options.json',
        {
            'runtime_settings': plan.runtime_settings.model_dump(mode='json'),
            'pdf_pipeline_options': (
                plan.pdf_pipeline_options.model_dump(
                    mode='json',
                    serialize_as_any=True,
                )
                if plan.pdf_pipeline_options is not None
                else None
            ),
            'word_pipeline_options': (
                plan.word_pipeline_options.model_dump(
                    mode='json',
                    serialize_as_any=True,
                )
                if plan.word_pipeline_options is not None
                else None
            ),
        },
    )
    inventory_path = plan.run_root / 'inventory.jsonl'
    with inventory_path.open('w', encoding='utf-8', newline='\n') as stream:
        for source in plan.sources:
            stream.write(
                json.dumps(
                    _source_inventory_record(source),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            stream.write('\n')
    _write_json(
        plan.run_root / 'run.json',
        {
            'run_id': plan.project['run_id'],
            'pipeline_version': plan.project['pipeline_version'],
            'config_version': plan.project['config_version'],
            'description': plan.project['description'],
            'config_path': plan.config_path.relative_to(REPO_ROOT).as_posix(),
            'config_sha256': plan.config_digest,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'source_count': len(plan.sources),
            'hardware': hardware,
            'docling_version': version('docling'),
            'execution_policy': {
                'python_api': True,
                'reuse_converter': True,
                'timeout': None,
                'automatic_retry': False,
                'failure_isolation': 'per-document',
            },
        },
    )

    converter = _build_converter(plan)
    manifest_path = plan.run_root / 'manifest.jsonl'
    manifest_path.touch()
    records = []
    for index, source in enumerate(plan.sources, start=1):
        started = time.monotonic()
        temporary_dir = candidate_root / ('.' + source.doc_id + '.tmp')
        record: dict[str, Any] = {
            **_source_inventory_record(source),
            'source_sha256_before': source.source_sha256,
            'source_sha256_after': None,
            'source_unchanged': False,
            'normalized_source_path': None,
            'normalized_source_sha256': None,
            'conversion_chain': None,
            'status': 'failed',
            'docling_status': None,
            'errors': [],
            'artifacts': [],
            'elapsed_seconds': None,
        }
        LOG.info(
            '[%03d/%03d][%s][%s] staging parser input',
            index,
            len(plan.sources),
            source.doc_id,
            source.source_format,
        )
        try:
            staged_source, conversion_chain = _stage_source(
                source,
                plan,
                normalized_root,
            )
            record['normalized_source_path'] = (
                staged_source.relative_to(REPO_ROOT).as_posix()
            )
            record['normalized_source_sha256'] = sha256_file(staged_source)
            record['conversion_chain'] = conversion_chain
            LOG.info(
                '[%03d/%03d][%s][%s] starting Docling Python conversion',
                index,
                len(plan.sources),
                source.doc_id,
                source.source_format,
            )
            result = converter.convert(
                source=staged_source,
                **plan.docling.get('convert_options', {}),
            )
            record['docling_status'] = result.status.value
            record['errors'] = _jsonable(result.errors)
            if result.status not in {
                ConversionStatus.SUCCESS,
                ConversionStatus.PARTIAL_SUCCESS,
            }:
                raise RuntimeError(
                    'Docling conversion status: {}'.format(result.status.value)
                )

            final_dir = candidate_root / source.doc_id
            temporary_dir.mkdir(parents=True, exist_ok=False)
            _export_document(
                result.document,
                source.doc_id,
                temporary_dir,
                plan,
            )
            artifact_records = _artifact_records(temporary_dir, final_dir)
            os.replace(temporary_dir, final_dir)
            record['artifacts'] = artifact_records
            record['status'] = (
                'success'
                if result.status == ConversionStatus.SUCCESS
                else 'warning'
            )
            LOG.info(
                '[%03d/%03d][%s][%s] export complete',
                index,
                len(plan.sources),
                source.doc_id,
                source.source_format,
            )
        except Exception as exc:
            record['errors'].append(
                {'type': type(exc).__name__, 'message': str(exc)}
            )
            LOG.exception(
                '[%03d/%03d][%s][%s] processing failed',
                index,
                len(plan.sources),
                source.doc_id,
                source.source_format,
            )
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir)
        finally:
            try:
                record['source_sha256_after'] = sha256_file(source.source_path)
                record['source_unchanged'] = (
                    record['source_sha256_before']
                    == record['source_sha256_after']
                )
                if not record['source_unchanged']:
                    record['status'] = 'failed'
                    record['errors'].append(
                        {
                            'type': 'SourceMutationError',
                            'message': 'source SHA-256 changed during parsing',
                        }
                    )
            except Exception as exc:
                record['status'] = 'failed'
                record['errors'].append(
                    {'type': type(exc).__name__, 'message': str(exc)}
                )
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            records.append(record)
            _append_manifest(manifest_path, record)
            LOG.info(
                '[%03d/%03d][%s][%s] terminal status=%s elapsed=%.3fs',
                index,
                len(plan.sources),
                source.doc_id,
                source.source_format,
                record['status'],
                record['elapsed_seconds'],
            )

    status_counts: dict[str, int] = {}
    format_counts: dict[str, dict[str, int]] = {}
    for record in records:
        status = str(record['status'])
        source_format = str(record['source_format'])
        status_counts[status] = status_counts.get(status, 0) + 1
        per_format = format_counts.setdefault(source_format, {})
        per_format[status] = per_format.get(status, 0) + 1
    summary = {
        'run_id': plan.project['run_id'],
        'pipeline_version': plan.project['pipeline_version'],
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'source_count': len(records),
        'status_counts': status_counts,
        'status_by_source_format': format_counts,
        'artifact_file_count': sum(
            len(record['artifacts']) for record in records
        ),
        'all_sources_unchanged': all(
            bool(record['source_unchanged']) for record in records
        ),
        'elapsed_seconds': round(time.monotonic() - run_started, 3),
    }
    _write_json(reports_root / 'summary.json', summary)
    print(dumps_json(summary))
    return 0 if status_counts.get('failed', 0) == 0 else 2
