from __future__ import annotations

import json

import pytest

from insightagent.business_contracts import Report
from insightagent.contracts import LLMResponse, LLMToolCall
from insightagent.data_contracts import (
    EventItem,
    EventSnapshot,
    HolderChangeItem,
    HolderChangeSnapshot,
)
from insightagent.llm import FakeLLMAdapter
from insightagent.runtime import AgentInstance, LoopTracer
from insightagent.sentiment_agent import (
    SentimentToolContext,
    register_sentiment_tools,
    sentiment_runtime_config,
)


def _event_snapshot_with_reduction() -> EventSnapshot:
    return EventSnapshot(
        stock_code="000858",
        events=[
            EventItem(
                event_id="event:000858:red-1",
                event_type="reduction",
                title="控股股东拟减持不超过 2% 股份",
                published_at="2026-08-15",
                source="announcement",
            ),
            EventItem(
                event_id="event:000858:inq-1",
                event_type="inquiry",
                title="收到交易所问询函",
                published_at="2026-08-16",
                source="announcement",
            ),
        ],
    )


def _event_snapshot_empty() -> EventSnapshot:
    return EventSnapshot(stock_code="000858", events=[])


def _holder_reduction() -> HolderChangeSnapshot:
    return HolderChangeSnapshot(
        stock_code="000858",
        items=[
            HolderChangeItem(
                holder_name="控股股东",
                change_type="reduction",
                change_shares=15_000_000,
                published_at="2026-08-15",
                note="减持",
            )
        ],
    )


def _holder_empty() -> HolderChangeSnapshot:
    return HolderChangeSnapshot(stock_code="000858", items=[])


def _fake_llm_with_events() -> FakeLLMAdapter:
    final_args = json.dumps(
        {
            "status": "completed",
            "output": {
                "report": {
                    "role": "sentiment",
                    "score": 3,
                    "stance": "hold",
                    "summary": "本期出现减持和问询函，风险偏好下降。",
                    "event_flags": ["has_reduction", "has_inquiry", "holder_reduction"],
                    "crowd_risk": "high",
                    "citations": [
                        {
                            "ref_id": "e1",
                            "kind": "event",
                            "id": "event:000858:red-1",
                            "observed_at": "2026-08-15T00:00:00Z",
                            "source": "get_event_snapshot",
                            "note": None,
                        }
                    ],
                    "risks": ["减持可能释放估值压力", "问询函可能导致信披成本上升"],
                }
            },
            "reflection": {
                "what_worked": ["checked events first"],
                "what_was_missing": [],
                "process_errors": [],
            },
            "state_patch": {
                "set": {"private_memory.memory_summary": "减持+问询"},
                "append": {},
                "remove": {},
            },
        }
    )
    return FakeLLMAdapter(
        [
            LLMResponse(
                id="tool-1",
                model="deepseek-v4-pro",
                content="inspecting",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="get_event_snapshot",
                        arguments='{"dummy":""}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="final-1",
                model="deepseek-v4-pro",
                content=final_args,
                finish_reason="stop",
            ),
        ]
    )


