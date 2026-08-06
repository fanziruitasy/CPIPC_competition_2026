"""Versioned configuration seam for the Docling Python adapter."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable

import yaml
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.datamodel.settings import AppSettings
from docling.document_converter import DocumentConverter
from docling_core.types.doc import DoclingDocument, ImageRefMode
from pydantic import BaseModel


REPO_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_INPUT_ROOT = (
    REPO_ROOT / 'Data' / 'originalData' / 'docANDpdf'
).resolve()
EXPECTED_RUNS_ROOT = (
    REPO_ROOT / 'Data' / 'staging' / 'document_preprocessing' / 'runs'
).resolve()


@dataclass(frozen=True)
class SampleConfig:
    sample_id: str
    source_path: Path
    purpose: str


@dataclass(frozen=True)
class RunPlan:
    config_path: Path
    config_digest: str
    project: dict[str, Any]
    docling: dict[str, Any]
    input_root: Path
    runs_root: Path
    run_root: Path
    samples: tuple[SampleConfig, ...]
    runtime_settings: AppSettings
    pdf_pipeline_options: PdfPipelineOptions


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest().upper()


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


def _validate_callable_options(
    options: dict[str, Any],
    target: Callable[..., Any],
    label: str,
    excluded: set[str] | None = None,
) -> None:
    excluded = excluded or set()
    allowed = {
        name
        for name in inspect.signature(target).parameters
        if name not in excluded
    }
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise ValueError(
            '{} contains unsupported options: {}'.format(label, unknown)
        )


def _validate_pdf_pipeline_options(raw: dict[str, Any]) -> PdfPipelineOptions:
    unknown = sorted(set(raw) - set(PdfPipelineOptions.model_fields))
    if unknown:
        raise ValueError(
            'docling.pdf_pipeline_options contains unsupported top-level '
            'options: {}'.format(unknown)
        )
    options = PdfPipelineOptions.model_validate(raw)
    defaults = PdfPipelineOptions()
    selector_keys = {'kind'}
    for name, raw_value in raw.items():
        if not isinstance(raw_value, dict):
            continue
        current_value = getattr(options, name, None)
        default_value = getattr(defaults, name, None)
        if not isinstance(current_value, BaseModel):
            continue
        configured_keys = set(raw_value) - selector_keys
        current_fields = set(type(current_value).model_fields)
        if configured_keys <= current_fields:
            continue
        if not isinstance(default_value, BaseModel):
            raise ValueError(
                'cannot resolve nested Docling options for {}'.format(name)
            )
        default_fields = set(type(default_value).model_fields)
        if not configured_keys <= default_fields:
            unknown_nested = sorted(configured_keys - default_fields)
            raise ValueError(
                'docling.pdf_pipeline_options.{} contains unsupported '
                'options: {}'.format(name, unknown_nested)
            )
        setattr(
            options,
            name,
            type(default_value).model_validate(raw_value),
        )
    return options


def load_run_plan(config_path: Path) -> RunPlan:
    config_path = _ensure_child(config_path, REPO_ROOT, 'config path')
    with config_path.open('r', encoding='utf-8') as stream:
        root = yaml.safe_load(stream)
    root = _require_mapping(root, 'configuration root')
    if set(root) != {'project', 'docling'}:
        raise ValueError(
            'configuration root must contain only project and docling sections'
        )

    project = _require_mapping(root['project'], 'project')
    docling = _require_mapping(root['docling'], 'docling')
    if str(project.get('pipeline_version')) != '0.01':
        raise ValueError('project.pipeline_version must be 0.01')
    expected_docling = str(docling.get('expected_version'))
    installed_docling = version('docling')
    if expected_docling != installed_docling:
        raise RuntimeError(
            'Docling version mismatch: expected {}, installed {}'.format(
                expected_docling,
                installed_docling,
            )
        )
    schema_path = _ensure_child(
        REPO_ROOT / str(docling['parameter_schema']),
        REPO_ROOT / 'configs' / 'preprocessing' / 'schemas',
        'Docling parameter schema',
    )
    if not schema_path.is_file():
        raise FileNotFoundError(schema_path)
    schema_metadata = json.loads(schema_path.read_text(encoding='utf-8'))
    if str(schema_metadata.get('docling_version')) != installed_docling:
        raise RuntimeError('Docling parameter schema version mismatch')

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

    samples = []
    seen_ids = set()
    for item in project.get('samples', []):
        item = _require_mapping(item, 'project.samples item')
        sample_id = str(item['id'])
        if sample_id in seen_ids:
            raise ValueError('duplicate sample id: {}'.format(sample_id))
        seen_ids.add(sample_id)
        source_path = _ensure_child(
            input_root / str(item['path']),
            input_root,
            'sample source',
        )
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        if source_path.suffix.lower() != '.pdf':
            raise ValueError(
                'Python API v0.01 retry accepts PDF only: {}'.format(
                    source_path
                )
            )
        samples.append(
            SampleConfig(
                sample_id=sample_id,
                source_path=source_path,
                purpose=str(item.get('purpose', '')),
            )
        )
    if not samples:
        raise ValueError('project.samples must not be empty')

    hardware = _require_mapping(project.get('hardware'), 'project.hardware')
    cpu_threads = int(hardware['cpu_threads'])
    if cpu_threads < 1:
        raise ValueError('project.hardware.cpu_threads must be positive')
    if project.get('short_source_names') is not True:
        raise ValueError('project.short_source_names must be true on Windows')
    execution = _require_mapping(
        project.get('execution'),
        'project.execution',
    )
    if execution != {
        'reuse_converter': True,
        'timeout_seconds': None,
        'automatic_retries': 0,
    }:
        raise ValueError(
            'project.execution must request converter reuse, no timeout, '
            'and zero automatic retries in v0.01'
        )

    runtime_raw = _require_mapping(
        docling.get('runtime_settings', {}),
        'docling.runtime_settings',
    )
    runtime_settings = AppSettings.model_validate(runtime_raw)
    pipeline_raw = _require_mapping(
        docling.get('pdf_pipeline_options', {}),
        'docling.pdf_pipeline_options',
    )
    pdf_pipeline_options = _validate_pdf_pipeline_options(pipeline_raw)

    converter = _require_mapping(
        docling.get('document_converter', {}),
        'docling.document_converter',
    )
    if converter.get('allowed_formats') != ['pdf']:
        raise ValueError(
            'docling.document_converter.allowed_formats must be [pdf] in v0.01'
        )
    pdf_format = _require_mapping(
        converter.get('pdf_format_option', {}),
        'docling.document_converter.pdf_format_option',
    )
    if pdf_format.get('pipeline_class') != 'standard':
        raise ValueError('only the standard PDF pipeline is supported in v0.01')
    if pdf_format.get('backend') not in {'pypdfium2', 'docling_parse'}:
        raise ValueError('unsupported PDF backend')

    convert_options = _require_mapping(
        docling.get('convert_options', {}),
        'docling.convert_options',
    )
    _validate_callable_options(
        convert_options,
        DocumentConverter.convert,
        'docling.convert_options',
        {'self', 'source'},
    )

    export = _require_mapping(docling.get('export', {}), 'docling.export')
    ImageRefMode(str(export['image_mode']))
    _validate_callable_options(
        _require_mapping(export.get('markdown', {}), 'docling.export.markdown'),
        DoclingDocument.save_as_markdown,
        'docling.export.markdown',
        {'self', 'filename', 'artifacts_dir', 'image_mode'},
    )
    _validate_callable_options(
        _require_mapping(export.get('json', {}), 'docling.export.json'),
        DoclingDocument.save_as_json,
        'docling.export.json',
        {'self', 'filename', 'artifacts_dir', 'image_mode'},
    )
    _validate_callable_options(
        _require_mapping(export.get('html', {}), 'docling.export.html'),
        DoclingDocument.save_as_html,
        'docling.export.html',
        {'self', 'filename', 'artifacts_dir', 'image_mode'},
    )

    return RunPlan(
        config_path=config_path,
        config_digest=sha256_file(config_path),
        project=project,
        docling=docling,
        input_root=input_root,
        runs_root=runs_root,
        run_root=run_root,
        samples=tuple(samples),
        runtime_settings=runtime_settings,
        pdf_pipeline_options=pdf_pipeline_options,
    )


def docling_schema_snapshot() -> dict[str, Any]:
    return {
        'docling_version': version('docling'),
        'pdf_pipeline_options': PdfPipelineOptions.model_json_schema(),
        'convert_options': list(
            inspect.signature(DocumentConverter.convert).parameters
        ),
        'export_options': {
            'markdown': list(
                inspect.signature(DoclingDocument.save_as_markdown).parameters
            ),
            'json': list(
                inspect.signature(DoclingDocument.save_as_json).parameters
            ),
            'html': list(
                inspect.signature(DoclingDocument.save_as_html).parameters
            ),
        },
    }


def plan_summary(plan: RunPlan) -> dict[str, Any]:
    return {
        'check': 'passed',
        'pipeline_version': plan.project['pipeline_version'],
        'config_version': plan.project['config_version'],
        'config_sha256': plan.config_digest,
        'run_id': plan.project['run_id'],
        'run_root': plan.run_root.relative_to(REPO_ROOT).as_posix(),
        'run_root_exists': plan.run_root.exists(),
        'hardware': plan.project['hardware'],
        'docling_version': version('docling'),
        'docling_runtime_settings': plan.runtime_settings.model_dump(mode='json'),
        'pdf_pipeline_options': plan.pdf_pipeline_options.model_dump(
            mode='json', serialize_as_any=True
        ),
        'samples': [
            {
                'id': sample.sample_id,
                'path': sample.source_path.relative_to(REPO_ROOT).as_posix(),
                'size_bytes': sample.source_path.stat().st_size,
                'purpose': sample.purpose,
            }
            for sample in plan.samples
        ],
    }


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
