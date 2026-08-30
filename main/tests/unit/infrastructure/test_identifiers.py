"""验证追踪标识和版本化运行标识。"""

from datetime import UTC, datetime

import pytest

from trusted_rag.infrastructure.errors import ErrorCode, TrustedRagError
from trusted_rag.infrastructure.identifiers import create_run_id, create_trace_id


def test_create_run_id_is_readable_and_deterministic_with_fixed_inputs() -> None:
    """固定输入应生成符合命名规范的确定性运行标识。"""
    run_id = create_run_id(
        "document-ingestion",
        "v0.01",
        occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        suffix="a1b2c3d4",
    )

    assert run_id == "document-ingestion-v0.01-20260830T120000Z-a1b2c3d4"


def test_create_trace_id_uses_requested_prefix() -> None:
    """追踪标识应保留合法类型前缀。"""
    assert create_trace_id("answer").startswith("answer_")


def test_invalid_pipeline_is_rejected() -> None:
    """包含路径字符的流水线名称必须被拒绝。"""
    with pytest.raises(TrustedRagError) as captured:
        create_run_id("../documents", "0.01")

    assert captured.value.code is ErrorCode.INVALID_IDENTIFIER

