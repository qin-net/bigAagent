from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .artifact_access import (
    ARTIFACT_TOOL_DESCRIPTION,
    ArtifactArgs,
    ArtifactOutput,
    load_whitelisted_artifact,
)
from .business_contracts import FundamentalSnapshot, Report
from .contracts import ResourceType
from .fundamentals import search_methodology
from .persistence import FileArtifactStore
from .resources import FunctionResource
from .runtime import AgentInstance, RuntimeConfig

PACKAGE_FIXTURES = Path(__file__).resolve().parent / "fixtures"

FUNDAMENTAL_SYSTEM_PROMPT = """
You are InsightAgent's fundamental analysis agent for A-share research.

Duty: value, financial quality, and margin of safety.
Forbidden: judging management integrity, predicting stock prices, technical
analysis, calling other agents, or writing the methodology library.

Work only from tools. Do not compute financial metrics yourself.
get_artifact is optional and only accepts this run's artifact:// SHA-256.
Never pass event_id, announcement id, or citation id. If get_artifact fails,
do not retry with another key; submit_final from snapshot tools.
Call get_fundamental_snapshot first. If computed_flags contains cashflow_lag,
you must cite kind=rule and id=cashflow_lag. Search methodology when flags or
missing fields need interpretation.

If required financial fields are missing, set abstain=true and stance=abstain.
Non-abstain reports need citations. Numbers in summary/valuation/
financial_health/earnings_quality must be copied from snapshot fields or
rule thresholds exactly as returned by tools. Do not round, truncate, or
drop decimal places (write 22.14548383, not 22 or 22.15). Years
(19xx/20xx), calendar dates, and the 1-5 score are allowed.

Final JSON must be submitted via the submit_final tool:
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

    query: str = Field(min_length=1)


class SearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: List[Dict[str, str]]


@dataclass
class FundamentalToolContext:
    snapshot: FundamentalSnapshot
    artifacts: FileArtifactStore


def fundamental_runtime_config(
    *,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = True,
) -> RuntimeConfig:
    return RuntimeConfig(
        model=model,
        system_prompt=FUNDAMENTAL_SYSTEM_PROMPT.format(
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


def register_fundamental_tools(
    agent: AgentInstance, context: FundamentalToolContext
) -> None:
    def get_snapshot(dummy: str = "") -> Dict[str, Any]:
        return context.snapshot.model_dump(mode="json")

    def search(query: str) -> Dict[str, Any]:
        return {"entries": search_methodology(query, scope="fundamental")}

    async def get_artifact(ref: str) -> Dict[str, Any]:
        return await load_whitelisted_artifact(
            context.artifacts, ref, context.snapshot.artifact_ref
        )

    agent.register_tool(
        FunctionResource(
            func=get_snapshot,
            name="get_fundamental_snapshot",
            description="Read the precomputed fundamental snapshot for this run.",
            input_model=EmptyArgs,
            output_model=FundamentalSnapshot,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=search,
            name="search_methodology",
            description=(
                "Search approved fundamental methodology snippets. "
                "Use when flags such as cashflow_lag need interpretation."
            ),
            input_model=SearchArgs,
            output_model=SearchOutput,
            resource_type=ResourceType.KNOWLEDGE_BASE,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=get_artifact,
            name="get_artifact",
            description=ARTIFACT_TOOL_DESCRIPTION,
            input_model=ArtifactArgs,
            output_model=ArtifactOutput,
        )
    )


def parse_report(output: Dict[str, Any]) -> Report:
    if "report" in output:
        return Report.model_validate(output["report"])
    return Report.model_validate(output)


def default_fixtures_dir(override: Optional[str] = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return PACKAGE_FIXTURES
