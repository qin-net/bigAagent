import json
import os

import pytest

from insightagent.contracts import AgentFinalResponse
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.market import FixtureMarketClient, MarketService
from insightagent.runtime import AgentInstance, LoopTracer, RuntimeConfig
from insightagent.tools import build_market_tools


pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_deepseek_calls_market_tools():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY is not set")

    tracer = LoopTracer()
    agent = AgentInstance(
        name="data",
        llm_adapter=DeepSeekChatAdapter(
            DeepSeekConfig(api_key=api_key, default_model="deepseek-v4-flash")
        ),
        config=RuntimeConfig(
            model="deepseek-v4-flash",
            system_prompt=(
                "You are a data agent. You MUST call get_price_snapshot for "
                "000858 and get_macro_snapshot, then return the final JSON. "
                "Put the price in output.price and LPR in output.lpr_1y."
            ),
            thinking_enabled=True,
            max_loop_round=6,
        ),
        tracer=tracer,
    )
    for tool in build_market_tools(MarketService(FixtureMarketClient())):
        agent.register_tool(tool)

    result = await agent.run(
        "Call get_price_snapshot and get_macro_snapshot for 000858, then stop.",
        session_id="live-tools-1",
        business_context={"stock_code": "000858"},
    )

    assert isinstance(result, AgentFinalResponse)
    dispatched = [
        event for event in tracer.events if event["stage"] == "scheduler_dispatch"
    ]
    called = {
        item["resource_name"]
        for event in dispatched
        for item in event["payload"]
    }
    assert "get_price_snapshot" in called or "fetch_quote" in called
    assert result.status in {"completed", "degraded", "abstained"}
    print("STAGES", tracer.stages())
    print("CALLED", sorted(called))
    print("OUTPUT", json.dumps(result.output, ensure_ascii=False)[:1000])
