"""定义跨模块稳定错误码和安全的公开错误结构。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """系统可观测且可供前端判断的稳定错误码。"""

    CONFIG_FILE_NOT_FOUND = "configuration.file_not_found"
    CONFIG_INVALID = "configuration.invalid"
    INVALID_IDENTIFIER = "validation.invalid_identifier"
    INTERNAL_ERROR = "system.internal_error"


class TrustedRagError(Exception):
    """携带稳定错误码和安全公开消息的系统异常。"""

    def __init__(
        self,
        code: ErrorCode,
        public_message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        """初始化系统异常。

        :param code: 稳定错误码。
        :param public_message: 可直接返回前端且不含敏感信息的消息。
        :param details: 仅供内部诊断的结构化信息，默认不对外返回。
        :return: 无。
        """
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.details = details or {}

    def to_public_dict(self) -> dict[str, str]:
        """生成不包含内部诊断信息的公开错误结构。

        :return: 包含错误码和公开消息的字典。
        """
        return {"code": self.code.value, "message": self.public_message}

