"""Evidence-grounded answer generation for retrieved Excel evidence."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from model_api import api_endpoint, post_json

@dataclass(frozen=True)
class GroundedAnswer:
    answer_type: str
    answer: str
    reason: str
    missing_information: list[str]
    evidence_indices: list[int]
    confidence: float
    backend: str


def _extract_json_object(text: str) -> dict[str, Any] | None:
    candidate = (text or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(candidate[start : end + 1])
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


class QwenGroundedAnswerer:
    """Generate an answer using only evidence supplied by the retrieval layer."""

    def __init__(
        self,
        api_key: str,
        model: str,
        endpoint: str,
        *,
        timeout: int = 60,
        max_evidence_chars: int = 16000,
    ):
        self.api_key = api_key
        self.model = model
        self.endpoint = api_endpoint(endpoint, "chat/completions")
        self.timeout = timeout
        self.max_evidence_chars = max(1000, max_evidence_chars)

    @classmethod
    def from_env(cls) -> QwenGroundedAnswerer | None:
        api_key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            model=os.environ.get(
                "QWEN_ANSWER_MODEL", os.environ.get("QWEN_MODEL", "qwen3.7-plus")
            ),
            endpoint=os.environ.get(
                "DASHSCOPE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            timeout=int(os.environ.get("QWEN_ANSWER_TIMEOUT", "60") or 60),
            max_evidence_chars=int(
                os.environ.get("QWEN_MAX_EVIDENCE_CHARS", "16000") or 16000
            ),
        )

    def __call__(
        self,
        *,
        question: str,
        evidence: list[dict[str, Any]],
        draft_answer: str,
        source_file: str,
        options: dict[str, str] | None = None,
    ) -> GroundedAnswer:
        if not evidence:
            return GroundedAnswer(
                answer_type="refuse",
                answer="当前没有检索到可以支持回答的证据。",
                reason="检索证据为空，禁止使用模型自身知识作答。",
                missing_information=["与问题直接相关的库内证据"],
                evidence_indices=[],
                confidence=0.0,
                backend=f"qwen:{self.model}",
            )

        evidence_blocks: list[str] = []
        remaining = self.max_evidence_chars
        for index, item in enumerate(evidence, start=1):
            block = f"[E{index}] " + json.dumps(item, ensure_ascii=False, default=str)
            if len(block) > remaining:
                block = block[:remaining]
            if block:
                evidence_blocks.append(block)
                remaining -= len(block)
            if remaining <= 0:
                break

        system_prompt = """
你是金融监管 Excel 知识库的受证据约束回答器。你只能使用用户消息里的“检索证据”和“受控查询结果草稿”，不得使用模型自身知识补充事实。

必须遵守：
1. 证据直接、完整支持问题时，answer_type="answer"。
2. 缺少期间、机构、地区、指标口径、统计粒度等用户可补充条件时，answer_type="clarify"，明确列出缺项。
3. 证据中没有目标指标、缺少外部材料、只能看到相关原则但不能推出结论、要求原因归因但证据没有原因时，answer_type="refuse"。
4. 禁止编造数字、日期、机构、文件、文号、原因或结论。不能把“相关”当成“足以证明”。
5. 引用只能填写实际支持判断的证据编号，例如 [1, 3]；不得生成不存在的编号。
6. 受控查询结果草稿用于保持精确数值和选项，不是独立证据；若它与证据冲突，以拒答为准。
7. evidence_kind="source_capability_profile" 是对该 Sheet 结构化事实的覆盖清单。当 listed_values_complete 的对应维度为 true 时，可据此明确判断某指标、地区、机构或产品线“表内没有”；不要误说成“没有检索到，无法确认”。
8. 若问题询问“能否查到”且事实证据直接命中目标地区/机构/指标，应据实回答能查到，必要时给出证据中的数值和口径。
9. 数据源已经明确且目标维度或指标经完整覆盖清单证明不存在时，使用 refuse；只有用户补充期间、机构、地区或口径后就可继续查询时，使用 clarify。
10. footnotes 是源表脚注证据，判断统计定义和适用边界时必须与事实行一起使用，不能声称脚注明示的条件“不在表内”。
11. available_entity_levels 区分单家机构 institution、机构类别 category 和行业汇总 industry_aggregate；类别或汇总不得冒充单家公司。
12. evidence_kind="missing_operand" 表示受控计算缺少必要操作数，必须 refuse，不得用相近指标替代。
13. 如果面向用户的回答表达“不能直接给出、无法计算、表内未提供或需要其他证据”，answer_type 不得填写 answer；可由用户补充明确条件时用 clarify，否则用 refuse。
14. 只输出 JSON，不要输出 Markdown。

JSON 字段：
{"answer_type":"answer/clarify/refuse","answer":"面向用户的中文回答","reason":"证据边界说明","missing_information":[],"evidence_indices":[1],"confidence":0.0}
""".strip()
        user_payload = {
            "question": question,
            "options": options or {},
            "source_file": source_file,
            "controlled_draft_answer": draft_answer,
            "retrieved_evidence": evidence_blocks,
        }
        payload = post_json(
            self.endpoint,
            api_key=self.api_key,
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(user_payload, ensure_ascii=False),
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=self.timeout,
            service_name="Qwen 证据回答",
        )

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Qwen 证据回答返回结构无效") from exc
        parsed = _extract_json_object(str(content))
        if not parsed:
            raise RuntimeError("Qwen 证据回答没有返回有效 JSON")

        answer_type = str(parsed.get("answer_type") or "").strip().lower()
        if answer_type not in {"answer", "clarify", "refuse"}:
            raise RuntimeError(f"Qwen 证据回答类型无效：{answer_type}")
        answer = str(parsed.get("answer") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        if not answer:
            raise RuntimeError("Qwen 证据回答缺少 answer")
        raw_missing = parsed.get("missing_information")
        missing = (
            [str(item).strip() for item in raw_missing if str(item).strip()]
            if isinstance(raw_missing, list)
            else []
        )
        if answer_type == "answer" and re.search(
            r"(?:不能直接|无法(?:计算|确定|给出|判断)|未提供|不包含|证据不足|"
            r"需要(?:补充|明确|其他|外部).*(?:才能|方可)?)",
            answer,
        ):
            answer_type = (
                "clarify"
                if missing or re.search(r"需要(?:补充|明确|选择)", answer)
                else "refuse"
            )
        raw_indices = parsed.get("evidence_indices")
        indices: list[int] = []
        if isinstance(raw_indices, list):
            for value in raw_indices:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= index <= len(evidence) and index not in indices:
                    indices.append(index)
        try:
            confidence = min(1.0, max(0.0, float(parsed.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

        if answer_type == "answer" and not indices:
            raise RuntimeError("Qwen 给出答案但没有引用任何检索证据")
        return GroundedAnswer(
            answer_type=answer_type,
            answer=answer,
            reason=reason,
            missing_information=missing,
            evidence_indices=indices,
            confidence=confidence,
            backend=f"qwen:{self.model}",
        )
