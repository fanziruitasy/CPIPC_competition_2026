"""验证 DashScope Dense Embedding 的顺序、重试和安全审计。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from trusted_rag.indexing.dashscope_embedding import DashScopeDenseEmbedder


def test_embedding_preserves_order_dimension_and_audit(tmp_path: Path) -> None:
    """API 返回顺序应按 text_index 复原，审计不得保存正文和密钥。"""
    audit_path = tmp_path / "embedding_audit.jsonl"

    def call(**_: object) -> SimpleNamespace:
        return SimpleNamespace(
            status_code=200,
            request_id="request-1",
            output={
                "embeddings": [
                    {"text_index": 1, "embedding": [2.0] * 1024},
                    {"text_index": 0, "embedding": [1.0] * 1024},
                ],
                "usage": {"total_tokens": 20},
            },
        )

    embedder = DashScopeDenseEmbedder(
        api_key="secret-key",
        call=call,
        audit_path=audit_path,
        price_cny_per_million_tokens=10,
    )
    vectors = embedder.embed(["资本充足率", "流动性覆盖率"])
    audit = json.loads(audit_path.read_text(encoding="utf-8"))

    assert vectors[0].values[0] == 1.0
    assert vectors[1].values[0] == 2.0
    assert len(vectors[0].values) == 1024
    assert audit["usage_tokens"] == 20
    assert audit["estimated_cost_cny"] == 0.0002
    assert "资本充足率" not in audit_path.read_text(encoding="utf-8")
    assert "secret-key" not in audit_path.read_text(encoding="utf-8")


def test_embedding_retries_once_then_succeeds() -> None:
    """临时失败应按配置重试且不改变输入顺序。"""
    attempts = 0

    def call(**_: object) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary")
        return SimpleNamespace(
            status_code=200,
            request_id="request-2",
            output={"embeddings": [{"embedding": [3.0] * 1024}]},
        )

    embedder = DashScopeDenseEmbedder(
        api_key="test",
        call=call,
        max_retries=1,
        sleeper=lambda _: None,
    )

    assert embedder.embed(["测试"])[0].values[0] == 3.0
    assert attempts == 2