def _fake_llm_no_events() -> FakeLLMAdapter:
    final_args = json.dumps(
        {
            "status": "abstained",
            "output": {
                "report": {
                    "role": "sentiment",
                    "score": 3,
                    "stance": "abstain",
                    "summary": "本期无重大事件，不做情绪判断。",
                    "citations": [],
                    "risks": ["事件真空期，无法评估情绪风险"],
                    "degraded": True,
                    "abstain": True,
                    "missing_information": ["events"],
                }
            },
            "reflection": {
                "what_worked": [],
                "what_was_missing": ["material events"],
                "process_errors": [],
            },
            "state_patch": {"set": {}, "append": {}, "remove": {}},
        }
    )
    return FakeLLMAdapter(
        [
            LLMResponse(
                id="tool-1",
                model="deepseek-v4-pro",
                content="inspecting",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="get_event_snapshot",
                        arguments='{"dummy":""}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="final-1",
                model="deepseek-v4-pro",
                content=final_args,
                finish_reason="stop",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_sentiment_agent_flags_reduction_and_inquiry():
    agent = AgentInstance(
        name="sentiment",
        llm_adapter=_fake_llm_with_events(),
        config=sentiment_runtime_config(),
    )
    register_sentiment_tools(
        agent,
        SentimentToolContext(
            events=_event_snapshot_with_reduction(),
            holders=_holder_reduction(),
        ),
    )
    result = await agent.run(
        "Analyze 000858 sentiment",
        session_id="sent-1",
        business_context={"stock_code": "000858"},
    )
    report = Report.model_validate(result.output["report"])
    assert report.role == "sentiment"
    assert "has_reduction" in report.event_flags
    assert "has_inquiry" in report.event_flags
    assert report.crowd_risk == "high"
    assert report.score == 3
    assert report.stance == "hold"


@pytest.mark.asyncio
async def test_sentiment_agent_abstains_when_no_material_event():
    agent = AgentInstance(
        name="sentiment",
        llm_adapter=_fake_llm_no_events(),
        config=sentiment_runtime_config(),
    )
    register_sentiment_tools(
        agent,
        SentimentToolContext(
            events=_event_snapshot_empty(),
            holders=_holder_empty(),
        ),
    )
    result = await agent.run(
        "Analyze 000858 sentiment",
        session_id="sent-2",
        business_context={"stock_code": "000858"},
    )
    report = Report.model_validate(result.output["report"])
    assert report.abstain is True
    assert report.stance == "abstain"
    assert report.degraded is True


def test_sentiment_registers_only_snapshot_tools():
    agent = AgentInstance(
        name="sentiment",
        llm_adapter=_fake_llm_with_events(),
        config=sentiment_runtime_config(),
    )
    register_sentiment_tools(
        agent,
        SentimentToolContext(
            events=_event_snapshot_with_reduction(),
            holders=_holder_reduction(),
        ),
    )
    names = {item.spec.name for item in agent.resource_registry.list_all()}
    assert names == {
        "get_event_snapshot",
        "get_holder_changes",
        "search_methodology",
    }


@pytest.mark.asyncio
async def test_sentiment_recovers_after_unknown_get_artifact():
    payload = json.dumps(
        {
            "status": "completed",
            "output": {
                "report": {
                    "role": "sentiment",
                    "score": 3,
                    "stance": "hold",
                    "summary": "本期出现减持和问询函，风险偏好下降。",
                    "event_flags": [
                        "has_reduction",
                        "has_inquiry",
                        "holder_reduction",
                    ],
                    "crowd_risk": "high",
                    "citations": [
                        {
                            "ref_id": "e1",
                            "kind": "event",
                            "id": "event:000858:red-1",
                            "observed_at": "2026-08-15T00:00:00Z",
                            "source": "get_event_snapshot",
                            "note": None,
                        }
                    ],
                    "risks": [
                        "减持可能释放估值压力",
                        "问询函可能导致信披成本上升",
                    ],
                }
            },
            "reflection": {"what_worked": ["ignored unknown tool"]},
            "state_patch": {"set": {}, "append": {}, "remove": {}},
        }
    )
    tracer = LoopTracer()
    fake = FakeLLMAdapter(
        [
            LLMResponse(
                id="tool-1",
                model="deepseek-v4-pro",
                content="inspecting",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="get_event_snapshot",
                        arguments='{"dummy":""}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="tool-2",
                model="deepseek-v4-pro",
                content="wrong tool",
                tool_calls=[
                    LLMToolCall(
                        id="call-2",
                        name="get_artifact",
                        arguments='{"ref":"event:000858:red-1"}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="final-1",
                model="deepseek-v4-pro",
                content=payload,
                finish_reason="stop",
            ),
        ]
    )
    agent = AgentInstance(
        name="sentiment",
        llm_adapter=fake,
        config=sentiment_runtime_config(),
        tracer=tracer,
    )
    register_sentiment_tools(
        agent,
        SentimentToolContext(
            events=_event_snapshot_with_reduction(),
            holders=_holder_reduction(),
        ),
    )
    result = await agent.run(
        "Analyze 000858 sentiment",
        session_id="sent-artifact",
        business_context={"stock_code": "000858"},
    )
    report = Report.model_validate(result.output["report"])
    assert report.stance == "hold"
    failed = [
        payload
        for event in tracer.events
        if event["stage"] == "tool_results"
        for payload in event["payload"].values()
        if isinstance(payload, dict) and payload.get("status") == "failed"
    ]
    assert failed
    assert failed[0]["error"]["type"] == "UnknownResourceError"
    assert "数据不足" not in report.summary


def test_methodology_is_scoped_by_dimension():
    from insightagent.fundamentals import search_methodology

    sentiment_hits = search_methodology("减持", scope="sentiment")
    assert any(item["id"] == "kb_event_reduction" for item in sentiment_hits)
    assert all(not item["id"].startswith("kb_cashflow") for item in sentiment_hits)
    assert search_methodology("减持", scope="fundamental") == []

    technical_hits = search_methodology("均线", scope="technical")
    assert any(item["id"] == "kb_ma_align" for item in technical_hits)
    assert search_methodology("均线", scope="sentiment") == []
