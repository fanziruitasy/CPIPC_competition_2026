"""使用 LibreOffice 将已登记 DOC 转换为可追溯 DOCX。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from trusted_rag.domain.common import ContractModel, LineageMetadata, NonEmptyStr, Sha256
from trusted_rag.domain.enums import SourceFormat, SourceKind
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import SourceDocument
from trusted_rag.infrastructure.artifacts import sha256_file

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class LibreOfficeConversionError(RuntimeError):
    """LibreOffice 未生成可验证 DOCX 时抛出的转换异常。"""


class ConversionRecord(ContractModel):
    """一份 DOC 到 DOCX 转换的来源映射和完整性记录。"""

    schema_version: Literal["document_conversion.v1"] = "document_conversion.v1"
    source_id: NonEmptyStr
    converted_source: SourceDocument
    source_sha256_before: Sha256
    source_sha256_after: Sha256
    source_unchanged: bool
    output_sha256: Sha256
    output_size_bytes: int = Field(ge=1)
    output_relative_uri: NonEmptyStr
    libreoffice_version: NonEmptyStr
    export_filter: NonEmptyStr
    elapsed_seconds: float = Field(ge=0)
    created_at: datetime


class LibreOfficeDocConverter:
    """封装无 Shell 的 LibreOffice DOC 转 DOCX 调用。"""

    def __init__(
        self,
        executable: Path,
        *,
        export_filter: str = "Office Open XML Text",
        timeout_seconds: float | None = None,
        runner: CommandRunner = subprocess.run,
        producer_version: str = "0.01",
    ) -> None:
        """初始化转换器。

        :param executable: ``soffice`` 或 ``soffice.com`` 可执行文件。
        :param export_filter: LibreOffice DOCX 导出过滤器。
        :param timeout_seconds: 单文件可选超时；空值表示一直等待。
        :param runner: 可注入命令执行函数，供测试使用。
        :param producer_version: 转换器实现版本。
        :return: 无。
        :raises ValueError: 超时不是正数或空值时抛出。
        """
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是正数或空值。")
        self.executable = executable.resolve()
        self.export_filter = export_filter
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.producer_version = producer_version

    def probe_version(self) -> str:
        """读取 LibreOffice 版本并验证程序可调用。

        :return: 非空版本字符串。
        :raises LibreOfficeConversionError: 程序失败或没有返回版本时抛出。
        """
        result = self._run([str(self.executable), "--version"])
        if result.returncode != 0:
            raise LibreOfficeConversionError("LibreOffice 版本检查失败。")
        version = (result.stdout or result.stderr).strip()
        if not version:
            raise LibreOfficeConversionError("LibreOffice 版本检查没有返回内容。")
        return version

    def convert(
        self,
        source: SourceDocument,
        *,
        source_root: Path,
        converted_root: Path,
        work_root: Path,
        profile_root: Path,
        run_id: str,
        libreoffice_version: str | None = None,
        created_at: datetime | None = None,
    ) -> ConversionRecord:
        """转换一份 DOC 并验证源文件、输出文件和回溯关系。

        :param source: 已登记的原始 DOC 来源。
        :param source_root: 用于解析来源相对路径的只读根目录。
        :param converted_root: 当前运行的转换产物目录。
        :param work_root: 当前运行的临时工作目录。
        :param profile_root: 隔离的 LibreOffice 用户配置目录。
        :param run_id: 当前知识构建运行标识。
        :param libreoffice_version: 可复用的已探测版本，避免逐文件重复检查。
        :param created_at: 可注入记录时间。
        :return: 转换映射和新的 `SourceDocument`。
        :raises ValueError: 来源不是原始 DOC 或路径逃逸时抛出。
        :raises LibreOfficeConversionError: 命令失败或输出无效时抛出。
        """
        if source.source_format is not SourceFormat.DOC:
            raise ValueError("LibreOfficeDocConverter 只接受 DOC 来源。")
        if source.source_kind is SourceKind.CONVERTED:
            raise ValueError("转换产物不能再次执行 DOC 转换。")
        resolved_source_root = source_root.resolve()
        source_path = (resolved_source_root / source.relative_path).resolve()
        try:
            source_path.relative_to(resolved_source_root)
        except ValueError as exc:
            raise ValueError("DOC 来源路径逃逸了来源根目录。") from exc
        source_hash_before = sha256_file(source_path)
        if source_hash_before != source.source_sha256:
            raise LibreOfficeConversionError("DOC 来源摘要与登记记录不一致。")

        started = time.monotonic()
        source_work = work_root / source.source_id
        temporary_output = source_work / "output"
        final_output = converted_root / source.source_id
        if source_work.exists() or final_output.exists():
            raise FileExistsError("当前运行中已存在该来源的转换目录。")
        source_work.mkdir(parents=True, exist_ok=False)
        staged_doc = source_work / f"{source.source_id}.doc"
        shutil.copy2(source_path, staged_doc)
        if sha256_file(staged_doc) != source_hash_before:
            raise LibreOfficeConversionError("临时 DOC 摘要与来源不一致。")
        converted_path, _ = convert_one_doc(
            executable=self.executable,
            staged_doc=staged_doc,
            output_dir=temporary_output,
            profile_dir=profile_root,
            export_filter=self.export_filter,
            timeout_seconds=self.timeout_seconds,
            runner=self.runner,
        )
        output_hash = sha256_file(converted_path)
        output_size = converted_path.stat().st_size
        final_output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_output, final_output)
        final_docx = final_output / converted_path.name
        source_hash_after = sha256_file(source_path)
        source_unchanged = source_hash_after == source_hash_before
        if not source_unchanged:
            raise LibreOfficeConversionError("DOC 来源在转换过程中发生变化。")
        relative_uri = final_docx.relative_to(converted_root.parent).as_posix()
        converted_source_id = stable_id(
            "source",
            source.knowledge_base_id,
            output_hash,
        )
        converted_source = SourceDocument(
            source_id=converted_source_id,
            knowledge_base_id=source.knowledge_base_id,
            original_file_name=source.original_file_name,
            normalized_file_name=f"{converted_source_id.removeprefix('source_')}.docx",
            source_format=SourceFormat.DOCX,
            source_kind=SourceKind.CONVERTED,
            source_sha256=output_hash,
            file_size_bytes=output_size,
            relative_path=relative_uri,
            converted_from_source_id=source.source_id,
            lineage=LineageMetadata(
                run_id=run_id,
                producer="libreoffice_doc_converter",
                producer_version=self.producer_version,
                input_ids=[source.source_id],
                created_at=created_at or datetime.now(UTC),
            ),
        )
        return ConversionRecord(
            source_id=source.source_id,
            converted_source=converted_source,
            source_sha256_before=source_hash_before,
            source_sha256_after=source_hash_after,
            source_unchanged=source_unchanged,
            output_sha256=output_hash,
            output_size_bytes=output_size,
            output_relative_uri=relative_uri,
            libreoffice_version=libreoffice_version or self.probe_version(),
            export_filter=self.export_filter,
            elapsed_seconds=round(time.monotonic() - started, 3),
            created_at=created_at or datetime.now(UTC),
        )

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                list(arguments),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise LibreOfficeConversionError(
                f"LibreOffice 超过 timeout_seconds={self.timeout_seconds}。"
            ) from exc


def convert_one_doc(
    *,
    executable: Path,
    staged_doc: Path,
    output_dir: Path,
    profile_dir: Path,
    export_filter: str,
    timeout_seconds: float | None,
    runner: CommandRunner = subprocess.run,
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """调用一次 LibreOffice 并要求生成非空 DOCX。

    :param executable: LibreOffice 可执行文件。
    :param staged_doc: 使用短稳定文件名的临时 DOC。
    :param output_dir: 必须尚不存在的单文件输出目录。
    :param profile_dir: 隔离的 LibreOffice 用户配置目录。
    :param export_filter: DOCX 导出过滤器。
    :param timeout_seconds: 可选命令超时。
    :param runner: 可注入命令执行函数。
    :return: 生成的 DOCX 路径和命令结果。
    :raises LibreOfficeConversionError: 超时、返回码非零或输出为空时抛出。
    """
    output_dir.mkdir(parents=True, exist_ok=False)
    profile_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{staged_doc.stem}.docx"
    arguments = [
        str(executable),
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--headless",
        "--convert-to",
        f"docx:{export_filter}",
        "--outdir",
        str(output_dir),
        str(staged_doc),
    ]
    try:
        result = runner(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise LibreOfficeConversionError(
            f"LibreOffice 超过 timeout_seconds={timeout_seconds}。"
        ) from exc
    if result.returncode != 0:
        raise LibreOfficeConversionError(f"LibreOffice 转换失败，返回码 {result.returncode}。")
    if not target.is_file() or target.stat().st_size == 0:
        raise LibreOfficeConversionError("LibreOffice 未生成非空 DOCX。")
    return target, result
