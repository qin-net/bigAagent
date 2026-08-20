from __future__ import annotations

import json
from pathlib import Path

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
from insightagent.persistence import FileArtifactStore, SQLiteDatabase
from insightagent.resources import FunctionResource
from insightagent.runtime import AgentInstance, RuntimeConfig
from insightagent.sentiment_agent import (
    SentimentToolContext,
    sentiment_runtime_config,
    register_sentiment_tools,
)


def _event_snapshot_with_reduction() -> EventSnapshot:
    return EventSnapshot(
        stock_code="000858",
        events=[
            EventItem(
                event_id="000858-red-1",
                event_type="reduction",
                title="控股股东拟减持不超过 2% 股份",
                published_at="2026-08-15",
                source="announcement",
            ),
            EventItem(
                event_id="000858-inq-1",
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
                            "id": "000858-red-1",
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
async def test_sentiment_agent_flags_reduction_and_inquiry(tmp_path: Path):
    db = SQLiteDatabase(str(tmp_path / "test.db"))
    await db.initialize()
    store = FileArtifactStore(db, str(tmp_path / "artifacts"))

    agent = AgentInstance(
        name="sentiment",
        llm_adapter=_fake_llm_with_events(),
        config=sentiment_runtime_config(),
    )
    ctx = SentimentToolContext(
        events=_event_snapshot_with_reduction(),
        holders=_holder_reduction(),
        artifacts=store,
    )
    register_sentiment_tools(agent, ctx)

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
async def test_sentiment_agent_abstains_when_no_material_event(tmp_path: Path):
    db = SQLiteDatabase(str(tmp_path / "test.db"))
    await db.initialize()
    store = FileArtifactStore(db, str(tmp_path / "artifacts"))

    agent = AgentInstance(
        name="sentiment",
        llm_adapter=_fake_llm_no_events(),
        config=sentiment_runtime_config(),
    )
    ctx = SentimentToolContext(
        events=_event_snapshot_empty(),
        holders=_holder_empty(),
        artifacts=store,
    )
    register_sentiment_tools(agent, ctx)

    result = await agent.run(
        "Analyze 000858 sentiment",
        session_id="sent-2",
        business_context={"stock_code": "000858"},
    )
    report = Report.model_validate(result.output["report"])
    assert report.abstain is True
    assert report.stance == "abstain"
    assert report.degraded is True
