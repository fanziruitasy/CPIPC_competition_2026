"""实现怡佳方案的 Jieba、监管词典和完整 BM25 文档编码。"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jieba  # type: ignore[import-untyped]

from trusted_rag.domain.ports import SparseVector

_CJK_OR_WORD = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]+|[%‰]+", re.UNICODE)


@dataclass(frozen=True)
class Bm25Vocabulary:
    """一次语料快照固定的词表与文档长度统计。"""

    token_to_index: dict[str, int]
    document_frequencies: dict[str, int]
    average_document_length: float
    document_count: int


class JiebaBm25Indexer:
    """使用独立 Jieba 分词器构建 Qdrant BM25 稀疏表示。"""

    def __init__(
        self,
        *,
        user_terms: Sequence[str] = (),
        k1: float = 1.2,
        b: float = 0.75,
        max_features: int = 80_000,
    ) -> None:
        """初始化分词器和 BM25 参数。

        :param user_terms: 需要保持完整的监管术语。
        :param k1: 词频饱和参数。
        :param b: 文档长度归一化参数。
        :param max_features: 按文档频次保留的最大词项数。
        :return: 无。
        """
        if k1 <= 0 or not 0 <= b <= 1 or max_features <= 0:
            raise ValueError("BM25 参数不合法。")
        self.k1 = k1
        self.b = b
        self.max_features = max_features
        self.tokenizer = jieba.Tokenizer()
        self.user_terms = tuple(dict.fromkeys(term.strip() for term in user_terms if term.strip()))
        for term in self.user_terms:
            self.tokenizer.add_word(term)
        self.vocabulary: Bm25Vocabulary | None = None

    @classmethod
    def from_lexicon(cls, path: Path, **kwargs: Any) -> JiebaBm25Indexer:
        """从 UTF-8 一行一词文件创建索引器。

        :param path: 监管术语词典路径。
        :param kwargs: 传给构造函数的 BM25 参数。
        :return: 尚未拟合语料的索引器。
        """
        terms = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return cls(user_terms=terms, **kwargs)

    @classmethod
    def load(cls, path: Path) -> JiebaBm25Indexer:
        """恢复建库时保存的词表，使查询侧编码与索引完全一致。

        :param path: ``save`` 方法生成的 BM25 词表 JSON。
        :return: 已装载固定词表和统计量的索引器。
        :raises ValueError: 文件模式版本或词表结构不合法时抛出。
        """
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "jieba_bm25.v1":
            raise ValueError("不支持的 BM25 词表模式版本。")
        raw_vocabulary = payload.get("vocabulary")
        if not isinstance(raw_vocabulary, dict):
            raise ValueError("BM25 词表缺少 vocabulary。")
        indexer = cls(
            user_terms=payload.get("user_terms", ()),
            k1=float(payload["k1"]),
            b=float(payload["b"]),
            max_features=int(payload["max_features"]),
        )
        token_to_index = {
            str(term): int(index) for term, index in raw_vocabulary["token_to_index"].items()
        }
        if len(set(token_to_index.values())) != len(token_to_index):
            raise ValueError("BM25 词表索引不得重复。")
        indexer.vocabulary = Bm25Vocabulary(
            token_to_index=token_to_index,
            document_frequencies={
                str(term): int(count)
                for term, count in raw_vocabulary["document_frequencies"].items()
            },
            average_document_length=float(raw_vocabulary["average_document_length"]),
            document_count=int(raw_vocabulary["document_count"]),
        )
        return indexer

    def tokenize(self, text: str) -> list[str]:
        """执行 Jieba 主分词并追加怡佳实现的中文二元回退词。

        :param text: 中文或中英混合文本。
        :return: 已小写、去空白和标点的 token 序列。
        """
        normalized = text.lower().strip()
        tokens = [
            token
            for raw in self.tokenizer.lcut(normalized)
            if (token := raw.strip()) and not re.fullmatch(r"[\W_]+", token, flags=re.UNICODE)
        ]
        for match in _CJK_OR_WORD.finditer(normalized):
            value = match.group(0)
            if re.fullmatch(r"[\u4e00-\u9fff]{2,}", value):
                tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
                if len(value) <= 8:
                    tokens.append(value)
            else:
                tokens.append(value)
        return tokens

    def fit(self, texts: Sequence[str]) -> Bm25Vocabulary:
        """从完整语料拟合稳定词表和平均文档长度。

        :param texts: 所有分块的 `bm25_text`。
        :return: 固定词表与统计量。
        """
        tokenized = [self.tokenize(text) for text in texts]
        frequencies: Counter[str] = Counter()
        for tokens in tokenized:
            frequencies.update(set(tokens))
        selected = sorted(frequencies, key=lambda term: (-frequencies[term], term))[: self.max_features]
        average_length = sum(len(tokens) for tokens in tokenized) / len(tokenized) if tokenized else 0.0
        self.vocabulary = Bm25Vocabulary(
            token_to_index={term: index for index, term in enumerate(selected)},
            document_frequencies={term: frequencies[term] for term in selected},
            average_document_length=average_length,
            document_count=len(tokenized),
        )
        return self.vocabulary

    def encode(self, texts: Sequence[str]) -> list[SparseVector]:
        """生成文档侧 TF 饱和和长度归一化权重。

        :param texts: 待编码文档文本。
        :return: 与输入顺序一致的稀疏向量；IDF 由 Qdrant Modifier.IDF 计算。
        """
        vocabulary = self._require_vocabulary()
        average_length = max(vocabulary.average_document_length, 1.0)
        vectors: list[SparseVector] = []
        for text in texts:
            tokens = self.tokenize(text)
            counts = Counter(token for token in tokens if token in vocabulary.token_to_index)
            denominator_base = self.k1 * (
                1 - self.b + self.b * max(len(tokens), 1) / average_length
            )
            pairs = sorted(
                (
                    vocabulary.token_to_index[token],
                    float((count * (self.k1 + 1)) / (count + denominator_base)),
                )
                for token, count in counts.items()
            )
            pairs = [(index, value) for index, value in pairs if math.isfinite(value)]
            vectors.append(
                SparseVector(
                    indices=[index for index, _ in pairs],
                    values=[value for _, value in pairs],
                )
            )
        return vectors

    def encode_query(self, text: str) -> SparseVector:
        """生成查询侧词频向量。

        :param text: 用户问题或检索改写。
        :return: 未登录词已忽略的查询稀疏向量。
        """
        vocabulary = self._require_vocabulary()
        counts = Counter(token for token in self.tokenize(text) if token in vocabulary.token_to_index)
        pairs = sorted((vocabulary.token_to_index[token], float(count)) for token, count in counts.items())
        return SparseVector(
            indices=[index for index, _ in pairs],
            values=[value for _, value in pairs],
        )

    def save(self, path: Path) -> Path:
        """保存可复现的词表、统计量和 BM25 参数。

        :param path: 目标 JSON 路径。
        :return: 写入完成的路径。
        """
        vocabulary = self._require_vocabulary()
        payload = {
            "schema_version": "jieba_bm25.v1",
            "tokenizer": "jieba+regulatory_terms+bigram_fallback",
            "k1": self.k1,
            "b": self.b,
            "max_features": self.max_features,
            "user_terms": list(self.user_terms),
            "vocabulary": {
                "token_to_index": vocabulary.token_to_index,
                "document_frequencies": vocabulary.document_frequencies,
                "average_document_length": vocabulary.average_document_length,
                "document_count": vocabulary.document_count,
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _require_vocabulary(self) -> Bm25Vocabulary:
        if self.vocabulary is None:
            raise RuntimeError("必须先调用 fit() 拟合 BM25 词表。")
        return self.vocabulary
