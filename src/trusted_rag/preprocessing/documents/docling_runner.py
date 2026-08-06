"""Docling Python adapter and two-document v0.01 orchestration."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from importlib.metadata import version
from pathlib import Path
from typing import Any

from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.backend_options import PdfBackendOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.settings import settings as docling_settings
from docling.document_converter import DocumentConverter, PdfFormatOption
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
    sha256_file,
)


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
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=plan.pdf_pipeline_options,
                pipeline_cls=StandardPdfPipeline,
                backend=backend_by_name[backend_name],
                backend_options=backend_options,
            )
        },
    )


def _normalize_json(path: Path, plan: RunPlan) -> None:
    serialization = plan.project['json_serialization']
    original = json.loads(path.read_text(encoding='utf-8'))
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


def _artifact_records(output_dir: Path) -> list[dict[str, Any]]:
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


def _export_document(
    document: Any,
    sample_id: str,
    temporary_dir: Path,
    plan: RunPlan,
) -> None:
    export = plan.docling['export']
    image_mode = ImageRefMode(str(export['image_mode']))
    assets_dir = temporary_dir / (sample_id + '_artifacts')
    outputs = set(plan.project['outputs'])
    document.name = sample_id

    if 'md' in outputs:
        document.save_as_markdown(
            filename=temporary_dir / (sample_id + '.md'),
            artifacts_dir=assets_dir,
            image_mode=image_mode,
            **export.get('markdown', {}),
        )
    if 'json' in outputs:
        json_path = temporary_dir / (sample_id + '.json')
        document.save_as_json(
            filename=json_path,
            artifacts_dir=assets_dir,
            image_mode=image_mode,
            **export.get('json', {}),
        )
        _normalize_json(json_path, plan)
    if 'html' in outputs:
        document.save_as_html(
            filename=temporary_dir / (sample_id + '.html'),
            artifacts_dir=assets_dir,
            image_mode=image_mode,
            **export.get('html', {}),
        )


def _prepare_short_source(
    sample_id: str,
    source: Path,
    normalized_root: Path,
    source_hash: str,
) -> Path:
    sample_root = normalized_root / sample_id
    sample_root.mkdir(parents=True, exist_ok=False)
    staged_source = sample_root / (sample_id + source.suffix.lower())
    shutil.copy2(source, staged_source)
    if sha256_file(staged_source) != source_hash:
        raise RuntimeError(
            'staged source hash does not match original: {}'.format(source)
        )
    return staged_source


def check_config(config_path: Path) -> dict[str, Any]:
    plan = load_run_plan(config_path)
    summary = plan_summary(plan)
    summary['hardware_probe'] = _apply_hardware_settings(plan)
    summary['docling_schema_fields'] = len(
        docling_schema_snapshot()['pdf_pipeline_options']['properties']
    )
    return summary


def run_documents(config_path: Path) -> int:
    plan = load_run_plan(config_path)
    if plan.run_root.exists():
        raise FileExistsError(
            'run directory already exists; choose a new project.run_id: {}'.format(
                plan.run_root
            )
        )

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
    LOG.info('Creating one reusable DocumentConverter for %d PDFs', len(plan.samples))

    shutil.copy2(plan.config_path, reports_root / plan.config_path.name)
    _write_json(reports_root / 'docling-schema.json', docling_schema_snapshot())
    _write_json(
        reports_root / 'resolved-docling-options.json',
        plan.pdf_pipeline_options.model_dump(
            mode='json', serialize_as_any=True
        ),
    )
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
            'hardware': hardware,
            'docling_version': version('docling'),
            'execution_policy': {
                'python_api': True,
                'reuse_converter': True,
                'timeout': None,
                'automatic_retry': False,
            },
        },
    )

    converter = _build_converter(plan)
    records = []
    for sample in plan.samples:
        started = time.monotonic()
        source_hash_before = sha256_file(sample.source_path)
        record: dict[str, Any] = {
            'sample_id': sample.sample_id,
            'purpose': sample.purpose,
            'source_path': sample.source_path.relative_to(REPO_ROOT).as_posix(),
            'source_sha256_before': source_hash_before,
            'source_sha256_after': None,
            'source_unchanged': False,
            'normalized_source_path': None,
            'status': 'failed',
            'docling_status': None,
            'errors': [],
            'artifacts': [],
            'elapsed_seconds': None,
        }
        LOG.info('[%s] staging source with a short filename', sample.sample_id)
        try:
            staged_source = _prepare_short_source(
                sample.sample_id,
                sample.source_path,
                normalized_root,
                source_hash_before,
            )
            record['normalized_source_path'] = (
                staged_source.relative_to(REPO_ROOT).as_posix()
            )
            LOG.info('[%s] starting Docling Python conversion', sample.sample_id)
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

            temporary_dir = candidate_root / ('.' + sample.sample_id + '.tmp')
            final_dir = candidate_root / sample.sample_id
            temporary_dir.mkdir(parents=True, exist_ok=False)
            _export_document(
                result.document,
                sample.sample_id,
                temporary_dir,
                plan,
            )
            os.replace(temporary_dir, final_dir)
            record['artifacts'] = _artifact_records(final_dir)
            record['status'] = (
                'success'
                if result.status == ConversionStatus.SUCCESS
                else 'warning'
            )
            LOG.info('[%s] export complete', sample.sample_id)
        except Exception as exc:
            record['errors'].append(
                {'type': type(exc).__name__, 'message': str(exc)}
            )
            LOG.exception('[%s] processing failed', sample.sample_id)
        finally:
            record['source_sha256_after'] = sha256_file(sample.source_path)
            record['source_unchanged'] = (
                record['source_sha256_before']
                == record['source_sha256_after']
            )
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            records.append(record)
            LOG.info(
                '[%s] terminal status=%s elapsed=%.3fs',
                sample.sample_id,
                record['status'],
                record['elapsed_seconds'],
            )

    manifest_path = plan.run_root / 'manifest.jsonl'
    with manifest_path.open('w', encoding='utf-8', newline='\n') as stream:
        for record in records:
            stream.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n'
            )
    status_counts: dict[str, int] = {}
    for record in records:
        status = str(record['status'])
        status_counts[status] = status_counts.get(status, 0) + 1
    summary = {
        'run_id': plan.project['run_id'],
        'pipeline_version': plan.project['pipeline_version'],
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'sample_count': len(records),
        'status_counts': status_counts,
        'all_sources_unchanged': all(
            bool(record['source_unchanged']) for record in records
        ),
    }
    _write_json(reports_root / 'summary.json', summary)
    print(dumps_json(summary))
    return 0 if status_counts.get('failed', 0) == 0 else 2
