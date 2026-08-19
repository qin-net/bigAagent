import json

import pytest

from insightagent.contracts import LLMResponse, LLMToolCall, ResourceCall
from insightagent.llm import FakeLLMAdapter
from insightagent.market import (
    FixtureMarketClient,
    FlakyMarketClient,
    MarketService,
    bars_from_value_rows,
    compute_indicators,
    em_dot_symbol,
    em_report_symbol,
    latest_record,
    scale_accounting_amount,
    to_float,
)
from insightagent.resources import CallOrchestrator, ResourceRegistry
from insightagent.retry import ExponentialBackoff, RetryConfig, RetryExhaustedError
from insightagent.runtime import AgentInstance, LoopTracer, RuntimeConfig
from insightagent.tools import build_market_tools


def _service():
    return MarketService(FixtureMarketClient())


def _tools(service=None):
    return {tool.spec.name: tool for tool in build_market_tools(service or _service())}


@pytest.mark.asyncio
async def test_each_market_tool_returns_typed_snapshot():
    tools = _tools()
    profile = await tools["fetch_stock_profile"].invoke(
        {"stock_code": "000858"}, idempotency_key="1"
    )
    assert profile["company_name"] == "五粮液"
    assert profile["industry"] == "白酒"

    quote = await tools["fetch_quote"].invoke(
        {"stock_code": "000858"}, idempotency_key="2"
    )
    assert quote["price"] is not None
    assert quote["volume"] is not None

    valuation = await tools["fetch_valuation"].invoke(
        {"stock_code": "000858"}, idempotency_key="3"
    )
    assert valuation["pe"] == 18.5
    assert valuation["pe_percentile_5y"] == 45.0

    financials = await tools["fetch_financials"].invoke(
        {"stock_code": "000858"}, idempotency_key="4"
    )
    assert financials["roe"] == 16.8
    assert financials["debt_ratio"] == 32.1

    cashflow = await tools["fetch_cashflow"].invoke(
        {"stock_code": "000858"}, idempotency_key="5"
    )
    assert cashflow["operating_cf"] == -12.4
    assert cashflow["profit_yoy"] == 8.5

    price = await tools["get_price_snapshot"].invoke(
        {"stock_code": "000858"}, idempotency_key="6"
    )
    assert price["price"] == quote["price"]

    indicators = await tools["get_indicator_snapshot"].invoke(
        {"stock_code": "000858"}, idempotency_key="7"
    )
    assert indicators["ma5"] is not None
    assert indicators["ma20"] is not None
    assert indicators["rsi14"] is not None
    assert indicators["bars_used"] == 80

    events = await tools["get_event_snapshot"].invoke(
        {"stock_code": "000858"}, idempotency_key="8"
    )
    types = {item["event_type"] for item in events["events"]}
    assert "reduction" in types
    assert "buyback" in types

    macro = await tools["get_macro_snapshot"].invoke({}, idempotency_key="9")
    assert macro["lpr_1y"] == 3.0

    search = await tools["search_announcements"].invoke(
        {"stock_code": "000858", "query": "减持", "limit": 5},
        idempotency_key="10",
    )
    assert search["hits"]
    assert "减持" in search["hits"][0]["title"]

    diff = await tools["diff_snapshot"].invoke(
        {
            "base": {"pe": 18.5, "roe": 16.8},
            "current": {"pe": 21.0, "roe": 16.8},
        },
        idempotency_key="11",
    )
    assert diff["unchanged_count"] == 1
    assert diff["changed_fields"][0]["field"] == "pe"

    news = await tools["search_news"].invoke(
        {"stock_code": "000858", "query": "库存", "limit": 5},
        idempotency_key="12",
    )
    assert news["hits"]
    holders = await tools["get_holder_changes"].invoke(
        {"stock_code": "000858"}, idempotency_key="13"
    )
    assert holders["items"]
    kline = await tools["get_kline_snapshot"].invoke(
        {"stock_code": "000858", "limit": 10}, idempotency_key="14"
    )
    assert kline["last_close"] is not None
    assert len(kline["bars"]) == 10
    assert kline["bars_used"] >= 10
    fundamental = await tools["get_fundamental_snapshot"].invoke(
        {"stock_code": "000858"}, idempotency_key="15"
    )
    assert fundamental["company_name"] == "五粮液"
    assert "cashflow_lag" in fundamental["computed_flags"]


def test_scale_and_value_row_indicator_fallback():
    assert scale_accounting_amount(-12.4) == -12.4
    assert scale_accounting_amount(359005459.04) == 3.5901
    assert to_float("80.63亿") == 8_063_000_000.0
    assert to_float("增持129.53万") == 1_295_300.0
    assert em_report_symbol("000858") == "SZ000858"
    assert em_dot_symbol("000858") == "000858.SZ"
    newest_first = [
        {"报告期": "2026-03-31", "净利润": "80.63亿"},
        {"报告期": "1998-12-31", "净利润": "3.59亿"},
    ]
    oldest_first = list(reversed(newest_first))
    assert latest_record(newest_first)["净利润"] == "80.63亿"
    assert latest_record(oldest_first)["净利润"] == "80.63亿"
    rows = [
        {"数据日期": "2026-01-{:02d}".format(index + 1), "当日收盘价": 10 + index * 0.1}
        for index in range(80)
    ]
    snapshot = compute_indicators("000858", bars_from_value_rows(rows))
    assert snapshot.ma5 is not None
    assert snapshot.rsi14 is not None
    assert snapshot.bars_used == 80


