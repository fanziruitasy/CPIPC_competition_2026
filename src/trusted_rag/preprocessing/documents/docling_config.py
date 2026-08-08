"""Versioned configuration seam for the Docling Python adapter."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, replace
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml
from docling.datamodel.pipeline_options import (
    ConvertPipelineOptions,
    PdfPipelineOptions,
)
from docling.datamodel.settings import AppSettings
from docling.document_converter import DocumentConverter
from docling_core.types.doc import DoclingDocument, ImageRefMode
from pydantic import BaseModel

from .document_inventory import (
    DocumentSource,
    describe_documents,
    discover_documents,
    sha256_file,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_INPUT_ROOT = (
    REPO_ROOT / 'Data' / 'originalData' / 'docANDpdf'
).resolve()
EXPECTED_RUNS_ROOT = (
    REPO_ROOT / 'Data' / 'staging' / 'document_preprocessing' / 'runs'
).resolve()

PipelineOptionsT = TypeVar('PipelineOptionsT', bound=BaseModel)


@dataclass(frozen=True)
class LegacyDocArtifact:
    conversion_run_id: str
    normalized_path: Path
    normalized_sha256: str
    libreoffice_version: str


@dataclass(frozen=True)
class RunPlan:
    config_path: Path
    config_digest: str
    project: dict[str, Any]
    docling: dict[str, Any]
    input_root: Path
    runs_root: Path
    run_root: Path
    sources: tuple[DocumentSource, ...]
    legacy_doc_artifacts: dict[str, LegacyDocArtifact]
    runtime_settings: AppSettings
    pdf_pipeline_options: PdfPipelineOptions | None
    word_pipeline_options: ConvertPipelineOptions | None


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


def _validate_pipeline_options(
    raw: dict[str, Any],
    model_type: type[PipelineOptionsT],
    label: str,
) -> PipelineOptionsT:
    unknown = sorted(set(raw) - set(model_type.model_fields))
    if unknown:
        raise ValueError(
            '{} contains unsupported top-level options: {}'.format(
                label,
                unknown,
            )
        )
    options = model_type.model_validate(raw)
    defaults = model_type()
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
                '{}.{} contains unsupported options: {}'.format(
                    label,
                    name,
                    unknown_nested,
                )
            )
        setattr(
            options,
            name,
            type(default_value).model_validate(raw_value),
        )
    return options


def _validate_pdf_pipeline_options(raw: dict[str, Any]) -> PdfPipelineOptions:
    return _validate_pipeline_options(
        raw,
        PdfPipelineOptions,
        'docling.pdf_pipeline_options',
    )


def _validate_word_pipeline_options(
    raw: dict[str, Any],
) -> ConvertPipelineOptions:
    return _validate_pipeline_options(
        raw,
        ConvertPipelineOptions,
        'docling.word_pipeline_options',
    )


def _source_format_counts(
    sources: tuple[DocumentSource, ...],
) -> dict[str, int]:
    counts = {'doc': 0, 'docx': 0, 'pdf': 0}
    for source in sources:
        counts[source.source_format] += 1
    counts['total'] = len(sources)
    return counts


def _load_sources(
    project: dict[str, Any],
    input_root: Path,
) -> tuple[DocumentSource, ...]:
    has_samples = 'samples' in project
    has_selection = 'source_selection' in project
    if has_samples == has_selection:
        raise ValueError(
            'project must contain exactly one of samples or source_selection'
        )

    if has_samples:
        paths = []
        configured: dict[str, dict[str, Any]] = {}
        for raw_item in project.get('samples', []):
            item = _require_mapping(raw_item, 'project.samples item')
            source_path = _ensure_child(
                input_root / str(item['path']),
                input_root,
                'sample source',
            )
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            if source_path.suffix.lower() not in {'.doc', '.docx', '.pdf'}:
                raise ValueError('unsupported sample source: {}'.format(source_path))
            relative_path = source_path.relative_to(input_root).as_posix()
            configured[relative_path] = item
            paths.append(source_path)
        if not paths:
            raise ValueError('project.samples must not be empty')
        described = describe_documents(paths, input_root)
        sources = []
        for source in described:
            item = configured[source.relative_path]
            configured_id = str(item['id'])
            if configured_id != source.doc_id:
                raise ValueError(
                    'configured id {} does not match derived id {} for {}'.format(
                        configured_id,
                        source.doc_id,
                        source.relative_path,
                    )
                )
            sources.append(
                replace(source, purpose=str(item.get('purpose', '')))
            )
        return tuple(sources)

    selection = _require_mapping(
        project['source_selection'],
        'project.source_selection',
    )
    if selection.get('mode') != 'all':
        raise ValueError('project.source_selection.mode must be all')
    sources = discover_documents(
        input_root=input_root,
        recursive=bool(selection.get('recursive', True)),
        include_extensions=tuple(selection.get('include_extensions', [])),
        exclude_name_prefixes=tuple(
            str(value)
            for value in selection.get('exclude_name_prefixes', ['~$'])
        ),
        order_by=str(selection.get('order_by', 'relative_path')),
    )
    if not sources:
        raise ValueError('no supported source documents were discovered')
    expected = _require_mapping(
        selection.get('expected_counts', {}),
        'project.source_selection.expected_counts',
    )
    actual = _source_format_counts(sources)
    normalized_expected = {
        key: int(expected.get(key, 0))
        for key in ('doc', 'docx', 'pdf', 'total')
    }
    if normalized_expected != actual:
        raise ValueError(
            'source count mismatch: expected {}, actual {}'.format(
                normalized_expected,
                actual,
            )
        )
    return sources


def _load_legacy_doc_artifacts(
    project: dict[str, Any],
    sources: tuple[DocumentSource, ...],
    runs_root: Path,
) -> dict[str, LegacyDocArtifact]:
    legacy_sources = {
        source.doc_id: source
        for source in sources
        if source.source_format == 'doc'
    }
    if not legacy_sources:
        return {}
    conversion = _require_mapping(
        project.get('legacy_doc_conversion'),
        'project.legacy_doc_conversion',
    )
    if conversion.get('require_success_manifest') is not True:
        raise ValueError(
            'project.legacy_doc_conversion.require_success_manifest must be true'
        )
    conversion_run_id = str(conversion['run_id'])
    conversion_root = _ensure_child(
        runs_root / conversion_run_id,
        runs_root,
        'legacy DOC conversion run',
    )
    manifest_path = conversion_root / 'manifest.jsonl'
    run_path = conversion_root / 'run.json'
    if not manifest_path.is_file() or not run_path.is_file():
        raise FileNotFoundError(
            'legacy DOC conversion run is incomplete: {}'.format(conversion_root)
        )
    run_metadata = json.loads(run_path.read_text(encoding='utf-8'))
    libreoffice_version = str(run_metadata.get('libreoffice_version', 'unknown'))
    records = {}
    with manifest_path.open('r', encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                record = json.loads(line)
                records[str(record['doc_id'])] = record

    artifacts = {}
    for doc_id, source in legacy_sources.items():
        record = records.get(doc_id)
        if record is None or record.get('status') != 'success':
            raise RuntimeError(
                'legacy DOC conversion is not successful for {}'.format(doc_id)
            )
        if record.get('source_sha256_before') != source.source_sha256:
            raise RuntimeError(
                'legacy DOC source hash changed since conversion: {}'.format(
                    source.source_path
                )
            )
        normalized_path = _ensure_child(
            REPO_ROOT / str(record['normalized_path']),
            conversion_root,
            'legacy normalized DOCX',
        )
        if not normalized_path.is_file():
            raise FileNotFoundError(normalized_path)
        normalized_sha256 = sha256_file(normalized_path)
        if normalized_sha256 != record.get('normalized_sha256'):
            raise RuntimeError(
                'legacy normalized DOCX hash mismatch: {}'.format(
                    normalized_path
                )
            )
        artifacts[doc_id] = LegacyDocArtifact(
            conversion_run_id=conversion_run_id,
            normalized_path=normalized_path,
            normalized_sha256=normalized_sha256,
            libreoffice_version=libreoffice_version,
        )
    return artifacts


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
    sources = _load_sources(project, input_root)
    legacy_doc_artifacts = _load_legacy_doc_artifacts(
        project,
        sources,
        runs_root,
    )

    hardware = _require_mapping(project.get('hardware'), 'project.hardware')
    cpu_threads = int(hardware['cpu_threads'])
    logical_cores = int(hardware['logical_cpu_cores'])
    if cpu_threads < 1 or cpu_threads > logical_cores:
        raise ValueError(
            'project.hardware.cpu_threads must be between 1 and logical cores'
        )
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
    converter = _require_mapping(
        docling.get('document_converter', {}),
        'docling.document_converter',
    )
    allowed_format_names = list(converter.get('allowed_formats', []))
    if len(set(allowed_format_names)) != len(allowed_format_names):
        raise ValueError('docling.document_converter.allowed_formats has duplicates')
    required_formats = {
        'pdf' if source.source_format == 'pdf' else 'docx'
        for source in sources
    }
    if set(allowed_format_names) != required_formats:
        raise ValueError(
            'allowed_formats must match parser inputs: {}'.format(
                sorted(required_formats)
            )
        )

    pdf_pipeline_options = None
    if 'pdf' in required_formats:
        pdf_format = _require_mapping(
            converter.get('pdf_format_option', {}),
            'docling.document_converter.pdf_format_option',
        )
        if pdf_format.get('pipeline_class') != 'standard':
            raise ValueError('PDF pipeline_class must be standard')
        if pdf_format.get('backend') not in {'pypdfium2', 'docling_parse'}:
            raise ValueError('unsupported PDF backend')
        pdf_pipeline_options = _validate_pdf_pipeline_options(
            _require_mapping(
                docling.get('pdf_pipeline_options', {}),
                'docling.pdf_pipeline_options',
            )
        )

    word_pipeline_options = None
    if 'docx' in required_formats:
        word_format = _require_mapping(
            converter.get('word_format_option', {}),
            'docling.document_converter.word_format_option',
        )
        if word_format.get('pipeline_class') != 'simple':
            raise ValueError('Word pipeline_class must be simple')
        if word_format.get('backend') != 'msword':
            raise ValueError('Word backend must be msword')
        word_pipeline_options = _validate_word_pipeline_options(
            _require_mapping(
                docling.get('word_pipeline_options', {}),
                'docling.word_pipeline_options',
            )
        )

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
        sources=sources,
        legacy_doc_artifacts=legacy_doc_artifacts,
        runtime_settings=runtime_settings,
        pdf_pipeline_options=pdf_pipeline_options,
        word_pipeline_options=word_pipeline_options,
    )


def docling_schema_snapshot() -> dict[str, Any]:
    return {
        'docling_version': version('docling'),
        'pdf_pipeline_options': PdfPipelineOptions.model_json_schema(),
        'word_pipeline_options': ConvertPipelineOptions.model_json_schema(),
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
    format_counts = _source_format_counts(plan.sources)
    largest = sorted(
        plan.sources,
        key=lambda source: source.size_bytes,
        reverse=True,
    )[:5]
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
        'source_counts': format_counts,
        'source_size_bytes': sum(source.size_bytes for source in plan.sources),
        'legacy_docx_count': len(plan.legacy_doc_artifacts),
        'largest_sources': [
            {
                'id': source.doc_id,
                'format': source.source_format,
                'path': source.source_path.relative_to(REPO_ROOT).as_posix(),
                'size_bytes': source.size_bytes,
            }
            for source in largest
        ],
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
    }


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
