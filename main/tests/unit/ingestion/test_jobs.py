"""验证异步入库任务状态机、逐文件状态和 JSON 仓储。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trusted_rag.domain.enums import IngestionStage, JobStatus
from trusted_rag.ingestion.jobs import IngestionJobStateMachine, JsonIngestionJobRepository
from trusted_rag.ingestion.source_registry import SourceRegistrar, discover_source_files


def test_partial_success_and_repository_round_trip(
    tmp_path: Path,
    supported_source_root: Path,
) -> None:
    """一个文件成功且一个失败时任务为部分成功并可持久化恢复。"""
    sources = _sources(supported_source_root)
    machine = IngestionJobStateMachine()
    now = datetime(2026, 8, 30, tzinfo=UTC)
    job = machine.create(
        sources,
        knowledge_base_id="kb_a",
        idempotency_key="a" * 64,
        occurred_at=now,
    )
    job = machine.advance_file(
        job,
        sources[0].source_id,
        IngestionStage.COMPLETED,
        progress_percent=100,
        occurred_at=now,
    )
    job = machine.fail_file(
        job,
        sources[1].source_id,
        error_code="document.parse_failed",
        public_error=r"解析 E:\private\373.pdf 失败，api_key=top-secret",
        occurred_at=now,
    )

    assert job.status is JobStatus.PARTIALLY_COMPLETED
    assert job.progress_percent == 50
    assert "E:\\private" not in (job.files[1].public_error or "")
    assert "top-secret" not in (job.files[1].public_error or "")

    repository = JsonIngestionJobRepository(tmp_path / "jobs")
    repository.save(job)
    assert repository.get(job.job_id) == job
    assert repository.find_by_idempotency_key("a" * 64) == job


def test_stage_cannot_move_backwards(supported_source_root: Path) -> None:
    """运行中的文件不能退回更早阶段。"""
    source = _sources(supported_source_root)[0]
    machine = IngestionJobStateMachine()
    job = machine.create([source], knowledge_base_id="kb_a", idempotency_key="b" * 64)
    job = machine.advance_file(
        job,
        source.source_id,
        IngestionStage.PARSING,
        progress_percent=40,
    )

    with pytest.raises(ValueError, match="不能倒退"):
        machine.advance_file(
            job,
            source.source_id,
            IngestionStage.VALIDATING,
            progress_percent=20,
        )


def _sources(source_root: Path):  # type: ignore[no-untyped-def]
    registrar = SourceRegistrar(source_root)
    return [
        registrar.register(path, knowledge_base_id="kb_a", run_id="run_a")
        for path in discover_source_files(source_root)[:2]
    ]
