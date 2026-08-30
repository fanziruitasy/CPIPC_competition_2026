"""实现 DashScope qwen3-rerank 精排和可审计确定性降级。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

import dashscope
from dashscope import TextReRank

from trusted_rag.domain.ports import SearchHit
from trusted_rag.retrieval.contracts import RerankCallAudit, RerankResult


class DashScopeReranker:
    """对融合候选调用 qwen3-rerank，失败时保留 RRF 顺序。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/api/v1",
        model: str = "qwen3-rerank",
        timeout_seconds: int = 60,
        max_retries: int = 2,
        retry_backoff_seconds: float = 2,
        instruct: str | None = "检索与银行业监管制度、统计指标问题最相关且可直接作为证据的内容",
        request_callable: Callable[..., Any] | None = None,
    ) -> None:
        """初始化精排客户端，不立即发起请求。

        :param api_key: DashScope API Key；为空时读取环境变量。
        :param base_url: DashScope 原生 API 根地址。
        :param model: 固定为 qwen3-rerank。
        :param timeout_seconds: 单次请求超时秒数。
        :param max_retries: 首次失败后的重试次数。
        :param retry_backoff_seconds: 指数退避基准秒数。
        :param instruct: 中文精排任务指令。
        :param request_callable: 测试可注入的 SDK 调用函数。
        :return: 无。
        """
        if model != "qwen3-rerank" or timeout_seconds <= 0 or max_retries < 0:
            raise ValueError("Rerank 配置不合法。")
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError("缺少 DASHSCOPE_API_KEY。")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.instruct = instruct
        self.request_callable = request_callable or TextReRank.call
        dashscope.base_http_api_url = base_url.rstrip("/")

    def rerank(self, question: str, hits: Sequence[SearchHit], *, top_k: int) -> list[SearchHit]:
        """执行精排并返回候选，符合统一 Reranker 端口。

        :param question: 用户原始问题。
        :param hits: RRF 或单路召回候选。
        :param top_k: 最终保留数量。
        :return: 精排成功或确定性降级后的候选。
        """
        return self.rerank_with_audit(question, hits, top_k=top_k).hits

    def rerank_with_audit(
        self,
        question: str,
        hits: Sequence[SearchHit],
        *,
        top_k: int,
    ) -> RerankResult:
        """执行一次带脱敏审计的精排。

        :param question: 用户问题。
        :param hits: 待精排候选。
        :param top_k: 最终保留数量。
        :return: 候选与成功或降级审计。
        """
        if top_k <= 0:
            raise ValueError("top_k 必须为正数。")
        limited = list(hits)
        digest = _request_digest(question, limited)
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                request: dict[str, Any] = {
                    "model": self.model,
                    "query": question,
                    "documents": [hit.chunk.display_text[:4000] for hit in limited],
                    "return_documents": False,
                    "top_n": len(limited),
                    "api_key": self.api_key,
                    "timeout": self.timeout_seconds,
                }
                if self.instruct:
                    request["instruct"] = self.instruct
                response = self.request_callable(**request)
                scores = _parse_scores(response, len(limited))
                ordered = sorted(
                    enumerate(limited),
                    key=lambda item: (-scores[item[0]], item[1].rank, item[1].chunk_id),
                )[:top_k]
                reranked = [
                    hit.model_copy(update={"score": scores[index], "rank": rank, "channel": "qwen3-rerank"})
                    for rank, (index, hit) in enumerate(ordered, start=1)
                ]
                return RerankResult(
                    hits=reranked,
                    audit=RerankCallAudit(
                        request_sha256=digest,
                        request_id=getattr(response, "request_id", None),
                        candidate_count=len(limited),
                        attempt_count=attempt + 1,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        status="success",
                    ),
                )
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_seconds * (2**attempt))
        fallback = [
            hit.model_copy(update={"rank": rank, "channel": "rrf_fallback"})
            for rank, hit in enumerate(limited[:top_k], start=1)
        ]
        return RerankResult(
            hits=fallback,
            audit=RerankCallAudit(
                request_sha256=digest,
                candidate_count=len(limited),
                attempt_count=self.max_retries + 1,
                latency_ms=int((time.monotonic() - started) * 1000),
                status="fallback",
                fallback_reason=type(last_error).__name__ if last_error else "UnknownError",
            ),
        )


def _request_digest(question: str, hits: Sequence[SearchHit]) -> str:
    payload = {
        "question": question,
        "candidates": [
            {"chunk_id": hit.chunk_id, "text_sha256": hit.chunk.content_sha256}
            for hit in hits
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _parse_scores(response: Any, expected: int) -> dict[int, float]:
    status = int(getattr(response, "status_code", 0) or 0)
    if status and status != 200:
        raise RuntimeError(f"DashScope Rerank 返回状态码 {status}。")
    output = getattr(response, "output", None)
    results = output.get("results") if isinstance(output, dict) else getattr(output, "results", None)
    if not isinstance(results, list):
        raise TypeError("Rerank 返回缺少 results。")
    scores: dict[int, float] = {}
    for item in results:
        index = item.get("index") if isinstance(item, dict) else getattr(item, "index", None)
        score = item.get("relevance_score") if isinstance(item, dict) else getattr(item, "relevance_score", None)
        if not isinstance(index, int) or not 0 <= index < expected or index in scores:
            raise ValueError("Rerank 返回候选索引不合法。")
        if score is None:
            raise ValueError("Rerank 返回候选分数为空。")
        scores[index] = float(score)
    if set(scores) != set(range(expected)):
        raise ValueError("Rerank 未返回全部候选分数。")
    return scores
