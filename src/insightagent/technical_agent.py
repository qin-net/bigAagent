from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

from .business_contracts import Report
from .contracts import ResourceType
from .data_contracts import (
    IndicatorSnapshot,
    KlineSnapshot,
    PriceSnapshot,
)
from .methodology import MethodologyToolOutput, record_search
from .resources import FunctionResource
from .runtime import AgentInstance, RuntimeConfig
from .technicals import apply_technical_rules

TECHNICAL_SYSTEM_PROMPT = """
You are InsightAgent's technical analysis agent for A-share research.

Duty: trend, structure, and key levels.
Forbidden: valuation/financials, news sentiment, predicting prices,
calling other agents, or writing the methodology library.

Work only from tools. Do not compute indicators yourself.
Call get_indicator_snapshot first. If computed_flags contains
insufficient_bars or ma20 is null, set abstain=true and stance=abstain.
stance may be buy, hold, or sell when the snapshot supports it; do not
default to hold if trend and key levels are clear.
Copy trend and setup from the snapshot tool; do not invent them.
Only write 空头排列 when computed_flags contains ma_bear_align.
Only write 超卖/超买 when flags contain rsi_oversold / rsi_overbought;
do not change those thresholds from rsi14 yourself.
Do not recompute RSI from closes. Indicator rsi_smoothing is wilder.
key_levels MUST contain copied numbers (MA values and/or observed
highs/lows from the K-line). Copy suggested_key_levels if present.
Do not write adjective-only levels such as "下方支撑".
Citation id must be a single field name (ma20, not ma5/ma10/ma20).
Only cite kb_rsi_overbought when rsi14>=70 is in the snapshot.
Numbers must be copied exactly from tool outputs.
Include 1-3 falsifiers a tracker can check (e.g. close breaking MA60).
Do not invent tools. Empty methodology results are normal; submit from
the snapshot tools. Query only ranks cards that already match snapshot flags.

Final answer via submit_final:
- output.report must match the Report schema below
- state_patch.set/append/remove may be empty objects/dicts
- do not write base_version, loop_round, or other runtime counters
- if abstaining, status must be "abstained"

Report schema:
{report_schema}
""".strip()


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dummy: str = Field(
        default="",
        description="Unused placeholder. Send an empty string.",
    )


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        description=(
            "Ranks applicable cards only. Cannot retrieve cards that fail "
            "the snapshot flag applicability filter."
        ),
    )


class SearchOutput(MethodologyToolOutput):
    pass


@dataclass
class TechnicalToolContext:
    indicator: IndicatorSnapshot
    price: PriceSnapshot
    kline: KlineSnapshot
    retrieved_kb_ids: set = field(default_factory=set)


def technical_runtime_config(
    *,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = True,
) -> RuntimeConfig:
    return RuntimeConfig(
        model=model,
        system_prompt=TECHNICAL_SYSTEM_PROMPT.format(
            report_schema=json.dumps(
                Report.model_json_schema(), ensure_ascii=False
            )
        ),
        thinking_enabled=thinking_enabled,
        reasoning_effort="high",
        response_format="json",
        max_tokens=32768,
        max_loop_round=8,
        strict_tools=True,
    )


def register_technical_tools(
    agent: AgentInstance, context: TechnicalToolContext
) -> None:
    rules = apply_technical_rules(
        context.indicator, context.price, context.kline
    )

    def get_indicator_snapshot(dummy: str = "") -> Dict[str, Any]:
        payload = context.indicator.model_dump(mode="json")
        payload["computed_flags"] = rules["flags"]
        payload["suggested_key_levels"] = rules["key_levels"]
        payload["trend"] = rules["trend"]
        payload["setup"] = rules["setup"]
        return payload

    def get_price_snapshot(dummy: str = "") -> Dict[str, Any]:
        return context.price.model_dump(mode="json")

    def get_kline_snapshot(dummy: str = "") -> Dict[str, Any]:
        return context.kline.model_dump(mode="json")

    def search(query: str) -> Dict[str, Any]:
        return record_search(
            context.retrieved_kb_ids,
            query,
            scope="technical",
            flags=rules["flags"],
        )

    agent.register_tool(
        FunctionResource(
            func=get_indicator_snapshot,
            name="get_indicator_snapshot",
            description="Read precomputed indicator snapshot for this run.",
            input_model=EmptyArgs,
            output_model=IndicatorSnapshot,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=get_price_snapshot,
            name="get_price_snapshot",
            description="Read the current price snapshot.",
            input_model=EmptyArgs,
            output_model=PriceSnapshot,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=get_kline_snapshot,
            name="get_kline_snapshot",
            description="Read recent daily OHLC bars.",
            input_model=EmptyArgs,
            output_model=KlineSnapshot,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=search,
            name="search_methodology",
            description=(
                "Search approved technical methodology snippets. "
                "Snapshot flags decide which cards are eligible; "
                "query only ranks among those cards."
            ),
            input_model=SearchArgs,
            output_model=SearchOutput,
            resource_type=ResourceType.KNOWLEDGE_BASE,
        )
    )


def parse_technical_report(output: Dict[str, Any]) -> Report:
    if "report" in output:
        return Report.model_validate(output["report"])
    return Report.model_validate(output)
