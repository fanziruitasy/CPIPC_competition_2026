"""登记不可变来源文件并生成稳定输入清单。"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from trusted_rag.domain.common import ContractModel, LineageMetadata, NonEmptyStr, Sha256
from trusted_rag.domain.enums import SourceFormat, SourceKind
from trusted_rag.domain.identity import canonical_sha256, stable_id
from trusted_rag.domain.knowledge import SourceDocument
from trusted_rag.infrastructure.artifacts import sha256_file

_FORMAT_BY_SUFFIX = {
    ".doc": SourceFormat.DOC,
    ".docx": SourceFormat.DOCX,
    ".pdf": SourceFormat.PDF,
    ".xls": SourceFormat.XLS,
    ".xlsx": SourceFormat.XLSX,
}
_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


class InputManifest(ContractModel):
    """一次知识构建所登记的只读输入及其幂等信息。"""

    schema_version: Literal["input_manifest.v1"] = "input_manifest.v1"
    manifest_id: NonEmptyStr
    knowledge_base_id: NonEmptyStr
    idempotency_key: Sha256
    effective_config_sha256: Sha256
    sources: list[SourceDocument] = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        """确保清单时间包含时区。

        :param value: 清单创建时间。
        :return: 通过校验的时间。
        :raises ValueError: 时间不含时区时抛出。
        """
        if value.tzinfo is None:
            raise ValueError("created_at 必须包含时区。")
        return value


def discover_source_files(root: Path, *, recursive: bool = True) -> list[Path]:
    """按稳定顺序发现支持的 Word、PDF 和 Excel 文件。

    :param root: 只读来源根目录。
    :param recursive: 是否递归扫描子目录。
    :return: 排除 Office 临时文件后的绝对文件路径列表。
    :raises FileNotFoundError: 来源根目录不存在时抛出。
    """
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise FileNotFoundError(resolved_root)
    iterator = resolved_root.rglob("*") if recursive else resolved_root.glob("*")
    files = [
        path
        for path in iterator
        if path.is_file()
        and path.suffix.lower() in _FORMAT_BY_SUFFIX
        and not path.name.startswith("~$")
    ]
    return sorted(files, key=lambda path: path.relative_to(resolved_root).as_posix().casefold())


def detect_source_format(path: Path) -> SourceFormat:
    """结合扩展名和文件签名识别支持的来源格式。

    :param path: 待识别文件。
    :return: DOC、DOCX、PDF、XLS 或 XLSX。
    :raises ValueError: 扩展名不支持或签名与格式不一致时抛出。
    """
    expected = _FORMAT_BY_SUFFIX.get(path.suffix.lower())
    if expected is None:
        raise ValueError(f"不支持的文件扩展名：{path.suffix}")
    with path.open("rb") as stream:
        signature = stream.read(8)
    if expected is SourceFormat.PDF:
        if not signature.startswith(b"%PDF-"):
            raise ValueError("PDF 文件签名无效。")
        return expected
    if expected in {SourceFormat.DOC, SourceFormat.XLS}:
        if signature != _OLE_SIGNATURE:
            raise ValueError("旧版 Office 文件签名无效。")
        return expected
    if not zipfile.is_zipfile(path):
        raise ValueError("现代 Office 文件不是有效的 ZIP 容器。")
    required_part = "word/document.xml" if expected is SourceFormat.DOCX else "xl/workbook.xml"
    with zipfile.ZipFile(path) as package:
        if required_part not in package.namelist():
            raise ValueError(f"Office 文件缺少必要部件：{required_part}")
    return expected


class SourceRegistrar:
    """把只读文件转换为统一来源登记记录。"""

    def __init__(self, source_root: Path, *, producer_version: str = "0.01") -> None:
        """初始化来源登记器。

        :param source_root: 所有登记文件必须位于其中的只读根目录。
        :param producer_version: 登记逻辑版本。
        :return: 无。
        :raises FileNotFoundError: 来源根目录不存在时抛出。
        """
        self.source_root = source_root.resolve()
        if not self.source_root.is_dir():
            raise FileNotFoundError(self.source_root)
        self.producer_version = producer_version

    def register(
        self,
        path: Path,
        *,
        knowledge_base_id: str,
        run_id: str,
        source_kind: SourceKind = SourceKind.ORIGINAL,
        converted_from_source_id: str | None = None,
        created_at: datetime | None = None,
    ) -> SourceDocument:
        """登记单个来源文件，不写入或修改来源目录。

        :param path: 位于来源根目录内的文件。
        :param knowledge_base_id: 文件所属知识库标识。
        :param run_id: 本次登记运行标识。
        :param source_kind: 原始、上传或转换来源类型。
        :param converted_from_source_id: 转换产物对应的原来源标识。
        :param created_at: 可注入的带时区创建时间。
        :return: 统一 `SourceDocument` 记录。
        :raises ValueError: 文件逃逸来源根目录或格式无效时抛出。
        """
        resolved = path.resolve()
        try:
            relative_path = resolved.relative_to(self.source_root).as_posix()
        except ValueError as exc:
            raise ValueError("来源文件必须位于指定来源根目录内。") from exc
        source_format = detect_source_format(resolved)
        source_hash = sha256_file(resolved)
        source_id = stable_id("source", knowledge_base_id, source_hash)
        normalized_file_name = f"{source_id.removeprefix('source_')}.{source_format.value}"
        return SourceDocument(
            source_id=source_id,
            knowledge_base_id=knowledge_base_id,
            original_file_name=resolved.name,
            normalized_file_name=normalized_file_name,
            source_format=source_format,
            source_kind=source_kind,
            source_sha256=source_hash,
            file_size_bytes=resolved.stat().st_size,
            relative_path=relative_path,
            converted_from_source_id=converted_from_source_id,
            lineage=LineageMetadata(
                run_id=run_id,
                producer="source_registry",
                producer_version=self.producer_version,
                created_at=created_at or datetime.now(UTC),
            ),
        )

    def verify_unchanged(self, source: SourceDocument) -> bool:
        """重新计算摘要以确认登记后的来源文件没有变化。

        :param source: 已登记来源记录。
        :return: 当前大小和 SHA-256 都与登记值一致时返回真。
        """
        path = (self.source_root / source.relative_path).resolve()
        try:
            path.relative_to(self.source_root)
        except ValueError:
            return False
        return path.stat().st_size == source.file_size_bytes and sha256_file(path) == source.source_sha256


def build_input_manifest(
    sources: list[SourceDocument],
    *,
    knowledge_base_id: str,
    effective_config_sha256: str,
    created_at: datetime | None = None,
) -> InputManifest:
    """生成与输入顺序无关的幂等键和输入清单。

    :param sources: 已登记来源记录。
    :param knowledge_base_id: 目标知识库标识。
    :param effective_config_sha256: 去除凭证后的有效配置摘要。
    :param created_at: 可注入的带时区清单时间。
    :return: 稳定排序的输入清单。
    :raises ValueError: 来源为空或包含其他知识库记录时抛出。
    """
    if not sources:
        raise ValueError("输入清单至少需要一个来源文件。")
    if any(source.knowledge_base_id != knowledge_base_id for source in sources):
        raise ValueError("输入清单中的来源必须属于同一知识库。")
    ordered = sorted(sources, key=lambda source: source.source_id)
    identity_payload = {
        "knowledge_base_id": knowledge_base_id,
        "source_sha256": [source.source_sha256 for source in ordered],
        "effective_config_sha256": effective_config_sha256,
    }
    idempotency_key = canonical_sha256(identity_payload)
    return InputManifest(
        manifest_id=stable_id("manifest", knowledge_base_id, idempotency_key),
        knowledge_base_id=knowledge_base_id,
        idempotency_key=idempotency_key,
        effective_config_sha256=effective_config_sha256,
        sources=ordered,
        created_at=created_at or datetime.now(UTC),
    )
