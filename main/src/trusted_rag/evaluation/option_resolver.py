"""把可信开放式答案映射为官方选择题选项。"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from openai import OpenAI

from trusted_rag.domain.common import ContractModel

Choice = Literal["A", "B", "C", "D"]
_CHOICES: tuple[Choice, ...] = ("A", "B", "C", "D")
_NUMBER_ONLY = re.compile(r"^[-+]?\d+(?:[,.]\d+)*(?:%|％)?$")
_ANSWER_CHOICE = re.compile(r"(?:答案|选择|选项)\s*[:：]?\s*([ABCD])\b", re.IGNORECASE)


class OptionResolution(ContractModel):
    """一次确定性或模型选项映射结果。"""

    choice: Choice | None = None
    method: Literal["empty", "explicit_label", "numeric", "text", "model", "unresolved"]
    model_name: str | None = None
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class DashScopeOptionResolver:
    """确定性规则优先，必要时调用模型完成语义选项映射。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model: str = "qwen3.7-plus",
        timeout_seconds: int = 60,
        max_retries: int = 2,
        request_callable: Callable[..., Any] | None = None,
    ) -> None:
        """初始化选项映射器。

        :param api_key: DashScope API Key；空值时读取环境变量。
        :param base_url: OpenAI 兼容接口地址。
        :param model: 语义映射模型。
        :param timeout_seconds: 单次请求超时秒数。
        :param max_retries: SDK 内部重试次数。
        :param request_callable: 测试可注入的请求函数。
        :return: 无。
        """
        self.model = model
        if request_callable is not None:
            self.request_callable = request_callable
            return
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ValueError("缺少 DASHSCOPE_API_KEY。")
        client = OpenAI(
            api_key=key,
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self.request_callable = client.chat.completions.create

    def resolve(self, answer_text: str, options: Mapping[Choice, str]) -> OptionResolution:
        """把已生成答案映射为唯一 A/B/C/D。

        :param answer_text: 已通过可信回答门禁的开放式答案。
        :param options: 官方题目四个候选项。
        :return: 选项、映射方式和可选模型用量。
        """
        normalized_options = {choice: str(options[choice]).strip() for choice in _CHOICES}
        deterministic = _deterministic_resolution(answer_text, normalized_options)
        if deterministic is not None:
            return deterministic
        started = time.monotonic()
        response = self.request_callable(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你只负责把已有答案映射到最相符的候选项，不得重新回答问题或补充外部事实。"
                        "输出 JSON，仅含 choice；能够唯一映射时为 A/B/C/D，否则为 null。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"answer_text": answer_text, "options": normalized_options},
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=50,
            extra_body={"enable_search": False, "enable_thinking": False},
        )
        raw = _response_content(response)
        parsed = _json_object(raw)
        raw_choice = str(parsed.get("choice") or "").strip().upper()
        choice: Choice | None = raw_choice if raw_choice in _CHOICES else None  # type: ignore[assignment]
        usage = getattr(response, "usage", None)
        return OptionResolution(
            choice=choice,
            method="model" if choice else "unresolved",
            model_name=self.model,
            request_id=getattr(response, "id", None),
            input_tokens=_usage(usage, "prompt_tokens", "input_tokens"),
            output_tokens=_usage(usage, "completion_tokens", "output_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def _deterministic_resolution(
    answer_text: str,
    options: Mapping[Choice, str],
) -> OptionResolution | None:
    answer = answer_text.strip()
    if not answer:
        return OptionResolution(method="empty")
    label = _ANSWER_CHOICE.search(answer)
    if label:
        return OptionResolution(choice=label.group(1).upper(), method="explicit_label")  # type: ignore[arg-type]
    answer_numbers = _numbers(answer)
    numeric_matches = [
        choice
        for choice, text in options.items()
        if (number := _number_value(text)) is not None and number in answer_numbers
    ]
    if len(numeric_matches) == 1:
        return OptionResolution(choice=numeric_matches[0], method="numeric")
    normalized_answer = _normalize_text(answer)
    text_matches = [
        choice
        for choice, text in options.items()
        if len(normalized := _normalize_text(text)) >= 2 and normalized in normalized_answer
    ]
    if len(text_matches) == 1:
        return OptionResolution(choice=text_matches[0], method="text")
    return None


def _numbers(text: str) -> set[Decimal]:
    result: set[Decimal] = set()
    for value in re.findall(r"[-+]?\d+(?:[,.]\d+)*(?:%|％)?", text):
        parsed = _number_value(value)
        if parsed is not None:
            result.add(parsed)
    return result


def _number_value(text: str) -> Decimal | None:
    value = text.strip().replace("，", ",").replace("％", "%")
    if not _NUMBER_ONLY.fullmatch(value):
        return None
    is_percent = value.endswith("%")
    value = value.removesuffix("%").replace(",", "")
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number / 100 if is_percent else number


def _normalize_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold(), flags=re.UNICODE)


def _response_content(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        raise TypeError("选项映射模型返回缺少 choices。")
    content = choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise TypeError("选项映射模型返回内容为空。")
    return content.strip()


def _json_object(content: str) -> dict[str, Any]:
    start = content.find("{")
    if start < 0:
        raise ValueError("选项映射模型未返回 JSON 对象。")
    value, _ = json.JSONDecoder().raw_decode(content[start:])
    if not isinstance(value, dict):
        raise ValueError("选项映射模型 JSON 根节点必须是对象。")
    return value


def _usage(usage: Any, *names: str) -> int:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if value is not None:
            return int(value)
    return 0
