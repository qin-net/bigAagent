from __future__ import annotations

import json
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import LLMMessage, LLMRequest
from .llm import LLMAdapter


class InvestorProfileNarrative(BaseModel):
    """Model-written investment persona, grounded only in stored behavior."""

    model_config = ConfigDict(extra="forbid")

    persona_title: str = Field(min_length=2, max_length=24)
    overview: str = Field(min_length=12, max_length=300)
    strategy_preferences: List[str] = Field(min_length=1, max_length=6)
    decision_style: str = Field(min_length=4, max_length=160)
    risk_tendency: Literal[
        "conservative", "balanced", "aggressive", "barbell", "unclear"
    ]
    strengths: List[str] = Field(default_factory=list, max_length=4)
    blind_spots: List[str] = Field(default_factory=list, max_length=4)
    evidence_basis: List[str] = Field(min_length=1, max_length=6)
    confidence: Literal["low", "medium", "high"]


SYSTEM_PROMPT = """你是投资研究产品中的用户画像分析器。
你只根据给出的结构化行为证据描述用户的投资偏好，不做选股，不给投资建议。

要求：
1. 输出真正可读的投资者画像，而不是复述字段和次数。
2. persona_title 要简洁、鲜明、有辨识度，例如“现金流纪律型投资者”“剑走偏锋的景气猎手”；但不得夸张、羞辱或诊断人格。
3. 明确偏好的选股策略、决策方式、风险倾向、优势和可能盲点。
4. 区分用户明确表达的偏好与根据持仓/行为推断的倾向；证据不足必须写 unclear 或降低 confidence。
5. 不推断年龄、性别、职业、财富、健康、政治等敏感身份。
6. 专家报告不是用户偏好。输入不含用户原话，禁止编造。
7. evidence_basis 写自然语言依据，不写数据库字段名。
8. 只输出符合给定 schema 的 JSON，不要 Markdown。
"""


def profile_evidence(
    aggregate: Dict[str, Any], paper: Dict[str, Any]
) -> Dict[str, Any]:
    equity = float(paper.get("equity") or 0)
    positions = []
    for item in paper.get("positions") or []:
        market_value = float(item.get("market_value") or 0)
        positions.append(
            {
                "stock_code": item.get("stock_code"),
                "name": item.get("name"),
                "portfolio_weight": (
                    round(market_value / equity, 4) if equity else None
                ),
                "unrealized_positive": (
                    float(item.get("unrealized") or 0) >= 0
                ),
            }
        )
    return {
        "remembered_preferences": [
            {
                "scope": item.get("scope"),
                "stock_code": item.get("stock_code"),
                "kind": item.get("kind"),
                "statement": item.get("statement"),
            }
            for item in aggregate.get("preferences") or []
        ],
        "selection_memories": [
            {
                "stock_code": item.get("stock_code"),
                "statement": item.get("statement"),
            }
            for item in paper.get("picks") or []
        ],
        "behavior_summary": {
            "events": aggregate.get("utterance_count") or 0,
            "effects": aggregate.get("effects") or {},
            "dimensions_used": aggregate.get("dims") or {},
            "stocks_researched": aggregate.get("stocks") or [],
        },
        "paper_portfolio": {
            "invested_ratio": (
                round(float(paper.get("market_value") or 0) / equity, 4)
                if equity
                else 0
            ),
            "positions": positions,
        },
    }


def has_profile_evidence(evidence: Dict[str, Any]) -> bool:
    behavior = evidence["behavior_summary"]
    portfolio = evidence["paper_portfolio"]
    return bool(
        evidence["remembered_preferences"]
        or evidence["selection_memories"]
        or behavior["events"]
        or portfolio["positions"]
    )


async def generate_investor_profile(
    *,
    llm_adapter: LLMAdapter,
    model: str,
    aggregate: Dict[str, Any],
    paper: Dict[str, Any],
    user_id: str = "local",
) -> InvestorProfileNarrative:
    evidence = profile_evidence(aggregate, paper)
    if not has_profile_evidence(evidence):
        raise ValueError("insufficient user behavior for profile")
    request = LLMRequest(
        model=model,
        messages=[
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    "输出 schema：\n"
                    + json.dumps(
                        InvestorProfileNarrative.model_json_schema(),
                        ensure_ascii=False,
                    )
                    + "\n\n结构化行为证据：\n"
                    + json.dumps(evidence, ensure_ascii=False)
                ),
            ),
        ],
        thinking_enabled=False,
        response_format="json",
        max_tokens=1800,
        user_id=user_id,
    )
    response = await llm_adapter.complete(request)
    raw = (response.content or "").strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```")
        raw = raw.removesuffix("```").strip()
    return InvestorProfileNarrative.model_validate(json.loads(raw))
