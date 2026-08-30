"""管理不会覆盖历史结果的知识入库运行目录和阶段清单。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from trusted_rag.domain.common import ContractModel, NonEmptyStr, Sha256
from trusted_rag.infrastructure.artifacts import canonical_digest, sha256_file, write_json_atomic
from trusted_rag.infrastructure.identifiers import create_run_id
from trusted_rag.ingestion.source_registry import InputManifest

_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|secret|password|token|credential)", re.IGNORECASE)


class StageManifestRecord(ContractModel):
    """运行清单中的单个处理阶段状态。"""

    stage: NonEmptyStr
    status: Literal["pending", "running", "completed", "failed"]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    succeeded_files: int = 0
    failed_files: int = 0
    public_error: str | None = None


class OutputArtifactRecord(ContractModel):
    """运行目录内单个输出文件的完整性记录。"""

    relative_uri: NonEmptyStr
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class RunManifest(ContractModel):
    """一轮知识入库运行的版本、输入、阶段和输出总清单。"""

    schema_version: Literal["ingestion_run_manifest.v1"] = "ingestion_run_manifest.v1"
    run_id: NonEmptyStr
    pipeline: NonEmptyStr
    config_version: NonEmptyStr
    config_sha256: Sha256
    code_version: NonEmptyStr
    tool_versions: dict[str, str] = Field(default_factory=dict)
    input_manifest_uri: NonEmptyStr
    stages: list[StageManifestRecord] = Field(default_factory=list)
    outputs: list[OutputArtifactRecord] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RunLayout:
    """一次运行中各类产物的物理目录。"""

    root: Path
    manifest_path: Path
    input_manifest_path: Path
    config_snapshot_dir: Path
    converted_dir: Path
    parsed_dir: Path
    normalized_dir: Path
    quality_dir: Path
    logs_dir: Path
    failed_dir: Path


@dataclass(frozen=True)
class FileExecutionResult[T, R]:
    """失败隔离批处理中的单项结果。"""

    item: T
    succeeded: bool
    value: R | None = None
    error_type: str | None = None
    public_error: str | None = None


class RunManager:
    """创建版本化运行目录并维护不可泄密的清单。"""

    def __init__(self, runs_root: Path) -> None:
        """初始化运行管理器。

        :param runs_root: 所有知识入库运行的持久化根目录。
        :return: 无。
        """
        self.runs_root = runs_root.resolve()

    def create_run(
        self,
        *,
        pipeline: str,
        config_version: str,
        effective_config: Mapping[str, Any],
        input_manifest: InputManifest,
        code_version: str,
        tool_versions: Mapping[str, str] | None = None,
        occurred_at: datetime | None = None,
        suffix: str | None = None,
        run_id: str | None = None,
    ) -> tuple[RunLayout, RunManifest]:
        """创建唯一运行目录并写入配置与输入快照。

        :param pipeline: 小写短横线流水线名称。
        :param config_version: 版本号，例如 ``0.01``。
        :param effective_config: 当前有效配置；敏感字段会被掩码。
        :param input_manifest: 已完成来源登记的输入清单。
        :param code_version: 当前代码提交或交付版本。
        :param tool_versions: 解析器、数据库等工具版本。
        :param occurred_at: 可注入运行时间。
        :param suffix: 可注入八位十六进制后缀。
        :param run_id: 可选的预生成运行标识，用于让来源血缘和目录一致。
        :return: 运行目录布局及初始运行清单。
        :raises FileExistsError: 生成的运行目录已经存在时抛出。
        """
        expected_prefix = f"{pipeline}-v{config_version.removeprefix('v')}-"
        if run_id is not None and not run_id.startswith(expected_prefix):
            raise ValueError("预生成 run_id 与流水线或配置版本不一致。")
        run_id = run_id or create_run_id(
            pipeline,
            config_version,
            occurred_at=occurred_at,
            suffix=suffix,
        )
        root = self.runs_root / run_id
        root.mkdir(parents=True, exist_ok=False)
        layout = _create_layout(root)
        safe_config = redact_sensitive_configuration(effective_config)
        config_path = layout.config_snapshot_dir / "effective_config.json"
        write_json_atomic(config_path, safe_config)
        write_json_atomic(layout.input_manifest_path, input_manifest)
        now = occurred_at or datetime.now(UTC)
        manifest = RunManifest(
            run_id=run_id,
            pipeline=pipeline,
            config_version=config_version,
            config_sha256=canonical_digest(safe_config),
            code_version=code_version,
            tool_versions=dict(tool_versions or {}),
            input_manifest_uri=layout.input_manifest_path.relative_to(root).as_posix(),
            created_at=now,
            updated_at=now,
        )
        write_json_atomic(layout.manifest_path, manifest)
        return layout, manifest

    def record_stage(
        self,
        layout: RunLayout,
        manifest: RunManifest,
        stage: StageManifestRecord,
        *,
        updated_at: datetime | None = None,
    ) -> RunManifest:
        """新增或替换一个阶段状态并原子更新运行清单。

        :param layout: 当前运行目录布局。
        :param manifest: 当前运行清单。
        :param stage: 最新阶段记录。
        :param updated_at: 可注入更新时间。
        :return: 更新后的不可变运行清单。
        """
        stages = [item for item in manifest.stages if item.stage != stage.stage]
        stages.append(stage)
        updated = manifest.model_copy(
            update={"stages": stages, "updated_at": updated_at or datetime.now(UTC)}
        )
        write_json_atomic(layout.manifest_path, updated)
        return updated

    def finalize_outputs(
        self,
        layout: RunLayout,
        manifest: RunManifest,
        *,
        updated_at: datetime | None = None,
    ) -> RunManifest:
        """扫描运行产物并写入大小和摘要清单。

        :param layout: 当前运行目录布局。
        :param manifest: 当前运行清单。
        :param updated_at: 可注入更新时间。
        :return: 带完整输出记录的运行清单。
        """
        excluded = {layout.manifest_path.resolve()}
        outputs = [
            OutputArtifactRecord(
                relative_uri=path.relative_to(layout.root).as_posix(),
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
            )
            for path in sorted(layout.root.rglob("*"), key=lambda item: item.as_posix())
            if path.is_file() and path.resolve() not in excluded
        ]
        updated = manifest.model_copy(
            update={"outputs": outputs, "updated_at": updated_at or datetime.now(UTC)}
        )
        write_json_atomic(layout.manifest_path, updated)
        return updated


def execute_isolated[T, R](
    items: Sequence[T],
    operation: Callable[[T], R],
    *,
    failure_message: str = "文件处理失败，请查看运行日志。",
) -> list[FileExecutionResult[T, R]]:
    """逐项执行批处理，使一个文件失败不终止其他文件。

    :param items: 待处理文件或记录序列。
    :param operation: 单项处理函数。
    :param failure_message: 可公开且不含路径的失败摘要。
    :return: 与输入顺序一致的成功或失败结果。
    """
    results: list[FileExecutionResult[T, R]] = []
    for item in items:
        try:
            results.append(FileExecutionResult(item=item, succeeded=True, value=operation(item)))
        except Exception as exc:
            results.append(
                FileExecutionResult(
                    item=item,
                    succeeded=False,
                    error_type=type(exc).__name__,
                    public_error=failure_message,
                )
            )
    return results


def redact_sensitive_configuration(payload: Mapping[str, Any]) -> dict[str, Any]:
    """递归掩码配置中的密钥、Token、密码和凭证字段。

    :param payload: 有效配置映射。
    :return: 可安全持久化的独立配置字典。
    """
    return _redact_mapping(payload)


def _redact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in payload.items():
        if _SENSITIVE_KEY.search(str(key)):
            output[str(key)] = "***REDACTED***"
        elif isinstance(value, Mapping):
            output[str(key)] = _redact_mapping(value)
        elif isinstance(value, list):
            output[str(key)] = [
                _redact_mapping(item) if isinstance(item, Mapping) else item for item in value
            ]
        else:
            output[str(key)] = value
    return output


def _create_layout(root: Path) -> RunLayout:
    directories = {
        "config_snapshot_dir": root / "config_snapshot",
        "converted_dir": root / "converted",
        "parsed_dir": root / "parsed",
        "normalized_dir": root / "normalized",
        "quality_dir": root / "quality",
        "logs_dir": root / "logs",
        "failed_dir": root / "failed",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=False)
    return RunLayout(
        root=root,
        manifest_path=root / "manifest.json",
        input_manifest_path=root / "input_manifest.json",
        **directories,
    )
