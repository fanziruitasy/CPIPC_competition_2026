"""验证公共来源登记用例的运行初始化和幂等复用。"""

from __future__ import annotations

import json
from pathlib import Path

from trusted_rag.application.ingestion_registration import (
    IngestionRegistrationService,
    RegistrationOutcome,
)


def test_registration_creates_run_then_reuses_same_job(
    tmp_path: Path,
    supported_source_root: Path,
) -> None:
    """相同输入与配置第二次登记时复用原任务且不创建新运行。"""
    service = IngestionRegistrationService(
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
    )
    first = _register(service, supported_source_root)
    second = _register(service, supported_source_root)

    assert first.reused_existing_job is False
    assert first.run_layout is not None
    assert first.run_manifest is not None
    input_manifest = json.loads(first.run_layout.input_manifest_path.read_text(encoding="utf-8"))
    assert all(
        source["lineage"]["run_id"] == first.run_manifest.run_id
        for source in input_manifest["sources"]
    )
    assert second.reused_existing_job is True
    assert second.job.job_id == first.job.job_id
    assert second.run_layout is None
    assert len(list((tmp_path / "runs").iterdir())) == 1


def _register(
    service: IngestionRegistrationService,
    source_root: Path,
) -> RegistrationOutcome:
    return service.register(
        source_root=source_root,
        knowledge_base_id="kb_a",
        pipeline="knowledge-ingestion",
        config_version="0.01",
        effective_config={"version": "0.01", "api_key": "secret"},
        code_version="test-commit",
        limit_per_format=1,
    )
