from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import ResourceType, SideEffect, utc_now
from .business_contracts import (
    AgentSkillCallRecord,
    ExpertEvaluation,
    NextCheckSuggestion,
    RunRecord,
    TrackingDeliverable,
    TrackingUserOutput,
)
from .methodology import (
    MethodologyCatalog,
    MethodologyToolOutput,
    allowed_flags_for,
    bind_catalog,
    read_whitelisted_markdown,
    record_search,
    reset_catalog,
    split_kb_database,
)
from .persistence import (
    FileArtifactStore,
    SQLiteAuditLog,
    SQLiteContextArchive,
    SQLiteDatabase,
    SQLiteStateStore,
)
from .research_store import ResearchStore
from .resources import FunctionResource
from .runtime import AgentInstance, RuntimeConfig
from .user_contracts import DIMS, NONE, UserIntent
from .user_intent import (
    collect_task_user_fields,
    compute_rerun_dims,
    format_intent_echo,
    overlay_none_slots,
    should_schedule_track_rerun,
)
from .user_store import UserStore
from .schema_model import model_from_json_schema
from .state import attach_prior_memory, prior_session_memory
from .tracking import (
    MAX_SKILLS_PER_LOOP,
    TrackSnapshots,
    assemble_context,
    fetch_track_snapshots,
    prescreen,
    snapshots_from_artifacts,
)

DEFAULT_MARKDOWN_ROOTS = (
    Path("data/kb/markdown"),
    Path("tests/fixtures/kb"),
)

TRACKING_SYSTEM_PROMPT = """
You are InsightAgent's tracking agent.

You are not a fifth dimensional analyst. Fundamental, technical,
sentiment, and macro facts still come from the four specialists.
You cannot approve methodology entries or change specialist system
prompts.

You are the officer on duty. You must think. After a specialist
returns, you do not only check whether the answer looks true. You
evaluate it: did it answer the question, overclaim, miss evidence,
conflict with the baseline thesis or the prescreen, or leave gaps.
Then you synthesize: weigh accepted specialist output, the archive,
and the delta sheet, and write your own tracking analysis.

Do not invent a specialist's dimensional conclusion when a call
fails. You may still analyze what the failure means for the thesis.
Do not replace a missing specialist by guessing numbers, stance,
or scores.

This round's Task JSON names the job. Use only the tools registered
this round. Distill tasks must not call analysts. Tracking tasks
must not read L0 markdown.

Prefer reusing the latest reports and the prescreen when they
already answer the question. Call at most one analyst in a single
tool round. After you see that result, evaluate it; a later loop
may call a different analyst. Do not call two analysts in the same
tool round.
Finish with submit_final. tracking output must include thinking,
synthesis, and expert_evaluations for every specialist you called.
""".strip()


DEFAULT_TRACK_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "answer",
        "thesis_impact",
        "evidence_refs",
        "abstain",
        "falsifier_hit",
        "missing",
    ],
    "properties": {
        "answer": {"type": "string"},
        "thesis_impact": {
            "type": "string",
            "enum": ["none", "weaken", "invalidate", "uncertain"],
        },
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "falsifier_hit": {"type": "boolean"},
        "abstain": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
}

REVAL_DUTY = {
    "fundamental": "value, financial quality, and margin of safety",
    "technical": "price structure and indicator snapshots only",
    "sentiment": "events and holder changes only",
    "macro": "macro and industry environment only",
}

REVAL_SYSTEM_PROMPT = """
You are InsightAgent's {role} analyst answering one tracking question.

Duty: {duty}.
Forbidden: writing methodology cards, calling other agents, substituting
other disciplines, treating a tracker brief as your own conclusion.

Work only from this dimension's tools. Then submit_final.
output must match the schema in this Task and in the submit_final tool.
It is not the first-research Report unless that schema was given.
""".strip()


def reval_runtime_config(
    role: str,
    output_model: type,
    *,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = False,
) -> RuntimeConfig:
    return RuntimeConfig(
        model=model,
        system_prompt=REVAL_SYSTEM_PROMPT.format(
            role=role, duty=REVAL_DUTY[role]
        ),
        thinking_enabled=thinking_enabled,
        reasoning_effort="high",
        response_format="json",
        max_tokens=8192,
        max_loop_round=8,
        max_parallel_calls=1,
        strict_tools=True,
        final_output_model=output_model,
    )


