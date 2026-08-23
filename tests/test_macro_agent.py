from __future__ import annotations

import json
from typing import Optional

import pytest

from insightagent.business_contracts import Report
from insightagent.contracts import LLMResponse, LLMToolCall
from insightagent.data_contracts import MacroSnapshot
from insightagent.evidence import EvidenceBindingError, bind_macro_report_evidence
from insightagent.llm import FakeLLMAdapter
from insightagent.macro_agent import (
    MacroToolContext,
    macro_runtime_config,
    register_macro_tools,
)
from insightagent.macros import apply_macro_rules
from insightagent.runtime import AgentInstance


def _macro_snapshot(*, lpr_1y: Optional[float] = 3.0) -> MacroSnapshot:
    return MacroSnapshot(
        lpr_1y=lpr_1y,
        lpr_5y=3.5,
        shibor_overnight=1.4,
        source="fixture",
    )


def test_macro_rules_classify_relevance_and_missing_lpr():
    available = _macro_snapshot()
    assert apply_macro_rules(available, "白酒")["flags"] == ["low_relevance"]
    assert apply_macro_rules(available, "")["flags"] == ["low_relevance"]
    assert apply_macro_rules(available, "银行")["flags"] == ["rate_sensitive"]
    assert "lpr_missing" in apply_macro_rules(
        _macro_snapshot(lpr_1y=None), "银行"
    )["flags"]


def _fake_llm() -> FakeLLMAdapter:
    payload = {
        "status": "completed",
        "output": {
            "report": {
                "role": "macro",
                "score": 4,
                "stance": "hold",
                "summary": "LPR 1年期为3.0，5年期为3.5，隔夜Shibor为1.4；与银行业相关。",
                "citations": [
                    {
                        "ref_id": "lpr_1y",
                        "kind": "field",
                        "id": "lpr_1y",
                        "source": "get_macro_snapshot",
                    }
                ],
                "risks": ["利率数据仅反映当前快照"],
                "cycle_tag": "rate_data_available",
                "market_bias": "neutral",
                "relevance_to_stock": "high",
            }
        },
        "reflection": {"what_worked": ["read macro snapshot"]},
        "state_patch": {"set": {}, "append": {}, "remove": {}},
    }
    return FakeLLMAdapter(
        [
            LLMResponse(
                id="tool-1",
                model="fake",
                content="reading macro",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="get_macro_snapshot",
                        arguments='{"dummy":""}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="final-1",
                model="fake",
                content=json.dumps(payload, ensure_ascii=False),
                finish_reason="stop",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_macro_agent_reads_snapshot_then_returns_macro_report():
    agent = AgentInstance(
        name="macro", llm_adapter=_fake_llm(), config=macro_runtime_config()
    )
    register_macro_tools(
        agent,
        MacroToolContext(
            macro=_macro_snapshot(),
            industry="银行",
            stock_code="000001",
            company_name="平安银行",
        ),
    )

    result = await agent.run(
        "Analyze 000001", session_id="macro-1", business_context={"stock_code": "000001"}
    )
    report = Report.model_validate(result.output["report"])
    assert report.role == "macro"
    assert report.stance == "hold"
    assert report.relevance_to_stock == "high"


def test_macro_registers_only_snapshot_tools():
    agent = AgentInstance(
        name="macro", llm_adapter=_fake_llm(), config=macro_runtime_config()
    )
    register_macro_tools(
        agent,
        MacroToolContext(
            macro=_macro_snapshot(),
            industry="银行",
            stock_code="000001",
            company_name="平安银行",
        ),
    )
    names = {item.spec.name for item in agent.resource_registry.list_all()}
    assert names == {"get_macro_snapshot", "search_methodology"}


def test_macro_report_rejects_unbound_number():
    report = Report(
        role="macro",
        score=4,
        stance="hold",
        summary="LPR 1年期为2.9。",
        citations=[
            {
                "ref_id": "lpr_1y",
                "kind": "field",
                "id": "lpr_1y",
                "source": "get_macro_snapshot",
            }
        ],
        risks=["利率数据仅反映当前快照"],
        cycle_tag="rate_data_available",
        relevance_to_stock="high",
    )
    with pytest.raises(EvidenceBindingError):
        bind_macro_report_evidence(report, _macro_snapshot())


def test_macro_report_cannot_issue_buy_or_sell():
    with pytest.raises(ValueError, match="macro report stance"):
        Report(
            role="macro",
            score=4,
            stance="buy",
            summary="不应形成买入建议。",
            citations=[
                {
                    "ref_id": "lpr_1y",
                    "kind": "field",
                    "id": "lpr_1y",
                    "source": "get_macro_snapshot",
                }
            ],
        )
