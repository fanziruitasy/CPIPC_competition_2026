"""实现异步入库任务状态机和基于 JSON 的状态仓储。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from trusted_rag.domain.enums import FileProcessingStatus, IngestionStage, JobStatus
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import SourceDocument
from trusted_rag.domain.ports import IngestionFileRecord, IngestionJobRecord
from trusted_rag.infrastructure.artifacts import read_json, write_json_atomic

_STAGE_ORDER = list(IngestionStage)
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|credential)\s*[=:]\s*[^\s,;]+"
)


class JsonIngestionJobRepository:
    """以每任务一个 JSON 文件保存当前状态。"""

    def __init__(self, root: Path) -> None:
        """初始化任务状态目录。

        :param root: 任务状态持久化目录。
        :return: 无。
        """
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, job: IngestionJobRecord) -> None:
        """原子新增或更新一条任务状态。

        :param job: 最新任务记录。
        :return: 无。
        """
        write_json_atomic(self._job_path(job.job_id), job)

    def get(self, job_id: str) -> IngestionJobRecord | None:
        """按任务标识读取当前状态。

        :param job_id: 入库任务标识。
        :return: 任务记录；不存在时返回空值。
        """
        path = self._job_path(job_id)
        if not path.is_file():
            return None
        return IngestionJobRecord.model_validate(read_json(path))

    def find_by_idempotency_key(self, idempotency_key: str) -> IngestionJobRecord | None:
        """查找具有相同幂等键的现有任务。

        :param idempotency_key: 输入与有效配置共同生成的摘要。
        :return: 首个匹配任务；不存在时返回空值。
        """
        for path in sorted(self.root.glob("*.json")):
            job = IngestionJobRecord.model_validate(read_json(path))
            if job.idempotency_key == idempotency_key:
                return job
        return None

    def _job_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"job_[0-9a-f]{24}", job_id):
            raise ValueError("job_id 不符合稳定标识规范。")
        return self.root / f"{job_id}.json"


class IngestionJobStateMachine:
    """执行任务和逐文件状态的合法转换。"""

    def create(
        self,
        sources: Sequence[SourceDocument],
        *,
        knowledge_base_id: str,
        idempotency_key: str,
        occurred_at: datetime | None = None,
    ) -> IngestionJobRecord:
        """创建处于排队状态的入库任务。

        :param sources: 已登记来源文件。
        :param knowledge_base_id: 目标知识库标识。
        :param idempotency_key: 输入与有效配置摘要。
        :param occurred_at: 可注入创建时间。
        :return: 初始任务状态。
        :raises ValueError: 来源为空或知识库不一致时抛出。
        """
        if not sources:
            raise ValueError("入库任务至少需要一个来源文件。")
        if any(source.knowledge_base_id != knowledge_base_id for source in sources):
            raise ValueError("入库任务来源必须属于同一知识库。")
        now = occurred_at or datetime.now(UTC)
        files = [
            IngestionFileRecord(
                source_id=source.source_id,
                original_file_name=source.original_file_name,
                updated_at=now,
            )
            for source in sources
        ]
        return IngestionJobRecord(
            job_id=stable_id("job", knowledge_base_id, idempotency_key),
            knowledge_base_id=knowledge_base_id,
            idempotency_key=idempotency_key,
            status=JobStatus.QUEUED,
            source_ids=[source.source_id for source in sources],
            files=files,
            created_at=now,
            updated_at=now,
        )

    def advance_file(
        self,
        job: IngestionJobRecord,
        source_id: str,
        stage: IngestionStage,
        *,
        progress_percent: int,
        occurred_at: datetime | None = None,
    ) -> IngestionJobRecord:
        """把一个文件推进到更晚阶段并重算任务进度。

        :param job: 当前任务状态。
        :param source_id: 目标来源标识。
        :param stage: 新处理阶段。
        :param progress_percent: 新文件进度。
        :param occurred_at: 可注入更新时间。
        :return: 更新后的任务状态。
        :raises ValueError: 文件不存在、已经终止或阶段倒退时抛出。
        """
        now = occurred_at or datetime.now(UTC)
        target = self._require_file(job, source_id)
        if target.status in {
            FileProcessingStatus.COMPLETED,
            FileProcessingStatus.FAILED,
            FileProcessingStatus.CANCELLED,
        }:
            raise ValueError("终态文件不能继续推进。")
        if _STAGE_ORDER.index(stage) < _STAGE_ORDER.index(target.stage):
            raise ValueError("文件处理阶段不能倒退。")
        status = (
            FileProcessingStatus.COMPLETED
            if stage is IngestionStage.COMPLETED
            else FileProcessingStatus.RUNNING
        )
        final_progress = 100 if status is FileProcessingStatus.COMPLETED else progress_percent
        updated_file = target.model_copy(
            update={
                "stage": stage,
                "status": status,
                "progress_percent": final_progress,
                "updated_at": now,
            }
        )
        return self._replace_and_summarize(job, updated_file, now)

    def fail_file(
        self,
        job: IngestionJobRecord,
        source_id: str,
        *,
        error_code: str,
        public_error: str,
        occurred_at: datetime | None = None,
    ) -> IngestionJobRecord:
        """将单个文件标记失败并保存脱敏公开摘要。

        :param job: 当前任务状态。
        :param source_id: 失败来源标识。
        :param error_code: 可供前端识别的稳定错误码。
        :param public_error: 不应包含内部路径或凭证的错误摘要。
        :param occurred_at: 可注入更新时间。
        :return: 更新后的任务状态。
        """
        now = occurred_at or datetime.now(UTC)
        target = self._require_file(job, source_id)
        if target.status is FileProcessingStatus.COMPLETED:
            raise ValueError("已完成文件不能改为失败。")
        updated_file = target.model_copy(
            update={
                "status": FileProcessingStatus.FAILED,
                "public_error_code": error_code,
                "public_error": sanitize_public_error(public_error),
                "updated_at": now,
            }
        )
        return self._replace_and_summarize(job, updated_file, now)

    @staticmethod
    def _require_file(job: IngestionJobRecord, source_id: str) -> IngestionFileRecord:
        for item in job.files:
            if item.source_id == source_id:
                return item
        raise ValueError("任务中不存在指定来源文件。")

    @staticmethod
    def _replace_and_summarize(
        job: IngestionJobRecord,
        updated_file: IngestionFileRecord,
        now: datetime,
    ) -> IngestionJobRecord:
        files = [updated_file if item.source_id == updated_file.source_id else item for item in job.files]
        progress = round(sum(item.progress_percent for item in files) / len(files))
        terminal = all(
            item.status
            in {
                FileProcessingStatus.COMPLETED,
                FileProcessingStatus.FAILED,
                FileProcessingStatus.CANCELLED,
            }
            for item in files
        )
        completed = sum(item.status is FileProcessingStatus.COMPLETED for item in files)
        if terminal and completed == len(files):
            status = JobStatus.COMPLETED
        elif terminal and completed > 0:
            status = JobStatus.PARTIALLY_COMPLETED
        elif terminal:
            status = JobStatus.FAILED
        else:
            status = JobStatus.RUNNING
        public_error = "部分文件处理失败。" if status is JobStatus.PARTIALLY_COMPLETED else None
        if status is JobStatus.FAILED:
            public_error = "全部文件处理失败。"
        return job.model_copy(
            update={
                "files": files,
                "status": status,
                "progress_percent": progress,
                "public_error": public_error,
                "updated_at": now,
            }
        )


def sanitize_public_error(message: str) -> str:
    """移除公开错误中的 Windows 路径和可能的凭证赋值。

    :param message: 原始公开错误摘要。
    :return: 最长 300 字且不含明显内部路径或凭证的摘要。
    """
    sanitized = _WINDOWS_PATH.sub("[内部路径已隐藏]", message)
    sanitized = _SECRET_ASSIGNMENT.sub(r"\1=***REDACTED***", sanitized)
    return sanitized[:300]