def parse_output_schema(raw: str) -> Dict[str, Any]:
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("output_schema must be JSON text") from error
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("output_schema must be a JSON object schema")
    if not isinstance(schema.get("properties"), dict) or not schema["properties"]:
        raise ValueError("output_schema.properties is required")
    return schema



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

    entries: List[Dict[str, Any]]


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


def tracking_runtime_config(
    *,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = False,
    final_output_model: Optional[type] = None,
) -> RuntimeConfig:
    return RuntimeConfig(
        model=model,
        system_prompt=TRACKING_SYSTEM_PROMPT,
        thinking_enabled=thinking_enabled,
        reasoning_effort="high",
        response_format="json",
        max_tokens=8192,
        max_loop_round=8,
        max_parallel_calls=1,
        strict_tools=True,
        final_output_model=final_output_model or DistillFinalOutput,
    )


def tracking_distill_config(
    *,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = False,
) -> RuntimeConfig:
    return tracking_runtime_config(
        model=model,
        thinking_enabled=thinking_enabled,
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
    instruction: Optional[str] = None,
    kb_database: Optional[SQLiteDatabase] = None,
) -> DistillResult:
    if scope not in {"fundamental", "technical", "sentiment", "macro"}:
        raise ValueError("unsupported distill scope")
    await database.initialize()
    catalog = MethodologyCatalog(kb_database or split_kb_database(database))
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
            "instruction": instruction
            or (
                "Read the whitelisted chapter, map discipline onto frozen "
                "flags, and submit_candidate only. Do not call analysts."
            ),
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


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dummy: str = ""


class SkillCallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    output_schema: str = Field(
        min_length=2,
        description=(
            "JSON text of an object schema. The analyst submit_final.output "
            "must match it. Pass a string, not a nested object."
        ),
    )
    required_context_refs: List[str] = Field(default_factory=list)


class SkillCallOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    status: str
    question: str
    reason: str
    error: str = ""
    output: Optional[Dict[str, Any]] = None


class TrackSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    scope: str


@dataclass
class TrackToolContext:
    catalog: MethodologyCatalog
    tracking_context: Dict[str, Any]
    prescreen: Dict[str, Any]
    current: TrackSnapshots
    track_run: RunRecord
    database: SQLiteDatabase
    artifacts: FileArtifactStore
    llm_adapter: Any
    expert_adapters: Dict[str, Any]
    model: str
    thinking_enabled: bool
    retrieved_kb_ids: set = field(default_factory=set)
    skill_calls: List[Dict[str, Any]] = field(default_factory=list)
    intent: Optional[UserIntent] = None
    prefs_by_scope: Dict[str, List[str]] = field(default_factory=dict)
    must_call_dims: List[str] = field(default_factory=list)


@dataclass
class TrackResult:
    thesis_id: str
    run_id: str
    session_id: str
    deliverable: Dict[str, Any]
    prescreen: Dict[str, Any]
    skill_calls: List[Dict[str, Any]]
    intent: Optional[UserIntent] = None
    show_intent_echo: bool = False


@dataclass
class TrackFeedbackResult:
    skipped: bool = False
    noop: bool = False
    parent_run_id: Optional[str] = None
    intent: Optional[UserIntent] = None
    show_intent_echo: bool = False
    track_result: Optional[TrackResult] = None
    error: Optional[str] = None


def _union_flags(prescreen_payload: Dict[str, Any]) -> List[str]:
    current = prescreen_payload.get("current_flags") or {}
    flags: List[str] = []
    for items in current.values():
        flags.extend(str(item) for item in items or [])
    return flags


