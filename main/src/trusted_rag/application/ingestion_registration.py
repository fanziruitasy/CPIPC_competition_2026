"""编排来源登记、幂等任务创建和版本化运行初始化。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trusted_rag.domain.enums import SourceFormat
from trusted_rag.domain.ports import IngestionJobRecord
from trusted_rag.infrastructure.artifacts import canonical_digest
from trusted_rag.infrastructure.identifiers import create_run_id
from trusted_rag.ingestion.jobs import IngestionJobStateMachine, JsonIngestionJobRepository
from trusted_rag.ingestion.run_management import (
    RunLayout,
    RunManager,
    RunManifest,
    StageManifestRecord,
    redact_sensitive_configuration,
)
from trusted_rag.ingestion.source_registry import (
    SourceRegistrar,
    build_input_manifest,
    discover_source_files,
)


@dataclass(frozen=True)
class RegistrationOutcome:
    """公共来源登记用例的返回结果。"""

    job: IngestionJobRecord
    reused_existing_job: bool
    run_layout: RunLayout | None
    run_manifest: RunManifest | None


class IngestionRegistrationService:
    """建立三类专用处理器共同使用的入库运行起点。"""

    def __init__(self, *, runs_root: Path, jobs_root: Path) -> None:
        """初始化登记用例。

        :param runs_root: 版本化入库运行目录根路径。
        :param jobs_root: 当前任务状态目录根路径。
        :return: 无。
        """
        self.run_manager = RunManager(runs_root)
        self.job_repository = JsonIngestionJobRepository(jobs_root)
        self.state_machine = IngestionJobStateMachine()

    def register(
        self,
        *,
        source_root: Path,
        knowledge_base_id: str,
        pipeline: str,
        config_version: str,
        effective_config: Mapping[str, Any],
        code_version: str,
        recursive: bool = True,
        limit_per_format: int | None = None,
        tool_versions: Mapping[str, str] | None = None,
    ) -> RegistrationOutcome:
        """登记来源、检查幂等任务并初始化新运行。

        :param source_root: 只读来源根目录。
        :param knowledge_base_id: 目标知识库标识。
        :param pipeline: 本次知识构建流水线名称。
        :param config_version: 有效业务配置版本。
        :param effective_config: 不含或可安全脱敏的完整有效配置。
        :param code_version: 当前代码提交或交付版本。
        :param recursive: 是否递归发现来源文件。
        :param limit_per_format: 可选的每种格式样例数量；正式运行使用空值。
        :param tool_versions: 当前工具版本映射。
        :return: 新建或复用任务及相应运行信息。
        :raises ValueError: 没有发现支持文件或样例上限无效时抛出。
        """
        paths = discover_source_files(source_root, recursive=recursive)
        paths = _limit_by_format(paths, limit_per_format)
        if not paths:
            raise ValueError("来源目录中没有发现支持的文件。")
        safe_config = redact_sensitive_configuration(effective_config)
        config_sha256 = canonical_digest(safe_config)
        run_id = create_run_id(pipeline, config_version)
        registrar = SourceRegistrar(source_root)
        sources = [
            registrar.register(
                path,
                knowledge_base_id=knowledge_base_id,
                run_id=run_id,
            )
            for path in paths
        ]
        input_manifest = build_input_manifest(
            sources,
            knowledge_base_id=knowledge_base_id,
            effective_config_sha256=config_sha256,
        )
        existing = self.job_repository.find_by_idempotency_key(input_manifest.idempotency_key)
        if existing is not None:
            return RegistrationOutcome(
                job=existing,
                reused_existing_job=True,
                run_layout=None,
                run_manifest=None,
            )

        layout, manifest = self.run_manager.create_run(
            pipeline=pipeline,
            config_version=config_version,
            effective_config=safe_config,
            input_manifest=input_manifest,
            code_version=code_version,
            tool_versions=tool_versions,
            run_id=run_id,
        )
        job = self.state_machine.create(
            sources,
            knowledge_base_id=knowledge_base_id,
            idempotency_key=input_manifest.idempotency_key,
        )
        self.job_repository.save(job)
        manifest = self.run_manager.record_stage(
            layout,
            manifest,
            StageManifestRecord(
                stage="source_registration",
                status="completed",
                succeeded_files=len(sources),
            ),
        )
        manifest = self.run_manager.finalize_outputs(layout, manifest)
        return RegistrationOutcome(
            job=job,
            reused_existing_job=False,
            run_layout=layout,
            run_manifest=manifest,
        )


def _limit_by_format(paths: list[Path], limit_per_format: int | None) -> list[Path]:
    if limit_per_format is None:
        return paths
    if limit_per_format <= 0:
        raise ValueError("limit_per_format 必须是正整数或空值。")
    counts: dict[SourceFormat, int] = {}
    selected: list[Path] = []
    format_by_suffix = {f".{item.value}": item for item in SourceFormat}
    for path in paths:
        source_format = format_by_suffix[path.suffix.lower()]
        count = counts.get(source_format, 0)
        if count >= limit_per_format:
            continue
        selected.append(path)
        counts[source_format] = count + 1
    return selected
