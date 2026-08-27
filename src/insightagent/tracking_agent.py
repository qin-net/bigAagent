from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .contracts import ResourceType, SideEffect
from .methodology import (
    MethodologyCatalog,
    allowed_flags_for,
    read_whitelisted_markdown,
)
from .persistence import SQLiteContextArchive, SQLiteDatabase, SQLiteStateStore
from .resources import FunctionResource
from .runtime import AgentInstance, RuntimeConfig

DEFAULT_MARKDOWN_ROOTS = (
    Path("data/kb/markdown"),
    Path("tests/fixtures/kb"),
)

TRACKING_SYSTEM_PROMPT = """
You are InsightAgent's tracking agent.

You manage the methodology library. You do not analyze a single stock in
this task. You do not call fundamental, technical, sentiment, or macro
agents. You cannot approve entries or change analyst system prompts.

This round is distillation: read the whitelisted markdown chapter via
tools, map discipline onto frozen flag names, and submit_candidate only.
Do not copy long source text. text must stay short. Do not write buy/sell
instructions. If a card cannot map onto allowed flags, skip it.

Finish with submit_final. output.notes may summarize what you submitted.
""".strip()


class DistillFinalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str = ""


class MarkdownArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class MarkdownOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    text: str
    truncated: bool = False


class ScopeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dummy: str = ""


class FlagsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    flags: List[str]


class SearchExistingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)


class ExistingSearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: List[Dict[str, str]]


class CandidateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    type: str = "rule"
    trigger: str
    action: str = ""
    evidence_required: List[str]
    exceptions: List[str] = Field(default_factory=list)
    source_refs: List[str] = Field(default_factory=list)
    text: str
    priority: int = 50


class CandidateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    version: int
    error: str = ""


@dataclass
class DistillToolContext:
    catalog: MethodologyCatalog
    scope: str
    markdown_path: Path
    roots: Sequence[Path]
    submitted_ids: List[str] = field(default_factory=list)


def tracking_distill_config(
    *,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = False,
) -> RuntimeConfig:
    return RuntimeConfig(
        model=model,
        system_prompt=TRACKING_SYSTEM_PROMPT,
        thinking_enabled=thinking_enabled,
        reasoning_effort="high",
        response_format="json",
        max_tokens=8192,
        max_loop_round=8,
        strict_tools=True,
        final_output_model=DistillFinalOutput,
    )


def register_distill_tools(
    agent: AgentInstance, context: DistillToolContext
) -> None:
    def read_source(path: str) -> Dict[str, Any]:
        requested = path.strip() or str(context.markdown_path)
        text = read_whitelisted_markdown(requested, context.roots)
        return {
            "path": requested,
            "text": text,
            "truncated": len(text) >= 8000,
        }

    def list_flags(dummy: str = "") -> Dict[str, Any]:
        return {
            "scope": context.scope,
            "flags": allowed_flags_for(context.scope),
        }

    def search_existing(query: str) -> Dict[str, Any]:
        entries = context.catalog.search(
            query,
            scope=context.scope,
            flags=None,
            statuses=("approved", "candidate"),
        )
        return {"entries": entries}

    def submit_candidate(
        id: str,
        trigger: str,
        evidence_required: List[str],
        text: str,
        title: str = "",
        type: str = "rule",
        action: str = "",
        exceptions: Optional[List[str]] = None,
        source_refs: Optional[List[str]] = None,
        priority: int = 50,
    ) -> Dict[str, Any]:
        try:
            stored = context.catalog.submit_candidate(
                {
                    "id": id,
                    "title": title or id,
                    "type": type,
                    "scope": [context.scope],
                    "trigger": trigger,
                    "action": action,
                    "evidence_required": evidence_required,
                    "exceptions": exceptions or [],
                    "source_refs": source_refs or [],
                    "text": text,
                    "priority": priority,
                }
            )
        except ValueError as error:
            return {
                "id": id,
                "status": "rejected",
                "version": 0,
                "error": str(error),
            }
        context.submitted_ids.append(stored["id"])
        return {
            "id": stored["id"],
            "status": stored["status"],
            "version": int(stored["version"]),
            "error": "",
        }

    agent.register_tool(
        FunctionResource(
            func=read_source,
            name="read_source_markdown",
            description="Read a whitelisted markdown chapter. PDF is not allowed.",
            input_model=MarkdownArgs,
            output_model=MarkdownOutput,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=list_flags,
            name="list_allowed_flags",
            description="Frozen snapshot flag names this scope may use.",
            input_model=ScopeArgs,
            output_model=FlagsOutput,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=search_existing,
            name="search_existing_entries",
            description="Search approved and candidate cards for dedup.",
            input_model=SearchExistingArgs,
            output_model=ExistingSearchOutput,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=submit_candidate,
            name="submit_candidate",
            description="Write a candidate methodology card. Cannot approve.",
            input_model=CandidateArgs,
            output_model=CandidateOutput,
            side_effect=SideEffect.NON_IDEMPOTENT,
        )
    )


@dataclass
class DistillResult:
    submitted_ids: List[str]
    notes: str
    session_id: str


async def distill_chapter(
    markdown_path: str,
    *,
    scope: str,
    database: SQLiteDatabase,
    llm_adapter,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = False,
    roots: Optional[Sequence[Path]] = None,
) -> DistillResult:
    if scope not in {"fundamental", "technical", "sentiment", "macro"}:
        raise ValueError("unsupported distill scope")
    await database.initialize()
    catalog = MethodologyCatalog(database)
    catalog.ensure_seeded()
    path = Path(markdown_path)
    used_roots = list(roots or DEFAULT_MARKDOWN_ROOTS)
    used_roots.append(path.parent)
    context = DistillToolContext(
        catalog=catalog,
        scope=scope,
        markdown_path=path,
        roots=used_roots,
    )
    session_id = str(uuid4())
    agent = AgentInstance(
        name="tracking",
        llm_adapter=llm_adapter,
        config=tracking_distill_config(
            model=model, thinking_enabled=thinking_enabled
        ),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    register_distill_tools(agent, context)
    user_query = json.dumps(
        {
            "task": "distill",
            "scope": scope,
            "markdown_path": str(path),
        },
        ensure_ascii=False,
    )
    final = await agent.run(
        user_query,
        session_id=session_id,
        business_context={"task": "distill", "scope": scope},
    )
    notes = ""
    if isinstance(final.output, dict):
        notes = str(final.output.get("notes") or "")
    return DistillResult(
        submitted_ids=list(context.submitted_ids),
        notes=notes,
        session_id=session_id,
    )
