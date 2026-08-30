"""根据规范化业务内容生成跨运行稳定的对象标识和摘要。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def canonical_sha256(value: Any) -> str:
    """计算与字典插入顺序无关的规范 SHA-256。

    :param value: 可转换为 JSON 的业务内容。
    :return: 六十四位小写十六进制 SHA-256。
    :raises TypeError: 输入包含无法规范化的类型时抛出。
    """
    encoded = json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_id(prefix: str, *identity_parts: Any) -> str:
    """生成不受运行标识影响的稳定业务对象标识。

    :param prefix: 体现对象类型的小写前缀。
    :param identity_parts: 决定对象身份且顺序固定的业务内容。
    :return: 形如 ``chunk_0123...`` 的二十四位摘要标识。
    :raises ValueError: 前缀无效或没有提供身份内容时抛出。
    """
    if not _PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError("稳定标识前缀必须使用小写字母、数字和下划线。")
    if not identity_parts:
        raise ValueError("稳定标识至少需要一个身份字段。")
    return f"{prefix}_{canonical_sha256(identity_parts)[:24]}"


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"无法规范化稳定标识字段类型：{type(value).__name__}")