def register_track_tools(agent: AgentInstance, context: TrackToolContext) -> None:
    def get_context(dummy: str = "") -> Dict[str, Any]:
        return context.tracking_context

    def get_prescreen(dummy: str = "") -> Dict[str, Any]:
        return context.prescreen

    def search(query: str, scope: str) -> Dict[str, Any]:
        return record_search(
            context.retrieved_kb_ids,
            query,
            scope=scope,
            flags=_union_flags(context.prescreen),
        )

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
                    "scope": ["fundamental"],
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
        return {
            "id": stored["id"],
            "status": stored["status"],
            "version": int(stored["version"]),
            "error": "",
        }

    async def _loop_round() -> int:
        session_id = context.track_run.session_ids.get("tracking")
        if not session_id:
            return 0
        store = SQLiteStateStore(context.database)
        state = await store.load_or_create(
            agent_name="tracking",
            session_id=session_id,
        )
        return int(state.loop_round)

    async def _call_expert(
        agent_name: str,
        question: str,
        reason: str,
        refs: List[str],
        output_schema_text: str,
    ) -> Dict[str, Any]:
        loop_round = await _loop_round()

        def _payload(
            status: str, *, error: str = "", output: Optional[Dict[str, Any]] = None
        ) -> Dict[str, Any]:
            return {
                "agent": agent_name,
                "status": status,
                "question": question,
                "reason": reason,
                "error": error,
                "output": output,
            }

        same_round = [
            item
            for item in context.skill_calls
            if item.get("loop_round") == loop_round
        ]
        if len(same_round) >= MAX_SKILLS_PER_LOOP:
            record = {
                "agent": agent_name,
                "question": question,
                "required_context_refs": refs,
                "reason": reason,
                "status": "rejected",
                "loop_round": loop_round,
            }
            context.skill_calls.append(record)
            return _payload(
                "rejected",
                error="at most one analyst per loop; call another after this result",
            )
        try:
            schema = parse_output_schema(output_schema_text)
            output_model = model_from_json_schema(schema)
        except ValueError as error:
            record = {
                "agent": agent_name,
                "question": question,
                "required_context_refs": refs,
                "reason": reason,
                "status": "failed",
                "loop_round": loop_round,
            }
            context.skill_calls.append(record)
            return _payload("failed", error=str(error))

        from .workflows.initial_research import (
            _run_fundamental_agent,
            _run_macro_agent,
            _run_sentiment_agent,
            _run_technical_agent,
        )

        llm = context.expert_adapters.get(agent_name) or context.llm_adapter
        runtime = reval_runtime_config(
            agent_name,
            output_model,
            model=context.model,
            thinking_enabled=context.thinking_enabled,
        )
        questions, constraints = collect_task_user_fields(
            context.intent,
            agent_name,
            context.prefs_by_scope.get(agent_name) or [],
        )
        query = json.dumps(
            {
                "task": "reval",
                "run_id": context.track_run.run_id,
                "stock_code": context.track_run.stock_code,
                "as_of": utc_now().isoformat(),
                "question": question,
                "reason": reason,
                "required_context_refs": refs,
                "output_schema": schema,
                "required_questions": questions,
                "constraints": constraints,
            },
            ensure_ascii=False,
        )
        run_kwargs = dict(
            run=context.track_run,
            database=context.database,
            llm_adapter=llm,
            model=context.model,
            thinking_enabled=context.thinking_enabled,
            user_query=query,
            runtime_config=runtime,
            return_raw=True,
        )
        try:
            if agent_name == "fundamental":
                output, _ = await _run_fundamental_agent(
                    snapshot=context.current.fundamental,
                    artifacts=context.artifacts,
                    **run_kwargs,
                )
            elif agent_name == "technical":
                output, _ = await _run_technical_agent(
                    tech_fields=context.current.technical,
                    **run_kwargs,
                )
            elif agent_name == "sentiment":
                output, _ = await _run_sentiment_agent(
                    sent_fields=context.current.sentiment,
                    **run_kwargs,
                )
            else:
                output, _ = await _run_macro_agent(
                    macro_fields=context.current.macro,
                    **run_kwargs,
                )
        except Exception as error:
            record = {
                "agent": agent_name,
                "question": question,
                "required_context_refs": refs,
                "reason": reason,
                "status": "failed",
                "loop_round": loop_round,
            }
            context.skill_calls.append(record)
            return _payload(
                "failed",
                error="{}: {}".format(type(error).__name__, error),
            )
        dumped = output if isinstance(output, dict) else {}
        record = {
            "agent": agent_name,
            "question": question,
            "required_context_refs": refs,
            "reason": reason,
            "status": "success",
            "loop_round": loop_round,
            "output": dumped,
        }
        context.skill_calls.append(record)
        return _payload("success", output=dumped)

    async def call_fundamental(
        question: str,
        reason: str,
        output_schema: str,
        required_context_refs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return await _call_expert(
            "fundamental",
            question,
            reason,
            required_context_refs or [],
            output_schema,
        )

    async def call_technical(
        question: str,
        reason: str,
        output_schema: str,
        required_context_refs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return await _call_expert(
            "technical",
            question,
            reason,
            required_context_refs or [],
            output_schema,
        )

    async def call_sentiment(
        question: str,
        reason: str,
        output_schema: str,
        required_context_refs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return await _call_expert(
            "sentiment",
            question,
            reason,
            required_context_refs or [],
            output_schema,
        )

    async def call_macro(
        question: str,
        reason: str,
        output_schema: str,
        required_context_refs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return await _call_expert(
            "macro",
            question,
            reason,
            required_context_refs or [],
            output_schema,
        )

    agent.register_tool(
        FunctionResource(
            func=get_context,
            name="get_tracking_context",
            description="Read-only TrackingContext for this thesis. Do not treat it as a professional conclusion.",
            input_model=EmptyArgs,
            output_model=None,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=get_prescreen,
            name="get_prescreen",
            description="Deterministic delta versus the baseline research run.",
            input_model=EmptyArgs,
            output_model=None,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=search,
            name="search_methodology",
            description="Search approved methodology cards. Flags come from the current snapshots.",
            input_model=TrackSearchArgs,
            output_model=MethodologyToolOutput,
            resource_type=ResourceType.KNOWLEDGE_BASE,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=submit_candidate,
            name="submit_candidate",
            description="Propose a candidate card. Cannot approve.",
            input_model=CandidateArgs,
            output_model=CandidateOutput,
            side_effect=SideEffect.NON_IDEMPOTENT,
        )
    )
    skill_kwargs = dict(
        input_model=SkillCallArgs,
        output_model=SkillCallOutput,
        resource_type=ResourceType.AGENT_SKILL,
        parallel_safe=False,
        timeout_seconds=180.0,
        side_effect=SideEffect.NON_IDEMPOTENT,
    )
    agent.register_tool(
        FunctionResource(
            func=call_fundamental,
            name="call_fundamental",
            description="Ask the fundamental analyst one question and supply output_schema JSON. At most one analyst per loop.",
            **skill_kwargs,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=call_technical,
            name="call_technical",
            description="Ask the technical analyst one question and supply output_schema JSON. At most one analyst per loop.",
            **skill_kwargs,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=call_sentiment,
            name="call_sentiment",
            description="Ask the sentiment analyst one question and supply output_schema JSON. At most one analyst per loop.",
            **skill_kwargs,
        )
    )
    agent.register_tool(
        FunctionResource(
            func=call_macro,
            name="call_macro",
            description="Ask the macro analyst one question and supply output_schema JSON. At most one analyst per loop.",
            **skill_kwargs,
        )
    )


def finalize_deliverable(
    raw: Dict[str, Any],
    *,
    prescreen_payload: Dict[str, Any],
    skill_calls: Sequence[Dict[str, Any]],
) -> TrackingDeliverable:
    calls = [
        AgentSkillCallRecord(
            agent=item["agent"],
            question=item.get("question") or "",
            required_context_refs=list(item.get("required_context_refs") or []),
            reason=item.get("reason") or "",
            status=item.get("status") or "failed",
        )
        for item in skill_calls
        if item.get("agent") in {"fundamental", "technical", "sentiment", "macro"}
    ]
    evidence = list(raw.get("evidence_refs") or [])
    for trigger in prescreen_payload.get("triggers") or []:
        if trigger not in evidence:
            evidence.append(trigger)
    for item in skill_calls:
        payload = item.get("output") or {}
        refs = payload.get("evidence_refs") if isinstance(payload, dict) else None
        if isinstance(refs, list):
            for cid in refs:
                text = str(cid or "")
                if text and text not in evidence:
                    evidence.append(text)
        for citation in (payload.get("citations") or []) if isinstance(payload, dict) else []:
            cid = str(citation.get("id") or "")
            if cid and cid not in evidence:
                evidence.append(cid)
    status = str(raw.get("status") or "unchanged")
    if status not in {"unchanged", "review", "invalidate"}:
        status = "unchanged"
    successful = [item for item in calls if item.status == "success"]
    if status == "invalidate" and not prescreen_payload.get("triggers") and not successful:
        status = "unchanged"
    if not evidence and status != "unchanged":
        status = "unchanged"
    user = raw.get("user_output") or {}
    if not isinstance(user, dict):
        user = {}
    holding = user.get("holding_advice") or status
    if holding not in {"unchanged", "review", "invalidate"}:
        holding = status
    evaluations = []
    for item in raw.get("expert_evaluations") or []:
        if not isinstance(item, dict):
            continue
        try:
            evaluations.append(ExpertEvaluation.model_validate(item))
        except ValidationError:
            continue
    called = {item.agent for item in calls if item.status == "success"}
    covered = {item.agent for item in evaluations}
    for agent_name in sorted(called - covered):
        evaluations.append(
            ExpertEvaluation(
                agent=agent_name,  # type: ignore[arg-type]
                reliability="medium",
                verdict="insufficient",
                notes="tracker omitted evaluation; marked incomplete",
            )
        )
    thinking = str(raw.get("thinking") or "")
    synthesis = str(raw.get("synthesis") or "")
    if successful and not thinking:
        thinking = "specialist output was received; tracker did not write thinking"
    if successful and not synthesis:
        synthesis = str(raw.get("work_summary") or user.get("summary") or "")
    return TrackingDeliverable(
        status=status,  # type: ignore[arg-type]
        work_summary=str(raw.get("work_summary") or "checked baseline versus current snapshots"),
        thinking=thinking,
        synthesis=synthesis,
        expert_evaluations=evaluations,
        evidence_refs=evidence,
        triggers_hit=list(prescreen_payload.get("triggers") or []),
        agent_skill_calls=calls,
        decision_required=status == "invalidate",
        user_output=TrackingUserOutput(
            title=str(user.get("title") or "本次跟踪更新"),
            summary=str(user.get("summary") or raw.get("work_summary") or status),
            holding_advice=holding,  # type: ignore[arg-type]
            key_changes=list(user.get("key_changes") or []),
            next_watch_items=list(user.get("next_watch_items") or []),
        ),
        next_check_suggestion=NextCheckSuggestion.model_validate(
            raw.get("next_check_suggestion") or {}
        ),
    )


DEFAULT_TRACK_INSTRUCTION = (
    "Read get_tracking_context and get_prescreen. "
    "Decide whether a specialist is needed. "
    "If you call one, evaluate the returned output: reliability, "
    "gaps, and whether to accept, discount, or reject it. "
    "Then write thinking and synthesis before submit_final. "
    "If nothing material changed, still write brief thinking "
    "and synthesis that the thesis still holds, and call no analyst. "
    "Call at most one analyst in this tool round. After that "
    "result, a later loop may call another. Do not invent an "
    "expert dimensional conclusion when a skill fails. "
    "A dimension tag does not force you to call that specialist."
)


async def _load_prefs_by_scope(
    user_store: UserStore, *, user_id: str, stock_code: str
) -> Dict[str, List[str]]:
    loaded: Dict[str, List[str]] = {}
    for dim in DIMS:
        rows = await user_store.active_preferences(
            user_id=user_id, scope=dim, stock_code=stock_code
        )
        loaded[dim] = [item.statement for item in rows]
    return loaded


def _track_user_query(
    *,
    thesis_id: str,
    stock_code: str,
    baseline_run_id: str,
    instruction: Optional[str],
    intent: Optional[UserIntent],
    prefs_by_scope: Dict[str, List[str]],
    must_call_dims: Sequence[str],
) -> str:
    questions, constraints = collect_task_user_fields(
        intent, "tracking", prefs_by_scope.get("tracking") or []
    )
    extra_q, extra_c = collect_task_user_fields(
        intent, "decision", prefs_by_scope.get("decision") or []
    )
    questions.extend(extra_q)
    constraints.extend(extra_c)
    text = instruction or DEFAULT_TRACK_INSTRUCTION
    named = [dim for dim in must_call_dims if dim]
    if named:
        text = (
            text
            + " You must call these analysts this session, one per loop: "
            + ", ".join(named)
            + "."
        )
    return json.dumps(
        {
            "task": "track",
            "thesis_id": thesis_id,
            "stock_code": stock_code,
            "baseline_run_id": baseline_run_id,
            "instruction": text,
            "required_questions": questions,
            "constraints": constraints,
        },
        ensure_ascii=False,
    )


async def track_thesis(
    thesis_id: str,
    *,
    database: SQLiteDatabase,
    llm_adapter,
    artifact_root: str,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = False,
    fixture: bool = True,
    fixtures_dir: Optional[str] = None,
    current: Optional[TrackSnapshots] = None,
    expert_adapters: Optional[Dict[str, Any]] = None,
    instruction: Optional[str] = None,
    user_prompt: str = NONE,
    user_id: str = "local",
    extract_llm_adapter=None,
    overlay_intent: Optional[UserIntent] = None,
    must_call_dims: Optional[Sequence[str]] = None,
) -> TrackResult:
    await database.initialize()
    store = ResearchStore(database)
    audit = SQLiteAuditLog(database)
    artifacts = FileArtifactStore(database, artifact_root)
    user_store = UserStore(database)
    packed = await store.get_baseline_run(thesis_id)
    if packed is None:
        raise ValueError("unknown thesis or run: {}".format(thesis_id))
    baseline_run = RunRecord.model_validate(packed["run"])
    if not packed.get("decisions"):
        raise ValueError("baseline run has no decision")
    decision = packed["decisions"][0]
    catalog = MethodologyCatalog(split_kb_database(database))
    catalog.ensure_seeded()

    refs = baseline_run.snapshot_refs or {}
    async def _load(key: str) -> Dict[str, Any]:
        ref = refs.get(key)
        if not ref:
            raise ValueError("baseline missing {} snapshot".format(key))
        return json.loads(await artifacts.get(ref))

    baseline_snaps = snapshots_from_artifacts(
        await _load("fundamental"),
        await _load("technical"),
        await _load("sentiment"),
        await _load("macro"),
    )
    current_snaps = current or await fetch_track_snapshots(
        baseline_run.stock_code,
        fixture=fixture,
        fixtures_dir=fixtures_dir,
    )
    timeline = await store.list_timeline(baseline_run.thesis_id)
    prescreen_payload = prescreen(
        baseline=baseline_snaps,
        current=current_snaps,
        prior_timeline=timeline,
    )
    tracking_context = assemble_context(
        stock_code=baseline_run.stock_code,
        thesis_id=baseline_run.thesis_id,
        baseline_run_id=baseline_run.run_id,
        decision=decision,
        reports=packed.get("reports") or [],
        prescreen_payload=prescreen_payload,
        timeline=timeline,
    )
    track_run = RunRecord(
        run_id=str(uuid4()),
        stock_code=baseline_run.stock_code,
        thesis_id=baseline_run.thesis_id,
        mode="track_day",
        status="running",
        parent_run_id=baseline_run.run_id,
        snapshot_refs=dict(baseline_run.snapshot_refs),
    )
    await store.save_run(track_run)
    session_id = str(uuid4())
    track_run.session_ids["tracking"] = session_id
    await store.save_run(track_run)
    await audit.append(
        "run_started",
        {"mode": "track_day", "thesis_id": track_run.thesis_id},
        run_id=track_run.run_id,
    )

    from .workflows.initial_research import _prepare_intent

    extract_llm = extract_llm_adapter or llm_adapter
    intent, show_intent_echo = await _prepare_intent(
        user_prompt=user_prompt,
        user_id=user_id,
        run=track_run,
        code=track_run.stock_code,
        llm_adapter=extract_llm,
        model=model,
        user_store=user_store,
        audit=audit,
        moment="pre_run",
    )
    prior = await store.latest_track_run(track_run.thesis_id)
    if prior is not None:
        pending = await user_store.intent_for_run_moment(
            run_id=prior["run"]["run_id"],
            moment="post_track",
            effect="this_run",
        )
        if pending is not None:
            intent = overlay_none_slots(intent, pending)
    if overlay_intent is not None:
        intent = overlay_none_slots(intent, overlay_intent)

    prefs_by_scope = await _load_prefs_by_scope(
        user_store, user_id=user_id, stock_code=track_run.stock_code
    )
    required = list(must_call_dims or [])

    tool_context = TrackToolContext(
        catalog=catalog,
        tracking_context=tracking_context.model_dump(mode="json"),
        prescreen=prescreen_payload,
        current=current_snaps,
        track_run=track_run,
        database=database,
        artifacts=artifacts,
        llm_adapter=llm_adapter,
        expert_adapters=expert_adapters or {},
        model=model,
        thinking_enabled=thinking_enabled,
        intent=intent,
        prefs_by_scope=prefs_by_scope,
        must_call_dims=required,
    )
    agent = AgentInstance(
        name="tracking",
        llm_adapter=llm_adapter,
        config=tracking_runtime_config(
            model=model,
            thinking_enabled=thinking_enabled,
            final_output_model=TrackingDeliverable,
        ),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    register_track_tools(agent, tool_context)
    token = bind_catalog(catalog)
    track_parent_id, track_memory = await prior_session_memory(
        SQLiteStateStore(database),
        agent_name="tracking",
        thesis_id=track_run.thesis_id,
    )
    user_query = attach_prior_memory(
        _track_user_query(
            thesis_id=track_run.thesis_id,
            stock_code=track_run.stock_code,
            baseline_run_id=baseline_run.run_id,
            instruction=instruction,
            intent=intent,
            prefs_by_scope=prefs_by_scope,
            must_call_dims=required,
        ),
        track_memory,
    )
    try:
        final = await agent.run(
            user_query,
            session_id=session_id,
            parent_session_id=track_parent_id,
            business_context={
                "task": "track",
                "stock_code": track_run.stock_code,
                "thesis_id": track_run.thesis_id,
                "run_id": track_run.run_id,
            },
        )
    finally:
        reset_catalog(token)

    raw = final.output if isinstance(final.output, dict) else {}
    deliverable = finalize_deliverable(
        raw,
        prescreen_payload=prescreen_payload,
        skill_calls=tool_context.skill_calls,
    )
    dumped = deliverable.model_dump(mode="json")
    for item in tool_context.skill_calls:
        await audit.append(
            "agent_dispatched",
            {
                "trigger_reason": item.get("reason"),
                "target_agent": item.get("agent"),
                "question": item.get("question"),
                "status": item.get("status"),
            },
            run_id=track_run.run_id,
            session_id=session_id,
        )
    timeline_payload = {
        "schema_version": "1",
        "mode": "track_day",
        "stock_code": track_run.stock_code,
        "thesis_id": track_run.thesis_id,
        "advice": deliverable.status,
        "triggers_hit": deliverable.triggers_hit,
        "suggested_agent": prescreen_payload.get("suggested_agent"),
        "triggers": prescreen_payload.get("triggers") or [],
        "dispatches": [item.get("agent") for item in tool_context.skill_calls],
        "deliverable": dumped,
    }
    await store.save_timeline(
        track_run.thesis_id, timeline_payload, run_id=track_run.run_id
    )
    track_run.status = "success"
    track_run.updated_at = utc_now()
    await store.save_run(track_run)
    return TrackResult(
        thesis_id=track_run.thesis_id,
        run_id=track_run.run_id,
        session_id=session_id,
        deliverable=dumped,
        prescreen=prescreen_payload,
        skill_calls=list(tool_context.skill_calls),
        intent=intent,
        show_intent_echo=show_intent_echo,
    )


class TrackFeedbackError(ValueError):
    pass


async def _resolve_track_parent(
    store: ResearchStore, thesis_or_run_id: str
) -> RunRecord:
    bundle = await store.get_run(thesis_or_run_id)
    if bundle is not None:
        run = RunRecord.model_validate(bundle["run"])
        if run.mode == "track_day":
            if bundle["status"] not in {"success", "degraded"}:
                raise TrackFeedbackError(
                    "parent track is not successful: {}".format(bundle["status"])
                )
            return run
        latest = await store.latest_track_run(run.thesis_id)
        if latest is None:
            raise TrackFeedbackError(
                "no completed track for thesis {}".format(run.thesis_id)
            )
        return RunRecord.model_validate(latest["run"])
    packed = await store.get_baseline_run(thesis_or_run_id)
    if packed is None:
        raise TrackFeedbackError(
            "unknown thesis or run: {}".format(thesis_or_run_id)
        )
    thesis_id = packed["run"]["thesis_id"]
    latest = await store.latest_track_run(thesis_id)
    if latest is None:
        raise TrackFeedbackError(
            "no completed track for thesis {}".format(thesis_id)
        )
    return RunRecord.model_validate(latest["run"])


async def feedback_on_track(
    thesis_or_run_id: str,
    *,
    database: SQLiteDatabase,
    llm_adapter,
    artifact_root: str,
    user_prompt: str = NONE,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = False,
    fixture: bool = True,
    fixtures_dir: Optional[str] = None,
    current: Optional[TrackSnapshots] = None,
    expert_adapters: Optional[Dict[str, Any]] = None,
    user_id: str = "local",
    extract_llm_adapter=None,
    instruction: Optional[str] = None,
) -> TrackFeedbackResult:
    prompt = (user_prompt or "").strip() or NONE
    if prompt == NONE:
        return TrackFeedbackResult(skipped=True, parent_run_id=thesis_or_run_id)

    await database.initialize()
    store = ResearchStore(database)
    audit = SQLiteAuditLog(database)
    user_store = UserStore(database)
    parent = await _resolve_track_parent(store, thesis_or_run_id)

    from .workflows.initial_research import _prepare_intent

    extract_llm = extract_llm_adapter or llm_adapter
    intent, show_echo = await _prepare_intent(
        user_prompt=prompt,
        user_id=user_id,
        run=parent,
        code=parent.stock_code,
        llm_adapter=extract_llm,
        model=model,
        user_store=user_store,
        audit=audit,
        moment="post_track",
    )
    if not should_schedule_track_rerun(intent):
        noop = intent.effect in {"rerun", "remember_rerun"}
        if noop:
            await audit.append(
                "rerun_noop",
                {"effect": intent.effect, "tags": intent.tags},
                run_id=parent.run_id,
            )
        return TrackFeedbackResult(
            noop=noop,
            parent_run_id=parent.run_id,
            intent=intent,
            show_intent_echo=show_echo,
        )
    result = await track_thesis(
        parent.thesis_id,
        database=database,
        llm_adapter=llm_adapter,
        artifact_root=artifact_root,
        model=model,
        thinking_enabled=thinking_enabled,
        fixture=fixture,
        fixtures_dir=fixtures_dir,
        current=current,
        expert_adapters=expert_adapters,
        user_prompt=NONE,
        user_id=user_id,
        extract_llm_adapter=extract_llm_adapter,
        overlay_intent=intent,
        must_call_dims=compute_rerun_dims(intent),
        instruction=instruction,
    )
    return TrackFeedbackResult(
        parent_run_id=parent.run_id,
        intent=intent,
        show_intent_echo=show_echo,
        track_result=result,
    )


def format_track_feedback_result(result: TrackFeedbackResult) -> str:
    if result.skipped:
        return ""
    lines = []
    if result.show_intent_echo and result.intent is not None:
        lines.append(format_intent_echo(result.intent))
    if result.track_result is not None:
        deliverable = result.track_result.deliverable
        user = deliverable.get("user_output") or {}
        lines.append("thesis: {}".format(result.track_result.thesis_id))
        lines.append("status: {}".format(deliverable.get("status")))
        lines.append(
            "summary: {}".format(
                user.get("summary") or deliverable.get("work_summary")
            )
        )
        return "\n".join(lines)
    lines.append(
        "未重跑，父 track {} 结论未改".format(result.parent_run_id or "-")
    )
    if result.noop:
        lines.append("rerun_noop")
    return "\n".join(lines)
