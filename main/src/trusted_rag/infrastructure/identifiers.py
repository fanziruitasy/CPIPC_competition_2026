"""集中生成可追踪请求标识和版本化运行标识。"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from trusted_rag.infrastructure.errors import ErrorCode, TrustedRagError

_PIPELINE_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}$")
_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def create_trace_id(prefix: str = "trace") -> str:
    """生成请求、任务或回答链路使用的唯一追踪标识。

    :param prefix: 标识类型前缀，只允许小写字母、数字和下划线。
    :return: 形如 ``trace_0123...`` 的唯一标识。
    :raises TrustedRagError: 前缀不符合命名规则时抛出。
    """
    if not _PREFIX_PATTERN.fullmatch(prefix):
        raise TrustedRagError(ErrorCode.INVALID_IDENTIFIER, "追踪标识前缀不符合命名规则。")
    return f"{prefix}_{uuid.uuid4().hex}"


def create_run_id(
    pipeline: str,
    config_version: str,
    *,
    occurred_at: datetime | None = None,
    suffix: str | None = None,
) -> str:
    """生成不会覆盖历史产物的版本化运行标识。

    :param pipeline: 使用小写短横线连接的流水线名称。
    :param config_version: 配置版本，可带或不带 ``v`` 前缀。
    :param occurred_at: 可选 UTC 时间，主要用于可重复测试。
    :param suffix: 可选八位小写十六进制后缀，主要用于可重复测试。
    :return: 形如 ``document-v0.01-20260830T120000Z-a1b2c3d4`` 的运行标识。
    :raises TrustedRagError: 流水线、版本或后缀不符合命名规则时抛出。
    """
    normalized_version = config_version.removeprefix("v")
    normalized_suffix = suffix or uuid.uuid4().hex[:8]
    if not _PIPELINE_PATTERN.fullmatch(pipeline):
        raise TrustedRagError(ErrorCode.INVALID_IDENTIFIER, "流水线名称不符合命名规则。")
    if not _VERSION_PATTERN.fullmatch(normalized_version):
        raise TrustedRagError(ErrorCode.INVALID_IDENTIFIER, "配置版本不符合命名规则。")
    if not re.fullmatch(r"[0-9a-f]{8}", normalized_suffix):
        raise TrustedRagError(ErrorCode.INVALID_IDENTIFIER, "运行标识后缀不符合命名规则。")

    timestamp = (occurred_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{pipeline}-v{normalized_version}-{timestamp}-{normalized_suffix}"

