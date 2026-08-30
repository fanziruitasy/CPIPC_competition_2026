"""验证版本化运行目录、配置快照、输出清单和失败隔离。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trusted_rag.infrastructure.artifacts import canonical_digest, read_json
from trusted_rag.ingestion.run_management import (
    RunManager,
    StageManifestRecord,
    execute_isolated,
)
from trusted_rag.ingestion.source_registry import (
    SourceRegistrar,
    build_input_manifest,
    discover_source_files,
)


def test_two_runs_never_overwrite_and_snapshot_is_redacted(
    tmp_path: Path,
    supported_source_root: Path,
) -> None:
    """相同输入的两次运行使用独立目录且配置快照不泄露密钥。"""
    input_manifest = _input_manifest(supported_source_root)
    manager = RunManager(tmp_path / "ingestion_runs")
    occurred_at = datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC)
    config = {"parser": {"version": "0.07"}, "dashscope_api_key": "real-secret"}

    first_layout, first = manager.create_run(
        pipeline="knowledge-ingestion",
        config_version="0.01",
        effective_config=config,
        input_manifest=input_manifest,
        code_version="test-commit",
        occurred_at=occurred_at,
        suffix="00000001",
    )
    second_layout, _ = manager.create_run(
        pipeline="knowledge-ingestion",
        config_version="0.01",
        effective_config=config,
        input_manifest=input_manifest,
        code_version="test-commit",
        occurred_at=occurred_at,
        suffix="00000002",
    )

    assert first_layout.root != second_layout.root
    assert first_layout.root.is_dir() and second_layout.root.is_dir()
    snapshot = read_json(first_layout.config_snapshot_dir / "effective_config.json")
    assert snapshot["dashscope_api_key"] == "***REDACTED***"
    assert "real-secret" not in first_layout.manifest_path.read_text(encoding="utf-8")

    first_layout.normalized_dir.joinpath("records.json").write_text("{}\n", encoding="utf-8")
    staged = manager.record_stage(
        first_layout,
        first,
        StageManifestRecord(stage="registration", status="completed", succeeded_files=2),
        updated_at=occurred_at,
    )
    finalized = manager.finalize_outputs(first_layout, staged, updated_at=occurred_at)
    assert any(item.relative_uri == "normalized/records.json" for item in finalized.outputs)
    assert finalized.stages[0].status == "completed"


def test_file_failure_does_not_stop_remaining_items() -> None:
    """单项异常会被隔离且后续项继续执行。"""

    def operation(value: int) -> int:
        if value == 2:
            raise RuntimeError("failure")
        return value * 10

    results = execute_isolated([1, 2, 3], operation)

    assert [result.succeeded for result in results] == [True, False, True]
    assert [result.value for result in results] == [10, None, 30]
    assert results[1].error_type == "RuntimeError"


def _input_manifest(source_root: Path):  # type: ignore[no-untyped-def]
    registrar = SourceRegistrar(source_root)
    sources = [
        registrar.register(path, knowledge_base_id="kb_a", run_id="run_a")
        for path in discover_source_files(source_root)[:2]
    ]
    return build_input_manifest(
        sources,
        knowledge_base_id="kb_a",
        effective_config_sha256=canonical_digest({"version": "0.01"}),
    )
