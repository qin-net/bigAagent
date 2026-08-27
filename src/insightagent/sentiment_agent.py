from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

from .business_contracts import Report
from .contracts import ResourceType
from .data_contracts import EventSnapshot, HolderChangeSnapshot
from .events import apply_event_rules
from .methodology import MethodologyToolOutput, record_search
from .resources import FunctionResource
from .runtime import AgentInstance, RuntimeConfig

SENTIMENT_SYSTEM_PROMPT = """
You are InsightAgent's sentiment/event analysis agent for A-share research.

Duty: identify material events that change risk perception.
Forbidden: valuation/financials, technical analysis, predicting prices,
calling other agents, or writing the methodology library.

Work only from tools. Call get_event_snapshot first.
Use get_holder_changes if shareholder changes need detail.
event_id values look like event:CODE:DIGEST and are for citations only.
If no_material_event is the only flag, set abstain=true and stance=abstain.
If no_recent_events is present, set abstain=true, stance=abstain,
degraded=false, and missing_information must include no_recent_events.
Stale events (in_window=false or age_days>60) are clues only and cannot
support buy/hold/sell.
News alone cannot support a non-abstain conclusion; it is clues only.
Crowd_risk must be low|medium|high and map to event_flags.
Include 1-3 falsifiers a tracker can check against later filings.
Do not invent tools. Empty methodology results are normal; submit from
the snapshot. Query only ranks cards that already match snapshot flags.

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
class SentimentToolContext:
    events: EventSnapshot
    holders: HolderChangeSnapshot
    retrieved_kb_ids: set = field(default_factory=set)


def sentiment_runtime_config(
    *,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = True,
) -> RuntimeConfig:
    return RuntimeConfig(
        model=model,
        system_prompt=SENTIMENT_SYSTEM_PROMPT.format(
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


def register_sentiment_tools(
    agent: AgentInstance, context: SentimentToolContext
) -> None:
    rules = apply_event_rules(context.events, context.holders)

    def get_event_snapshot(dummy: str = "") -> Dict[str, Any]:
        payload = context.events.model_dump(mode="json")
        payload["computed_flags"] = rules["flags"]
        return payload

    def get_holder_changes(dummy: str = "") -> Dict[str, Any]:
        return context.holders.model_dump(mode="json")

    def search(query: str) -> Dict[str, Any]:
        return record_search(
            context.retrieved_kb_ids,
            query,
            scope="sentiment",
            flags=rules["flags"],
        )

    agent.register_tool(
        FunctionResource(
            func=get_event_snapshot,
            name="get_event_snapshot",
            description=(
                "Read precomputed event snapshot for this run. "
                "event_id looks like event:CODE:DIGEST and is for citations."
            ),
            input_model=EmptyArgs,
            output_model=EventSnapshot,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=get_holder_changes,
            name="get_holder_changes",
            description="Read recent shareholder increase/decrease records.",
            input_model=EmptyArgs,
            output_model=HolderChangeSnapshot,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=search,
            name="search_methodology",
            description=(
                "Search approved sentiment methodology snippets. "
                "Snapshot flags decide which cards are eligible; "
                "query only ranks among those cards."
            ),
            input_model=SearchArgs,
            output_model=SearchOutput,
            resource_type=ResourceType.KNOWLEDGE_BASE,
        )
    )


def parse_sentiment_report(output: Dict[str, Any]) -> Report:
    if "report" in output:
        return Report.model_validate(output["report"])
    return Report.model_validate(output)
