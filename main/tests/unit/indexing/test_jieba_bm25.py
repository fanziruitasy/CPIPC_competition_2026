"""验证怡佳 Jieba 分词和完整 BM25 文档权重。"""

from __future__ import annotations

from pathlib import Path

from trusted_rag.indexing.bm25 import JiebaBm25Indexer


def test_regulatory_term_and_vocabulary_are_stable() -> None:
    """监管术语应保持完整，相同语料重复拟合应产生相同词项编号。"""
    texts = ["商业银行资本充足率不得低于监管要求", "资本充足率计算口径"]
    left = JiebaBm25Indexer(user_terms=["资本充足率"])
    right = JiebaBm25Indexer(user_terms=["资本充足率"])

    left_vocabulary = left.fit(texts)
    right_vocabulary = right.fit(texts)

    assert "资本充足率" in left.tokenize(texts[0])
    assert left_vocabulary.token_to_index == right_vocabulary.token_to_index


def test_bm25_applies_tf_saturation_and_length_normalization() -> None:
    """重复词收益必须饱和，较长文档中的同频词权重应更低。"""
    indexer = JiebaBm25Indexer(user_terms=["资本充足率"])
    indexer.fit(["资本充足率", "资本充足率 资本充足率", "资本充足率 其他 指标 说明 很长"])
    single, repeated, long_document = indexer.encode(
        ["资本充足率", "资本充足率 资本充足率", "资本充足率 其他 指标 说明 很长"]
    )
    term_index = indexer.vocabulary.token_to_index["资本充足率"]  # type: ignore[union-attr]

    single_weight = single.values[single.indices.index(term_index)]
    repeated_weight = repeated.values[repeated.indices.index(term_index)]
    long_weight = long_document.values[long_document.indices.index(term_index)]

    assert single_weight < repeated_weight < single_weight * 2
    assert long_weight < single_weight


def test_saved_vocabulary_can_be_loaded_for_query_encoding(tmp_path: Path) -> None:
    """查询阶段必须恢复建库词表，保证词项索引完全一致。"""
    indexer = JiebaBm25Indexer(user_terms=["资本充足率"])
    indexer.fit(["商业银行资本充足率要求", "资本监管规定"])
    path = indexer.save(tmp_path / "bm25.json")

    restored = JiebaBm25Indexer.load(path)

    assert restored.encode_query("资本充足率") == indexer.encode_query("资本充足率")
