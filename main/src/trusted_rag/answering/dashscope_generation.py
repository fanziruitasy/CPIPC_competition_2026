"""实现 DashScope 兼容接口的受控 JSON 回答生成客户端。"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from openai import OpenAI

from trusted_rag.domain.knowledge import EvidenceUnit
from trusted_rag.domain.ports import AnswerDraft


class DashScopeAnswerGenerator:
    """要求模型只使用编号证据回答并返回严格 JSON。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model: str = "qwen3.7-plus",
        timeout_seconds: int = 120,
        max_retries: int = 2,
        max_tokens: int = 1200,
        request_callable: Callable[..., Any] | None = None,
    ) -> None:
        """初始化生成客户端，不立即调用模型。

        :param api_key: DashScope API Key；为空时读取环境变量。
        :param base_url: OpenAI 兼容接口地址。
        :param model: 回答模型名称。
        :param timeout_seconds: 单次请求超时秒数。
        :param max_retries: SDK 内部重试次数。
        :param max_tokens: 最大输出 Token 数。
        :param request_callable: 测试可注入的生成函数。
        :return: 无。
        """
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError("缺少 DASHSCOPE_API_KEY。")
        self.model = model
        self.max_tokens = max_tokens
        if request_callable is None:
            client = OpenAI(
                api_key=self.api_key,
                base_url=base_url.rstrip("/"),
                timeout=timeout_seconds,
                max_retries=max_retries,
            )
            self.request_callable = client.chat.completions.create
        else:
            self.request_callable = request_callable

    def generate(self, question: str, evidence: Sequence[EvidenceUnit]) -> AnswerDraft:
        """根据最小证据集生成带证据 ID 的回答草稿。

        :param question: 用户原始问题。
        :param evidence: 已通过生成前门禁的证据。
        :return: 尚需确定性校验的回答草稿。
        """
        system = (
            "你是银行业可信问答助手。只能依据给出的 evidence 回答，不得补充外部事实。"
            "输出 JSON 对象，仅含 answer_text、cited_evidence_ids、refusal_reason。"
            "证据不足时 answer_text 为空并填写 refusal_reason；回答中的每个数字和规范性结论必须可由引用证据直接核验。"
        )
        payload = {
            "question": question,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "text": item.excerpt,
                    "source_value": item.source_value,
                    "unit": item.unit,
                    "location": item.location.model_dump(mode="json"),
                }
                for item in evidence
            ],
        }
        started = time.monotonic()
        response = self.request_callable(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=self.max_tokens,
            extra_body={"enable_search": False, "enable_thinking": False},
        )
        raw = _response_content(response)
        parsed = _json_object(raw)
        usage = getattr(response, "usage", None)
        return AnswerDraft(
            answer_text=str(parsed.get("answer_text") or ""),
            cited_evidence_ids=[str(item) for item in parsed.get("cited_evidence_ids", [])],
            refusal_reason=(str(parsed["refusal_reason"]) if parsed.get("refusal_reason") else None),
            model_name=self.model,
            input_tokens=_usage(usage, "prompt_tokens", "input_tokens"),
            output_tokens=_usage(usage, "completion_tokens", "output_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=getattr(response, "id", None),
        )


def _response_content(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        raise TypeError("回答模型返回缺少 choices。")
    message = choices[0].message
    content = message.content
    if not isinstance(content, str) or not content.strip():
        raise TypeError("回答模型返回内容为空。")
    return content.strip()


def _json_object(content: str) -> dict[str, Any]:
    start = content.find("{")
    if start < 0:
        raise ValueError("回答模型未返回 JSON 对象。")
    value, _ = json.JSONDecoder().raw_decode(content[start:])
    if not isinstance(value, dict):
        raise ValueError("回答模型 JSON 根节点必须是对象。")
    return value


def _usage(usage: Any, *names: str) -> int:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if value is not None:
            return int(value)
    return 0
