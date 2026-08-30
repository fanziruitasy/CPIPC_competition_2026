"""验证选择题选项映射的规则优先与模型降级。"""

from types import SimpleNamespace
from typing import Any

from trusted_rag.evaluation.option_resolver import DashScopeOptionResolver


def test_numeric_resolution_does_not_call_model() -> None:
    """答案包含唯一候选数值时应确定性映射。"""
    called = False

    def request(**_: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("不应调用模型")

    resolver = DashScopeOptionResolver(request_callable=request)
    result = resolver.resolve(
        "该指标为31739.18亿元。",
        {"A": "31739.18", "B": "6428.56", "C": "24912.73", "D": "397.89"},
    )

    assert result.choice == "A"
    assert result.method == "numeric"
    assert called is False


def test_model_resolves_paraphrased_text() -> None:
    """确定性规则无法唯一命中时应使用受控模型映射。"""

    def request(**_: Any) -> Any:
        return SimpleNamespace(
            id="resolver-request",
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"choice":"C"}'))],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=3),
        )

    resolver = DashScopeOptionResolver(request_callable=request)
    result = resolver.resolve(
        "该制度要求进行持续监测并及时处置风险。",
        {"A": "无需监测", "B": "仅年度检查", "C": "持续风险监测", "D": "取消处置"},
    )

    assert result.choice == "C"
    assert result.method == "model"
    assert result.input_tokens == 20
