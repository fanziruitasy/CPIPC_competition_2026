"""定义跨知识、查询和存储契约复用的基础结构。"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from trusted_rag.domain.enums import ModelContentStatus, QualityStatus

NonEmptyStr = Annotated[str, Field(min_length=1)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*_[0-9a-f]{24}$")]

_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class ContractModel(BaseModel):
    """禁止未知字段且创建后不可变的契约基础模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class BoundingBox(ContractModel):
    """页面左上坐标系中的二维边界框。"""

    left: float
    top: float
    right: float
    bottom: float

    @model_validator(mode="after")
    def validate_bounds(self) -> BoundingBox:
        """校验右下坐标不小于左上坐标。

        :return: 通过坐标约束的当前边界框。
        :raises ValueError: 坐标顺序无效时抛出。
        """
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("边界框右下坐标不得小于左上坐标。")
        return self


class SourceLocation(ContractModel):
    """内部证据使用的 Word、PDF、Excel 或产物定位。"""

    page_number: Annotated[int, Field(ge=1)] | None = None
    bounding_box: BoundingBox | None = None
    section_path: list[str] = Field(default_factory=list)
    clause: str | None = None
    docling_ref: str | None = None
    element_ref: str | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    table_id: str | None = None
    artifact_uri: str | None = None

    @field_validator("artifact_uri")
    @classmethod
    def reject_absolute_artifact_path(cls, value: str | None) -> str | None:
        """禁止把宿主机绝对路径写入统一契约。

        :param value: 相对产物 URI 或空值。
        :return: 通过检查的原值。
        :raises ValueError: 输入为 Windows 或 POSIX 绝对路径时抛出。
        """
        if value and (_WINDOWS_ABSOLUTE_PATH.match(value) or value.startswith(("/", "\\"))):
            raise ValueError("artifact_uri 必须是相对 URI，不能是宿主机绝对路径。")
        return value

    @model_validator(mode="after")
    def validate_location(self) -> SourceLocation:
        """校验边界框和单元格定位的依赖字段。

        :return: 通过位置约束的当前对象。
        :raises ValueError: 边界框缺少页码或单元格缺少 Sheet 时抛出。
        """
        if self.bounding_box is not None and self.page_number is None:
            raise ValueError("PDF 边界框必须同时提供 page_number。")
        if self.cell_range is not None and self.sheet_name is None:
            raise ValueError("Excel 单元格区域必须同时提供 sheet_name。")
        return self


class PublicSourceLocation(ContractModel):
    """可返回前端且不包含内部路径的位置。"""

    page_number: Annotated[int, Field(ge=1)] | None = None
    section_path: list[str] = Field(default_factory=list)
    clause: str | None = None
    element_ref: str | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    table_id: str | None = None

    @model_validator(mode="after")
    def validate_cell_location(self) -> PublicSourceLocation:
        """校验公开单元格定位必须包含 Sheet。

        :return: 通过检查的当前对象。
        :raises ValueError: 只有单元格区域而没有 Sheet 时抛出。
        """
        if self.cell_range is not None and self.sheet_name is None:
            raise ValueError("公开单元格引用必须同时提供 sheet_name。")
        return self


class QualityMetadata(ContractModel):
    """记录质量状态、标志和人工复核信息。"""

    status: QualityStatus = QualityStatus.PASSED
    flags: list[str] = Field(default_factory=list)
    requires_manual_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class ModelGeneratedContent(ContractModel):
    """图片或公式的大模型生成描述及其校验信息。"""

    text: NonEmptyStr
    model_name: NonEmptyStr
    validation_status: ModelContentStatus
    original_element_ref: NonEmptyStr
    generated_at: AwareDatetime
    request_id: str | None = None


class LineageMetadata(ContractModel):
    """记录从来源输入到当前记录的版本血缘。"""

    run_id: NonEmptyStr
    producer: NonEmptyStr
    producer_version: NonEmptyStr
    input_ids: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


class ArtifactReferences(ContractModel):
    """解析形成的机器产物与人工检查产物相对 URI。"""

    structured_json_uri: NonEmptyStr
    markdown_uri: str | None = None
    html_uri: str | None = None
    asset_directory_uri: str | None = None

    @field_validator("structured_json_uri", "markdown_uri", "html_uri", "asset_directory_uri")
    @classmethod
    def require_relative_uri(cls, value: str | None) -> str | None:
        """确保所有产物引用均不包含宿主机绝对路径。

        :param value: 待校验的产物 URI。
        :return: 通过校验的原值。
        :raises ValueError: 检测到绝对路径时抛出。
        """
        if value and (_WINDOWS_ABSOLUTE_PATH.match(value) or value.startswith(("/", "\\"))):
            raise ValueError("产物引用必须使用相对 URI。")
        return value


def model_to_utf8_json(model: BaseModel, *, indent: int | None = 2) -> str:
    """把契约模型序列化为不转义中文的 JSON。

    :param model: 任意 Pydantic 契约模型。
    :param indent: JSON 缩进，传入空值时输出紧凑格式。
    :return: UTF-8 中文原样可读的 JSON 字符串。
    """
    return model.model_dump_json(indent=indent)


JsonObject = dict[str, Any]