@pytest.mark.asyncio
async def test_indicator_computation_uses_code_not_model():
    service = _service()
    bars = await service.client.hist_daily("000858")
    snapshot = compute_indicators("000858", bars)
    assert snapshot.ma5 is not None
    assert snapshot.macd is not None or snapshot.bars_used < 26


@pytest.mark.asyncio
async def test_orchestrator_retries_flaky_quote_with_backoff():
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    inner = FixtureMarketClient()
    flaky = FlakyMarketClient(inner, fail_times=2)
    service = MarketService(flaky)
    tools = _tools(service)
    registry = ResourceRegistry()
    registry.register(tools["fetch_quote"])
    orchestrator = CallOrchestrator(
        registry,
        {
            "market": ExponentialBackoff(
                RetryConfig(
                    max_retries=3,
                    base_delay=1.0,
                    backoff_factor=2.0,
                    jitter_min=0.0,
                    jitter_max=0.0,
                ),
                sleep=fake_sleep,
            )
        },
    )

    results = await orchestrator.dispatch_calls(
        [
            ResourceCall(
                call_id="q1",
                resource_name="fetch_quote",
                arguments={"stock_code": "000858"},
            )
        ]
    )

    assert results["q1"].status == "success"
    assert results["q1"].data["price"] is not None
    assert results["q1"].attempts == 3
    assert flaky.calls == 3
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_orchestrator_backoff_exhausts():
    async def noop(_delay):
        return None

    flaky = FlakyMarketClient(FixtureMarketClient(), fail_times=10)
    registry = ResourceRegistry()
    registry.register(_tools(MarketService(flaky))["fetch_quote"])
    orchestrator = CallOrchestrator(
        registry,
        {
            "market": ExponentialBackoff(
                RetryConfig(max_retries=1, jitter_min=0, jitter_max=0),
                sleep=noop,
            )
        },
    )
    with pytest.raises(RetryExhaustedError):
        await orchestrator.dispatch_calls(
            [
                ResourceCall(
                    call_id="q1",
                    resource_name="fetch_quote",
                    arguments={"stock_code": "000858"},
                )
            ]
        )


class ScriptedMarketLLM:
    def __init__(self) -> None:
        self.phase = "tools"
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if self.phase == "tools":
            self.phase = "final"
            return LLMResponse(
                id="r1",
                model="fake",
                content="need price and macro",
                reasoning_content="call two tools in parallel",
                tool_calls=[
                    LLMToolCall(
                        id="c-price",
                        name="get_price_snapshot",
                        arguments='{"stock_code":"000858"}',
                    ),
                    LLMToolCall(
                        id="c-macro",
                        name="get_macro_snapshot",
                        arguments="{}",
                    ),
                ],
                finish_reason="tool_calls",
            )
        payload = {
            "status": "completed",
            "output": {
                "summary": "price and LPR loaded",
                "used_tools": ["get_price_snapshot", "get_macro_snapshot"],
            },
            "reflection": {"what_worked": ["parallel market tools"]},
            "state_patch": {
                "set": {"private_memory.memory_summary": "market tools ok"},
                "append": {},
                "remove": {},
            },
        }
        self.phase = "done"
        return LLMResponse(
            id="r2",
            model="fake",
            content=json.dumps(payload),
            reasoning_content="compose from tool results",
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_scheduler_loop_calls_market_tools_and_traces_stages():
    tracer = LoopTracer()
    llm = ScriptedMarketLLM()
    agent = AgentInstance(
        name="data",
        llm_adapter=llm,
        config=RuntimeConfig(
            system_prompt="Call market tools then return JSON.",
            max_loop_round=4,
        ),
        tracer=tracer,
    )
    for tool in build_market_tools(_service()):
        agent.register_tool(tool)

    result = await agent.run(
        "Inspect 000858 price and macro",
        session_id="loop-1",
        business_context={"stock_code": "000858"},
    )

    assert result.output["used_tools"] == [
        "get_price_snapshot",
        "get_macro_snapshot",
    ]
    assert tracer.stages() == [
        "loop_start",
        "llm_request",
        "llm_response",
        "scheduler_dispatch",
        "tool_results",
        "state_checkpoint",
        "llm_request",
        "llm_response",
        "final_response",
        "loop_complete",
    ]
    dispatch = next(
        event for event in tracer.events if event["stage"] == "scheduler_dispatch"
    )
    assert {item["resource_name"] for item in dispatch["payload"]} == {
        "get_price_snapshot",
        "get_macro_snapshot",
    }
    results = next(
        event for event in tracer.events if event["stage"] == "tool_results"
    )
    assert results["payload"]["c-price"]["status"] == "success"
    assert results["payload"]["c-macro"]["data"]["lpr_1y"] == 3.0
    final = next(
        event for event in tracer.events if event["stage"] == "final_response"
    )
    assert final["payload"]["output"]["summary"] == "price and LPR loaded"
