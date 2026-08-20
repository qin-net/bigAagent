from __future__ import annotations

import json
from pathlib import Path

import pytest

from insightagent.business_contracts import Report
from insightagent.contracts import LLMResponse, LLMToolCall
from insightagent.data_contracts import (
    IndicatorSnapshot,
    KlineBar,
    KlineSnapshot,
    PriceSnapshot,
)
from insightagent.llm import FakeLLMAdapter
from insightagent.persistence import FileArtifactStore, SQLiteDatabase
from insightagent.resources import FunctionResource
from insightagent.runtime import AgentInstance, RuntimeConfig
from insightagent.technical_agent import (
    TechnicalToolContext,
    technical_runtime_config,
    register_technical_tools,
)


def _indicator_snapshot() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        stock_code="000858",
        ma5=118.5,
        ma10=117.2,
        ma20=115.0,
        ma60=110.3,
        macd=1.2,
        macd_signal=0.8,
        macd_hist=0.4,
        rsi14=55.0,
        volume_ratio=1.1,
        bars_used=60,
    )


def _price_snapshot() -> PriceSnapshot:
    return PriceSnapshot(stock_code="000858", price=118.8, pre_close=117.0)


def _kline_snapshot() -> KlineSnapshot:
    bars = [
        KlineBar(
            date="2026-08-17",
            open=117.5,
            high=119.0,
            low=117.0,
            close=118.8,
            volume=1200000,
        ),
        KlineBar(
            date="2026-08-16",
            open=116.0,
            high=118.5,
            low=115.8,
            close=117.0,
            volume=1100000,
        ),
    ]
    return KlineSnapshot(stock_code="000858", bars=bars, bars_used=60, last_close=117.0)


def _fake_llm_success() -> FakeLLMAdapter:
    final_args = json.dumps(
        {
            "status": "completed",
            "output": {
                "report": {
                    "role": "technical",
                    "score": 4,
                    "stance": "hold",
                    "summary": "均线多头排列，MA20 上方运行，趋势偏多。",
                    "trend": "均线多头排列",
                    "setup": "MACD 零轴上方",
                    "key_levels": "近期高点 119.00；近期低点 115.80；MA20 115.00",
                    "citations": [
                        {
                            "ref_id": "f1",
                            "kind": "field",
                            "id": "ma5",
                            "observed_at": "2026-08-17T00:00:00Z",
                            "source": "get_indicator_snapshot",
                            "note": None,
                        }
                    ],
                    "risks": ["量能未明显放大"],
                }
            },
            "reflection": {
                "what_worked": ["read indicator first"],
                "what_was_missing": [],
                "process_errors": [],
            },
            "state_patch": {
                "set": {"private_memory.memory_summary": "多头趋势"},
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
                        name="get_indicator_snapshot",
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


def _fake_llm_abstain() -> FakeLLMAdapter:
    final_args = json.dumps(
        {
            "status": "abstained",
            "output": {
                "report": {
                    "role": "technical",
                    "score": 3,
                    "stance": "abstain",
                    "summary": "K 线不足，无法形成技术面判断。",
                    "citations": [],
                    "risks": ["K 线数据不足，时机不明"],
                    "degraded": True,
                    "abstain": True,
                    "missing_information": ["bars"],
                }
            },
            "reflection": {
                "what_worked": [],
                "what_was_missing": ["sufficient bars"],
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
                        name="get_indicator_snapshot",
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
async def test_technical_agent_returns_valid_report(tmp_path: Path):
    db = SQLiteDatabase(str(tmp_path / "test.db"))
    await db.initialize()
    store = FileArtifactStore(db, str(tmp_path / "artifacts"))

    agent = AgentInstance(
        name="technical",
        llm_adapter=_fake_llm_success(),
        config=technical_runtime_config(),
    )
    ctx = TechnicalToolContext(
        indicator=_indicator_snapshot(),
        price=_price_snapshot(),
        kline=_kline_snapshot(),
        artifacts=store,
    )
    register_technical_tools(agent, ctx)

    result = await agent.run(
        "Analyze 000858",
        session_id="tech-1",
        business_context={"stock_code": "000858"},
    )
    report = Report.model_validate(result.output["report"])
    assert report.role == "technical"
    assert report.trend == "均线多头排列"
    assert report.setup == "MACD 零轴上方"
    assert "MA20" in report.key_levels
    assert report.score == 4
    assert report.stance == "hold"


@pytest.mark.asyncio
async def test_technical_agent_abstains_when_insufficient_bars(tmp_path: Path):
    db = SQLiteDatabase(str(tmp_path / "test.db"))
    await db.initialize()
    store = FileArtifactStore(db, str(tmp_path / "artifacts"))

    indicator = IndicatorSnapshot(
        stock_code="000858",
        ma5=118.5,
        ma10=117.2,
        ma20=None,
        bars_used=10,
    )
    agent = AgentInstance(
        name="technical",
        llm_adapter=_fake_llm_abstain(),
        config=technical_runtime_config(),
    )
    ctx = TechnicalToolContext(
        indicator=indicator,
        price=_price_snapshot(),
        kline=_kline_snapshot(),
        artifacts=store,
    )
    register_technical_tools(agent, ctx)

    result = await agent.run(
        "Analyze 000858",
        session_id="tech-2",
        business_context={"stock_code": "000858"},
    )
    report = Report.model_validate(result.output["report"])
    assert report.abstain is True
    assert report.stance == "abstain"
    assert report.degraded is True
