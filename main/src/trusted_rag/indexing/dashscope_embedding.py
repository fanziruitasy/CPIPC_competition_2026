"""实现 DashScope text-embedding-v3 的 1024 维 Dense 向量端口。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from dashscope import TextEmbedding

from trusted_rag.domain.ports import DenseVector

EmbeddingCall = Callable[..., Any]


class DashScopeDenseEmbedder:
    """按固定配置调用 DashScope，并记录不含正文和密钥的审计日志。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "text-embedding-v3",
        dimensions: int = 1024,
        batch_size: int = 10,
        timeout_seconds: float = 60,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2,
        audit_path: Path | None = None,
        price_cny_per_million_tokens: float | None = None,
        call: EmbeddingCall = TextEmbedding.call,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """初始化 Dense Embedding 客户端。

        :param api_key: DashScope API Key；空值时读取 `DASHSCOPE_API_KEY`。
        :param model: 固定为 `text-embedding-v3`。
        :param dimensions: 固定为 1024。
        :param batch_size: 每次请求的最大文本数。
        :param timeout_seconds: 单次请求超时秒数。
        :param max_retries: 首次失败后的最大重试次数。
        :param retry_backoff_seconds: 指数退避基准秒数。
        :param audit_path: 可选 JSONL 调用审计文件。
        :param price_cny_per_million_tokens: 可选的每百万 Token 人民币价格。
        :param call: 可注入 DashScope SDK 调用函数。
        :param sleeper: 可注入等待函数。
        :return: 无。
        """
        if model != "text-embedding-v3" or dimensions != 1024:
            raise ValueError("Dense Embedding 必须使用 text-embedding-v3 和 1024 维。")
        if not 1 <= batch_size <= 10 or timeout_seconds <= 0 or max_retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("批大小、超时和重试参数不合法。")
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError("缺少 DASHSCOPE_API_KEY。")
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.audit_path = audit_path
        self.price_cny_per_million_tokens = price_cny_per_million_tokens
        self.call = call
        self.sleeper = sleeper

    def embed(self, texts: Sequence[str]) -> list[DenseVector]:
        """按输入顺序批量生成 Dense 向量。

        :param texts: 待向量化文本。
        :return: 数量、顺序与输入严格一致的 1024 维向量。
        :raises RuntimeError: 重试耗尽或响应结构不合法时抛出。
        """
        if any(not text.strip() for text in texts):
            raise ValueError("Embedding 文本不能为空。")
        vectors: list[DenseVector] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(list(texts[start : start + self.batch_size])))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[DenseVector]:
        started = time.monotonic()
        input_hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.call(
                    model=self.model,
                    input=texts,
                    api_key=self.api_key,
                    dimension=self.dimensions,
                    output_type="dense",
                    timeout=self.timeout_seconds,
                )
                request_id = getattr(response, "request_id", None)
                values, usage_tokens = self._parse_response(response, len(texts))
                self._audit(
                    status="success",
                    request_id=request_id,
                    attempt=attempt,
                    input_hashes=input_hashes,
                    elapsed_seconds=time.monotonic() - started,
                    usage_tokens=usage_tokens,
                )
                return [
                    DenseVector(values=item, model_name=self.model, request_id=request_id)
                    for item in values
                ]
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self.sleeper(self.retry_backoff_seconds * (2**attempt))
        self._audit(
            status="failed",
            request_id=None,
            attempt=self.max_retries,
            input_hashes=input_hashes,
            elapsed_seconds=time.monotonic() - started,
            usage_tokens=None,
            error=f"{type(last_error).__name__}: {last_error}",
        )
        raise RuntimeError(f"DashScope Embedding 请求失败：{last_error}")

    def _parse_response(self, response: Any, expected_count: int) -> tuple[list[list[float]], int | None]:
        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code != 200:
            code = str(getattr(response, "code", "") or "")
            message = str(getattr(response, "message", "") or "")
            raise RuntimeError(f"DashScope 返回状态码 {status_code}：{code} {message}".strip())
        output = getattr(response, "output", None)
        if not isinstance(output, dict):
            raise TypeError("DashScope response.output 不是字典。")
        items = output.get("embeddings") or output.get("results")
        if not isinstance(items, list) or len(items) != expected_count:
            raise RuntimeError("DashScope 返回向量数量与输入不一致。")
        if all(isinstance(item, dict) and "text_index" in item for item in items):
            items = sorted(items, key=lambda item: int(item["text_index"]))
        vectors: list[list[float]] = []
        for item in items:
            if not isinstance(item, dict):
                raise TypeError("DashScope 单条向量不是字典。")
            vector = item.get("embedding") or item.get("dense_embedding")
            if not isinstance(vector, list) or len(vector) != self.dimensions:
                raise RuntimeError("DashScope Dense 向量维度不等于 1024。")
            vectors.append([float(value) for value in vector])
        usage = output.get("usage") or getattr(response, "usage", None)
        usage_tokens = _usage_tokens(usage)
        return vectors, usage_tokens

    def _audit(
        self,
        *,
        status: str,
        request_id: str | None,
        attempt: int,
        input_hashes: list[str],
        elapsed_seconds: float,
        usage_tokens: int | None,
        error: str | None = None,
    ) -> None:
        if self.audit_path is None:
            return
        estimated_cost = None
        if usage_tokens is not None and self.price_cny_per_million_tokens is not None:
            estimated_cost = usage_tokens * self.price_cny_per_million_tokens / 1_000_000
        payload = {
            "status": status,
            "provider": "dashscope",
            "model": self.model,
            "dimensions": self.dimensions,
            "request_id": request_id,
            "attempt": attempt,
            "text_count": len(input_hashes),
            "input_sha256": input_hashes,
            "usage_tokens": usage_tokens,
            "estimated_cost_cny": estimated_cost,
            "elapsed_seconds": round(elapsed_seconds, 6),
            "error": error,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _usage_tokens(usage: Any) -> int | None:
    if isinstance(usage, dict):
        for key in ("total_tokens", "input_tokens", "tokens"):
            if usage.get(key) is not None:
                return int(usage[key])
    return None
