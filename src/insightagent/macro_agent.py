from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

from .business_contracts import Report
from .contracts import ResourceType
from .data_contracts import MacroSnapshot
from .fundamentals import search_methodology
from .macros import apply_macro_rules
from .persistence import FileArtifactStore
from .resources import FunctionResource
from .runtime import AgentInstance, RuntimeConfig


MACRO_SYSTEM_PROMPT = """
You are InsightAgent's macro analysis agent for A-share research.

Duty: describe the current rate environment and decide whether it is relevant
to this stock's industry. You are not a trading signal agent.
Forbidden: valuation/financials, technical indicators, news, announcements,
price forecasts, and investment buy/sell recommendations.

Call get_macro_snapshot first. If computed_flags contains lpr_missing or
low_relevance, abstain=true and stance=abstain. Otherwise stance must be hold.
Copy LPR and Shibor values exactly from the tool; never round or invent values.
cycle_tag may only be rate_data_available or insufficient. market_bias may only
be neutral or unclear. A non-abstained report must contain citations.

Final answer via submit_final:
- output.report must match the Report schema below
- do not write base_version, loop_round, or runtime counters
- if abstaining, status must be "abstained"

Report schema:
{report_schema}
""".strip()


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dummy: str = Field(default="", description="Unused placeholder.")


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)


class SearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: List[Dict[str, str]]


class ArtifactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)


class ArtifactOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    content: str


class MacroToolSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    macro: MacroSnapshot
    industry: str = ""
    stock_code: str
    company_name: str = ""
    computed_flags: List[str] = Field(default_factory=list)


@dataclass
class MacroToolContext:
    macro: MacroSnapshot
    industry: str
    stock_code: str
    company_name: str
    artifacts: FileArtifactStore


def macro_runtime_config(
    *, model: str = "deepseek-v4-flash", thinking_enabled: bool = True
) -> RuntimeConfig:
    return RuntimeConfig(
        model=model,
        system_prompt=MACRO_SYSTEM_PROMPT.format(
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


def register_macro_tools(agent: AgentInstance, context: MacroToolContext) -> None:
    rules = apply_macro_rules(context.macro, context.industry)

    def get_macro_snapshot(dummy: str = "") -> Dict[str, Any]:
        payload = {
            "macro": context.macro.model_dump(mode="json"),
            "industry": context.industry,
            "stock_code": context.stock_code,
            "company_name": context.company_name,
            "computed_flags": rules["flags"],
        }
        return payload

    def search(query: str) -> Dict[str, Any]:
        return {"entries": search_methodology(query)}

    async def get_artifact(ref: str) -> Dict[str, Any]:
        content = await context.artifacts.get(ref)
        return {"ref": ref, "content": content}

    agent.register_tool(
        FunctionResource(
            func=get_macro_snapshot,
            name="get_macro_snapshot",
            description="Read the precomputed macro snapshot for this run.",
            input_model=EmptyArgs,
            output_model=MacroToolSnapshot,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=search,
            name="search_methodology",
            description="Search approved macro methodology snippets.",
            input_model=SearchArgs,
            output_model=SearchOutput,
            resource_type=ResourceType.KNOWLEDGE_BASE,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=get_artifact,
            name="get_artifact",
            description="Load the original macro artifact by ref.",
            input_model=ArtifactArgs,
            output_model=ArtifactOutput,
        )
    )


def parse_macro_report(output: Dict[str, Any]) -> Report:
    if "report" in output:
        return Report.model_validate(output["report"])
    return Report.model_validate(output)
