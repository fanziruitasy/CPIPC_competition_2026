"""实现低置信度查询使用的 DashScope 受控规划模型。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from openai import OpenAI

from trusted_rag.domain.query import QueryPlan
from trusted_rag.retrieval.query_planner import parse_model_json


class DashScopePlannerModel:
    """通过 JSON Schema 边界把模型输出限制为查询计划补丁。"""

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
        """初始化查询规划模型客户端。

        :param api_key: DashScope API Key；为空时读取环境变量。
        :param base_url: OpenAI 兼容接口地址。
        :param model: 查询规划模型名称。
        :param timeout_seconds: 单次请求超时秒数。
        :param max_retries: SDK 重试次数。
        :param request_callable: 测试可注入的生成函数。
        :return: 无。
        """
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError("缺少 DASHSCOPE_API_KEY。")
        self.model = model
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

    def __call__(self, question: str, rule_plan: QueryPlan) -> dict[str, Any]:
        """生成仅含白名单字段的规划补丁。

        :param question: 用户原始问题。
        :param rule_plan: 规则规划结果。
        :return: 待 ModelPlanPatch 严格校验的 JSON 对象。
        """
        system = (
            "你是银行业查询规划器，只负责消解规则无法确定的查询，不回答问题。"
            "只能输出 JSON，字段必须为 route、intent、semantic_queries、filters、"
            "structured_operations、clarification_required、rejection_reason、reason。"
            "禁止输出 SQL。route 只能是 document/structured/mixed/reject；"
            "structured_operations.operation 只能是 lookup/sum/average/minimum/maximum/count/"
            "difference/ratio/trend。不能确定时 clarification_required=true。"
        )
        payload = {
            "question": question,
            "rule_plan": rule_plan.model_dump(mode="json"),
        }
        response = self.request_callable(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=900,
            extra_body={"enable_search": False, "enable_thinking": False},
        )
        choices = getattr(response, "choices", None)
        if not choices or not isinstance(choices[0].message.content, str):
            raise TypeError("查询规划模型返回缺少 JSON 内容。")
        return parse_model_json(choices[0].message.content)

