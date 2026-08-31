from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import ValidationError

from ..business_contracts import Decision, Report, RunRecord, sanitize_report
from ..contracts import utc_now
from ..data_contracts import (
    EventSnapshot,
    HolderChangeSnapshot,
    IndicatorSnapshot,
    KlineSnapshot,
    MacroSnapshot,
    PriceSnapshot,
)
from ..decision import build_multi_factor_decision, report_degrades_run
from ..evidence import (
    EvidenceBindingError,
    bind_macro_report_evidence,
    bind_report_evidence,
)
from ..fundamental_agent import (
    FundamentalToolContext,
    default_fixtures_dir,
    fundamental_runtime_config,
    parse_report,
    register_fundamental_tools,
)
from ..fundamentals import (
    REQUIRED_FIELDS,
    AkshareFundamentalAdapter,
    AkshareMacroAdapter,
    AkshareSentimentAdapter,
    AkshareTechnicalAdapter,
    FixtureFundamentalAdapter,
    FixtureMacroAdapter,
    FixtureSentimentAdapter,
    FixtureTechnicalAdapter,
    FundamentalSnapshot,
    MarketDataAdapter,
    apply_fundamental_rules,
)
from ..llm import LLMAdapter
from ..macro_agent import (
    MacroToolContext,
    macro_runtime_config,
    parse_macro_report,
    register_macro_tools,
)
from ..events import apply_computed_sentiment_semantics, apply_event_rules
from ..macros import apply_computed_macro_semantics
from ..market import synthetic_market_fixture
from ..methodology import (
    MethodologyCatalog,
    bind_catalog,
    drop_unretrieved_kb,
    reset_catalog,
)
from ..persistence import (
    FileArtifactStore,
    SQLiteAuditLog,
    SQLiteContextArchive,
    SQLiteDatabase,
    SQLiteStateStore,
)
from ..research_store import ResearchStore
from ..retry import ExponentialBackoff
from ..runtime import AgentInstance, InvalidModelOutputError, RuntimeConfig
from ..state import (
    StateConflictError,
    attach_prior_memory,
    prior_session_memory,
)
from ..sentiment_agent import (
    SentimentToolContext,
    parse_sentiment_report,
    register_sentiment_tools,
    sentiment_runtime_config,
)
from ..user_contracts import NONE, UserIntent, Moment
from ..user_intent import (
    build_expert_user_query,
    build_intent,
    build_utterance,
    compute_rerun_dims,
    extract_slots,
    fill_tagged_empty_slots,
    format_intent_echo,
    is_decision_only_rerun,
    merge_parsed_with_slots,
    parse_tags,
    should_schedule_rerun,
)
from ..user_store import UserStore
from ..technical_agent import (
    TechnicalToolContext,
    parse_technical_report,
    register_technical_tools,
    technical_runtime_config,
)
from ..technicals import apply_computed_technical_semantics, apply_technical_rules

STOCK_CODE_RE = re.compile(r"^\d{6}$")
UnboundPolicy = Literal["fail", "abstain"]


class FeedbackError(ValueError):
    pass


@dataclass
class FeedbackResult:
    skipped: bool = False
    noop: bool = False
    parent_run_id: Optional[str] = None
    intent: Optional[UserIntent] = None
    show_intent_echo: bool = False
    outcome: Optional[AnalysisOutcome] = None
    error: Optional[str] = None


class InvalidStockCodeError(ValueError):
    pass


@dataclass
class AnalysisOutcome:
    run: RunRecord
    snapshot: FundamentalSnapshot
    report: Optional[Report]
    technical_report: Optional[Report] = None
    sentiment_report: Optional[Report] = None
    macro_report: Optional[Report] = None
    decision: Optional[Decision] = None
    error: Optional[str] = None
    intent: Optional[UserIntent] = None
    show_intent_echo: bool = False
    parent_run_id: Optional[str] = None
    rerun_dimensions: List[str] = field(default_factory=list)
    copied_dimensions: List[str] = field(default_factory=list)
    parent_decision: Optional[Decision] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "run": self.run.model_dump(mode="json"),
            "snapshot": self.snapshot.model_dump(mode="json"),
            "report": (
                self.report.model_dump(mode="json") if self.report else None
            ),
            "technical_report": (
                self.technical_report.model_dump(mode="json")
                if self.technical_report
                else None
            ),
            "sentiment_report": (
                self.sentiment_report.model_dump(mode="json")
                if self.sentiment_report
                else None
            ),
            "macro_report": (
                self.macro_report.model_dump(mode="json")
                if self.macro_report
                else None
            ),
            "decision": (
                self.decision.model_dump(mode="json")
                if self.decision
                else None
            ),
            "error": self.error,
            "intent": (
                self.intent.model_dump(mode="json") if self.intent else None
            ),
        }
        if self.parent_run_id:
            payload["parent_run_id"] = self.parent_run_id
            payload["rerun_dimensions"] = list(self.rerun_dimensions)
            payload["copied_dimensions"] = list(self.copied_dimensions)
            payload["parent_decision"] = (
                {
                    "rating": self.parent_decision.rating,
                    "confidence": self.parent_decision.confidence,
                    "value_score": self.parent_decision.value_score,
                    "timing_score": self.parent_decision.timing_score,
                    "advice_one_liner": self.parent_decision.advice_one_liner,
                }
                if self.parent_decision
                else None
            )
        return payload


def normalize_stock_code(raw: str) -> str:
    code = raw.strip().upper().split(".")[0]
    if not STOCK_CODE_RE.fullmatch(code):
        raise InvalidStockCodeError(
            "Stock code must be 6 digits, got {!r}".format(raw)
        )
    return code


def build_market_adapter(
    *,
    fixture: bool,
    fixtures_dir: Optional[str] = None,
) -> MarketDataAdapter:
    if fixture:
        return FixtureFundamentalAdapter.from_directory(
            default_fixtures_dir(fixtures_dir)
        )
    return AkshareFundamentalAdapter()


def build_market_adapter_for(
    *,
    fixture: bool,
    dimension: str,
    fixtures_dir: Optional[str] = None,
    fixture_payload: Optional[Dict[str, Any]] = None,
) -> Any:
    if dimension == "fundamental":
        if fixture:
            return FixtureFundamentalAdapter.from_directory(
                default_fixtures_dir(fixtures_dir)
            )
        return AkshareFundamentalAdapter()
    if dimension == "technical":
        if fixture:
            return FixtureTechnicalAdapter(fixture_payload)
        return AkshareTechnicalAdapter()
    if dimension == "sentiment":
        if fixture:
            return FixtureSentimentAdapter(fixture_payload)
        return AkshareSentimentAdapter()
    if dimension == "macro":
        if fixture:
            return FixtureMacroAdapter(fixture_payload)
        return AkshareMacroAdapter()
    raise ValueError("unknown dimension {}".format(dimension))


async def analyze_stock(
    stock_code: str,
    *,
    database: SQLiteDatabase,
    llm_adapter: LLMAdapter,
    artifact_root: str,
    fixture: bool = True,
    fixtures_dir: Optional[str] = None,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = True,
    unbound_policy: UnboundPolicy = "fail",
    adapter: Optional[MarketDataAdapter] = None,
    technical_adapter: Optional[Any] = None,
    technical_llm_adapter: Optional[LLMAdapter] = None,
    sentiment_llm_adapter: Optional[LLMAdapter] = None,
    macro_llm_adapter: Optional[LLMAdapter] = None,
    user_prompt: str = NONE,
    user_id: str = "local",
    extract_llm_adapter: Optional[LLMAdapter] = None,
) -> AnalysisOutcome:
    code = normalize_stock_code(stock_code)
    artifacts = FileArtifactStore(database, artifact_root)
    store = ResearchStore(database)
    audit = SQLiteAuditLog(database)
    user_store = UserStore(database)
    extract_llm = extract_llm_adapter or llm_adapter
    fundamental_adapter = adapter or build_market_adapter_for(
        fixture=fixture, dimension="fundamental", fixtures_dir=fixtures_dir
    )
    fetch_retry = ExponentialBackoff()

    run = RunRecord(
        run_id=str(uuid4()),
        stock_code=code,
        thesis_id="{}-initial".format(code),
        status="running",
    )
    await store.save_run(run)
    await audit.append(
        "run_started",
        {"stock_code": code, "fixture": fixture},
        run_id=run.run_id,
    )
    intent, show_intent_echo = await _prepare_intent(
        user_prompt=user_prompt,
        user_id=user_id,
        run=run,
        code=code,
        llm_adapter=extract_llm,
        model=model,
        user_store=user_store,
        audit=audit,
    )
    fund_prefs = await user_store.active_preferences(
        user_id=user_id, scope="fundamental", stock_code=code
    )
    tech_prefs = await user_store.active_preferences(
        user_id=user_id, scope="technical", stock_code=code
    )
    sent_prefs = await user_store.active_preferences(
        user_id=user_id, scope="sentiment", stock_code=code
    )
    macro_prefs = await user_store.active_preferences(
        user_id=user_id, scope="macro", stock_code=code
    )
    decision_prefs = await user_store.active_preferences(
        user_id=user_id, scope="decision", stock_code=code
    )

    macro_fetch_error: Optional[Exception] = None
    try:
        snapshot = await fetch_retry.execute(
            fundamental_adapter.fetch_fundamental, code
        )
        snapshot = apply_fundamental_rules(snapshot)
        fundamental_artifact_ref = await artifacts.put(
            json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False)
        )
        snapshot.artifact_ref = fundamental_artifact_ref

        tech_fixture_payload = (
            synthetic_market_fixture(code) if fixture else None
        )
        sent_fixture_payload = (
            synthetic_market_fixture(code) if fixture else None
        )
        macro_fixture_payload = (
            synthetic_market_fixture(code) if fixture else None
        )
        technical_adapter = technical_adapter or build_market_adapter_for(
            fixture=fixture,
            dimension="technical",
            fixture_payload=tech_fixture_payload,
        )
        sentiment_adapter = build_market_adapter_for(
            fixture=fixture,
            dimension="sentiment",
            fixture_payload=sent_fixture_payload,
        )
        macro_adapter = build_market_adapter_for(
            fixture=fixture,
            dimension="macro",
            fixture_payload=macro_fixture_payload,
        )

        try:
            tech_fields = await fetch_retry.execute(
                technical_adapter.fetch_technical, code
            )
        except Exception:
            if technical_adapter is not None and not fixture:
                tech_fields = await fetch_retry.execute(
                    build_market_adapter_for(fixture=False, dimension="technical").fetch_technical,
                    code,
                )
            else:
                raise
        sent_fields = await fetch_retry.execute(
            sentiment_adapter.fetch_sentiment, code
        )
        macro_fetch_error: Optional[Exception] = None
        try:
            macro_fields = await fetch_retry.execute(macro_adapter.fetch_macro, code)
        except Exception as error:
            macro_fetch_error = error
            macro_fields = {
                "macro": MacroSnapshot().model_dump(mode="json"),
                "industry": "",
                "stock_code": code,
                "company_name": snapshot.company_name or "",
            }
        tech_artifact_ref = await artifacts.put(
            json.dumps(tech_fields, ensure_ascii=False)
        )
        sent_artifact_ref = await artifacts.put(
            json.dumps(sent_fields, ensure_ascii=False)
        )
        macro_artifact_ref = await artifacts.put(
            json.dumps(macro_fields, ensure_ascii=False)
        )
        run.snapshot_refs = {
            "fundamental": fundamental_artifact_ref,
            "technical": tech_artifact_ref,
            "sentiment": sent_artifact_ref,
            "macro": macro_artifact_ref,
        }
        run.updated_at = utc_now()
        await store.save_run(run)
        await audit.append(
            "snapshot_ready",
            {
                "fundamental_artifact_ref": fundamental_artifact_ref,
                "technical_artifact_ref": tech_artifact_ref,
                "sentiment_artifact_ref": sent_artifact_ref,
                "macro_artifact_ref": macro_artifact_ref,
                "missing_fields": snapshot.missing_fields,
                "computed_flags": snapshot.computed_flags,
            },
            run_id=run.run_id,
        )

        if _severely_missing(snapshot):
            fundamental_report = _abstain_for_missing(snapshot)
            technical_report = _abstain_technical()
            sentiment_report = _abstain_sentiment()
            macro_report = _abstain_macro()
            decision = build_multi_factor_decision(
                fundamental_report,
                technical_report,
                sentiment_report,
                macro_report,
                snapshot,
                user_constraint=_decision_constraint(intent, decision_prefs),
            )
            return await _complete(
                store,
                audit,
                run,
                snapshot,
                fundamental_report,
                technical_report,
                sentiment_report,
                macro_report,
                decision,
                status="degraded",
                intent=intent,
                show_intent_echo=show_intent_echo,
            )

        fundamental_future = _run_fundamental_agent(
            snapshot=snapshot,
            run=run,
            database=database,
            artifacts=artifacts,
            llm_adapter=llm_adapter,
            model=model,
            thinking_enabled=thinking_enabled,
            user_query=build_expert_user_query(
                run_id=run.run_id,
                stock_code=code,
                as_of=snapshot.as_of.isoformat(),
                dim="fundamental",
                intent=intent,
                preference_statements=[item.statement for item in fund_prefs],
            ),
        )
        technical_future = _run_technical_agent(
            tech_fields=tech_fields,
            run=run,
            database=database,
            llm_adapter=technical_llm_adapter or llm_adapter,
            model=model,
            thinking_enabled=thinking_enabled,
            user_query=build_expert_user_query(
                run_id=run.run_id,
                stock_code=code,
                as_of=snapshot.as_of.isoformat(),
                dim="technical",
                intent=intent,
                preference_statements=[item.statement for item in tech_prefs],
            ),
        )
        sentiment_future = _run_sentiment_agent(
            sent_fields=sent_fields,
            run=run,
            database=database,
            llm_adapter=sentiment_llm_adapter or llm_adapter,
            model=model,
            thinking_enabled=thinking_enabled,
            user_query=build_expert_user_query(
                run_id=run.run_id,
                stock_code=code,
                as_of=snapshot.as_of.isoformat(),
                dim="sentiment",
                intent=intent,
                preference_statements=[item.statement for item in sent_prefs],
            ),
        )
        macro_future = (
            _run_macro_agent(
                macro_fields=macro_fields,
                run=run,
                database=database,
                llm_adapter=macro_llm_adapter or llm_adapter,
                model=model,
                thinking_enabled=thinking_enabled,
                user_query=build_expert_user_query(
                    run_id=run.run_id,
                    stock_code=code,
                    as_of=snapshot.as_of.isoformat(),
                    dim="macro",
                    intent=intent,
                    preference_statements=[
                        item.statement for item in macro_prefs
                    ],
                ),
            )
            if macro_fetch_error is None
            else _immediate_macro_abstention()
        )

        fundamental_result, technical_result, sentiment_result, macro_result = (
            await asyncio.gather(
                fundamental_future,
                technical_future,
                sentiment_future,
                macro_future,
                return_exceptions=True,
            )
        )

        fundamental_exception = (
            fundamental_result
            if isinstance(fundamental_result, Exception)
            else None
        )
        technical_exception = (
            technical_result
            if isinstance(technical_result, Exception)
            else None
        )
        sentiment_exception = (
            sentiment_result
            if isinstance(sentiment_result, Exception)
            else None
        )
        macro_exception = (
            macro_result if isinstance(macro_result, Exception) else None
        )

        if fundamental_exception is not None:
            raise fundamental_exception

        fundamental_report, _ = fundamental_result
        technical_report = (
            technical_result[0]
            if not isinstance(technical_result, Exception)
            else _abstain_agent_failure("technical", technical_exception)
        )
        sentiment_report = (
            sentiment_result[0]
            if not isinstance(sentiment_result, Exception)
            else _abstain_agent_failure("sentiment", sentiment_exception)
        )
        macro_report = (
            macro_result[0]
            if not isinstance(macro_result, Exception)
            else _abstain_agent_failure("macro", macro_exception)
        )

        if technical_exception is not None:
            await audit.append(
                "agent_failed",
                {
                    "agent": "technical",
                    "error_type": type(technical_exception).__name__,
                    "message": str(technical_exception),
                },
                run_id=run.run_id,
            )
        if sentiment_exception is not None:
            await audit.append(
                "agent_failed",
                {
                    "agent": "sentiment",
                    "error_type": type(sentiment_exception).__name__,
                    "message": str(sentiment_exception),
                },
                run_id=run.run_id,
            )
        if macro_exception is not None:
            await audit.append(
                "agent_failed",
                {
                    "agent": "macro",
                    "error_type": type(macro_exception).__name__,
                    "message": str(macro_exception),
                },
                run_id=run.run_id,
            )

        await audit.append(
            "agent_completed",
            {
                "status": (
                    "completed" if not fundamental_report.abstain else "abstained"
                ),
                "stance": fundamental_report.stance,
                "score": fundamental_report.score,
            },
            run_id=run.run_id,
            session_id=run.session_ids.get("fundamental"),
        )
        await audit.append(
            "agent_completed",
            {
                "status": (
                    "completed" if not technical_report.abstain else "abstained"
                ),
                "stance": technical_report.stance,
                "score": technical_report.score,
            },
            run_id=run.run_id,
            session_id=run.session_ids.get("technical"),
        )
        await audit.append(
            "agent_completed",
            {
                "status": (
                    "completed" if not sentiment_report.abstain else "abstained"
                ),
                "stance": sentiment_report.stance,
                "score": sentiment_report.score,
            },
            run_id=run.run_id,
            session_id=run.session_ids.get("sentiment"),
        )
        await audit.append(
            "agent_completed",
            {
                "status": "completed" if not macro_report.abstain else "abstained",
                "stance": macro_report.stance,
                "score": macro_report.score,
            },
            run_id=run.run_id,
            session_id=run.session_ids.get("macro"),
        )

        try:
            bind_report_evidence(fundamental_report, snapshot)
        except EvidenceBindingError as error:
            if unbound_policy == "abstain":
                fundamental_report = _abstain_for_unbound(snapshot, error)
            else:
                raise
        try:
            bind_macro_report_evidence(
                macro_report, MacroSnapshot(**macro_fields["macro"])
            )
        except EvidenceBindingError as error:
            if unbound_policy == "abstain":
                macro_report = _abstain_macro("证据绑定失败，改为弃权。")
            else:
                raise

        decision = build_multi_factor_decision(
            fundamental_report,
            technical_report,
            sentiment_report,
            macro_report,
            snapshot,
            user_constraint=_decision_constraint(intent, decision_prefs),
        )
        status = "success"
        if any(
            report_degrades_run(dim, report)
            for dim, report in [
                ("fundamental", fundamental_report),
                ("technical", technical_report),
                ("sentiment", sentiment_report),
                ("macro", macro_report),
            ]
        ):
            status = "degraded"
        return await _complete(
            store,
            audit,
            run,
            snapshot,
            fundamental_report,
            technical_report,
            sentiment_report,
            macro_report,
            decision,
            status=status,
            intent=intent,
            show_intent_echo=show_intent_echo,
        )
    except Exception as error:
        run.status = "failed"
        run.updated_at = utc_now()
        await store.save_run(run)
        await audit.append(
            "run_failed",
            {"type": type(error).__name__, "message": str(error)},
            run_id=run.run_id,
        )
        if macro_fetch_error is not None:
            await audit.append(
                "agent_failed",
                {
                    "agent": "macro",
                    "error_type": type(macro_fetch_error).__name__,
                    "message": str(macro_fetch_error),
                },
                run_id=run.run_id,
            )
        return AnalysisOutcome(
            run=run,
            snapshot=locals().get(
                "snapshot",
                FundamentalSnapshot(stock_code=code),
            ),
            report=None,
            technical_report=None,
            sentiment_report=None,
            macro_report=None,
            decision=None,
            error="{}: {}".format(type(error).__name__, error),
            intent=intent,
            show_intent_echo=show_intent_echo,
        )


async def feedback_on_run(
    parent_run_id: str,
    *,
    database: SQLiteDatabase,
    llm_adapter: LLMAdapter,
    artifact_root: str,
    user_prompt: str = NONE,
    model: str = "deepseek-v4-flash",
    thinking_enabled: bool = True,
    unbound_policy: UnboundPolicy = "fail",
    user_id: str = "local",
    extract_llm_adapter: Optional[LLMAdapter] = None,
    technical_llm_adapter: Optional[LLMAdapter] = None,
    sentiment_llm_adapter: Optional[LLMAdapter] = None,
    macro_llm_adapter: Optional[LLMAdapter] = None,
) -> FeedbackResult:
    prompt = (user_prompt or "").strip() or NONE
    if prompt == NONE:
        return FeedbackResult(skipped=True, parent_run_id=parent_run_id)

    artifacts = FileArtifactStore(database, artifact_root)
    store = ResearchStore(database)
    audit = SQLiteAuditLog(database)
    user_store = UserStore(database)
    extract_llm = extract_llm_adapter or llm_adapter

    bundle = await store.get_run(parent_run_id)
    if bundle is None:
        raise FeedbackError("parent run not found: {}".format(parent_run_id))
    if bundle["status"] not in {"success", "degraded"}:
        raise FeedbackError(
            "parent run is not successful: {}".format(bundle["status"])
        )
    parent_run = RunRecord.model_validate(bundle["run"])
    parent_decision = (
        Decision.model_validate(bundle["decisions"][0])
        if bundle["decisions"]
        else None
    )
    parent_reports = _reports_from_bundle(bundle)

    parsed = parse_tags(prompt)
    slots, audit_type = await extract_slots(extract_llm, parsed.body, model)
    parsed = merge_parsed_with_slots(parsed, slots)
    slots = fill_tagged_empty_slots(parsed, slots)
    preview = build_intent(
        utterance_id=str(uuid4()), parsed=parsed, slots=slots
    )
    schedule = should_schedule_rerun(preview)
    rerun_dims = compute_rerun_dims(preview) if schedule else []
    decision_only = schedule and is_decision_only_rerun(preview)

    target_run = parent_run
    if schedule:
        target_run = RunRecord(
            run_id=str(uuid4()),
            stock_code=parent_run.stock_code,
            thesis_id=parent_run.thesis_id,
            status="running",
            snapshot_refs=dict(parent_run.snapshot_refs),
            session_ids={},
            parent_run_id=parent_run.run_id,
        )
        await store.save_run(target_run)
        await audit.append(
            "run_started",
            {
                "stock_code": target_run.stock_code,
                "parent_run_id": parent_run.run_id,
                "feedback": True,
            },
            run_id=target_run.run_id,
        )

    intent, show_echo = await _commit_intent(
        parsed=parsed,
        slots=slots,
        audit_type=audit_type,
        user_id=user_id,
        run=target_run,
        code=parent_run.stock_code,
        user_store=user_store,
        audit=audit,
        moment="post_decision",
    )

    if not schedule:
        if parsed.effect in {"rerun", "remember_rerun"}:
            await audit.append(
                "rerun_noop",
                {"effect": intent.effect, "tags": intent.tags},
                run_id=parent_run.run_id,
            )
            return FeedbackResult(
                noop=True,
                parent_run_id=parent_run.run_id,
                intent=intent,
                show_intent_echo=show_echo,
            )
        return FeedbackResult(
            parent_run_id=parent_run.run_id,
            intent=intent,
            show_intent_echo=show_echo,
        )

    reports = dict(parent_reports)
    copied = [
        dim
        for dim in ("fundamental", "technical", "sentiment", "macro")
        if dim not in rerun_dims
    ]
    snapshot = None
    tech_fields = None
    sent_fields = None
    macro_fields = None
    try:
        snapshot, tech_fields, sent_fields, macro_fields = await _load_frozen_snapshots(
            artifacts, parent_run
        )
        prefs = {}
        for dim in ("fundamental", "technical", "sentiment", "macro", "decision"):
            prefs[dim] = await user_store.active_preferences(
                user_id=user_id,
                scope=dim,
                stock_code=parent_run.stock_code,
            )
        if "fundamental" in rerun_dims:
            if snapshot is None:
                reports["fundamental"] = _abstain_for_missing(
                    FundamentalSnapshot(stock_code=parent_run.stock_code)
                )
                await audit.append(
                    "agent_failed",
                    {"agent": "fundamental", "message": "missing snapshot"},
                    run_id=target_run.run_id,
                )
            else:
                report, _ = await _run_fundamental_agent(
                    snapshot=snapshot,
                    run=target_run,
                    database=database,
                    artifacts=artifacts,
                    llm_adapter=llm_adapter,
                    model=model,
                    thinking_enabled=thinking_enabled,
                    user_query=_feedback_query(
                        target_run, snapshot, intent, prefs["fundamental"], "fundamental"
                    ),
                )
                try:
                    bind_report_evidence(report, snapshot)
                except EvidenceBindingError as error:
                    if unbound_policy == "abstain":
                        report = _abstain_for_unbound(snapshot, error)
                    else:
                        raise
                reports["fundamental"] = report
                await audit.append(
                    "agent_completed",
                    {
                        "status": (
                            "completed" if not report.abstain else "abstained"
                        ),
                        "stance": report.stance,
                        "score": report.score,
                    },
                    run_id=target_run.run_id,
                    session_id=target_run.session_ids.get("fundamental"),
                )
        if "technical" in rerun_dims:
            reports["technical"] = await _rerun_or_abstain_technical(
                tech_fields,
                target_run,
                database,
                technical_llm_adapter or llm_adapter,
                model,
                thinking_enabled,
                _feedback_query(
                    target_run, snapshot, intent, prefs["technical"], "technical"
                ),
                audit,
            )
        if "sentiment" in rerun_dims:
            reports["sentiment"] = await _rerun_or_abstain_sentiment(
                sent_fields,
                target_run,
                database,
                sentiment_llm_adapter or llm_adapter,
                model,
                thinking_enabled,
                _feedback_query(
                    target_run, snapshot, intent, prefs["sentiment"], "sentiment"
                ),
                audit,
            )
        if "macro" in rerun_dims:
            reports["macro"] = await _rerun_or_abstain_macro(
                macro_fields,
                target_run,
                database,
                macro_llm_adapter or llm_adapter,
                model,
                thinking_enabled,
                _feedback_query(
                    target_run, snapshot, intent, prefs["macro"], "macro"
                ),
                audit,
                unbound_policy,
            )

        for dim in copied:
            if dim in reports:
                await audit.append(
                    "report_copied",
                    {"role": dim, "from_run_id": parent_run.run_id},
                    run_id=target_run.run_id,
                )

        fund = reports.get("fundamental") or _abstain_for_missing(
            snapshot or FundamentalSnapshot(stock_code=parent_run.stock_code)
        )
        tech = reports.get("technical") or _abstain_technical()
        sent = reports.get("sentiment") or _abstain_sentiment()
        macro = reports.get("macro") or _abstain_macro()
        if snapshot is None:
            snapshot = FundamentalSnapshot(stock_code=parent_run.stock_code)

        decision = build_multi_factor_decision(
            fund,
            tech,
            sent,
            macro,
            snapshot,
            user_constraint=_decision_constraint(intent, prefs["decision"]),
        )
        rerun_label = "、".join(rerun_dims) if rerun_dims else "decision"
        decision = decision.model_copy(
            update={
                "rationale": "基于 run {} 的反馈重跑：{}。{}".format(
                    parent_run.run_id, rerun_label, decision.rationale
                )
            }
        )
        status = "success"
        if any(
            report_degrades_run(dim, report)
            for dim, report in [
                ("fundamental", fund),
                ("technical", tech),
                ("sentiment", sent),
                ("macro", macro),
            ]
        ):
            status = "degraded"
        outcome = await _complete(
            store,
            audit,
            target_run,
            snapshot,
            fund,
            tech,
            sent,
            macro,
            decision,
            status=status,
            intent=intent,
            show_intent_echo=show_echo,
        )
        outcome.parent_run_id = parent_run.run_id
        outcome.rerun_dimensions = list(rerun_dims)
        outcome.copied_dimensions = list(copied)
        outcome.parent_decision = parent_decision
        return FeedbackResult(
            parent_run_id=parent_run.run_id,
            intent=intent,
            show_intent_echo=show_echo,
            outcome=outcome,
        )
    except Exception as error:
        target_run.status = "failed"
        target_run.updated_at = utc_now()
        await store.save_run(target_run)
        await audit.append(
            "run_failed",
            {"type": type(error).__name__, "message": str(error)},
            run_id=target_run.run_id,
        )
        return FeedbackResult(
            parent_run_id=parent_run.run_id,
            intent=intent,
            show_intent_echo=show_echo,
            error="{}: {}".format(type(error).__name__, error),
            outcome=AnalysisOutcome(
                run=target_run,
                snapshot=snapshot
                if snapshot is not None
                else FundamentalSnapshot(stock_code=parent_run.stock_code),
                report=parent_reports.get("fundamental"),
                technical_report=parent_reports.get("technical"),
                sentiment_report=parent_reports.get("sentiment"),
                macro_report=parent_reports.get("macro"),
                decision=None,
                error="{}: {}".format(type(error).__name__, error),
                intent=intent,
                show_intent_echo=show_echo,
                parent_run_id=parent_run.run_id,
            ),
        )


def _reports_from_bundle(bundle: Dict[str, Any]) -> Dict[str, Report]:
    reports: Dict[str, Report] = {}
    for payload in bundle.get("reports") or []:
        report = Report.model_validate(payload)
        reports[report.role] = report
    return reports


def _feedback_query(run, snapshot, intent, prefs, dim: str) -> str:
    as_of = snapshot.as_of.isoformat() if snapshot is not None else utc_now().isoformat()
    return build_expert_user_query(
        run_id=run.run_id,
        stock_code=run.stock_code,
        as_of=as_of,
        dim=dim,
        intent=intent,
        preference_statements=[item.statement for item in prefs],
    )


async def _load_frozen_snapshots(
    artifacts: FileArtifactStore, parent: RunRecord
):
    refs = parent.snapshot_refs or {}

    async def _load(key: str):
        ref = refs.get(key)
        if not ref:
            return None
        return json.loads(await artifacts.get(ref))

    fund_payload = await _load("fundamental")
    snapshot = None
    if fund_payload:
        snapshot = apply_fundamental_rules(
            FundamentalSnapshot.model_validate(fund_payload)
        )
    return (
        snapshot,
        await _load("technical"),
        await _load("sentiment"),
        await _load("macro"),
    )


async def _rerun_or_abstain_technical(
    tech_fields, run, database, llm, model, thinking, query, audit
) -> Report:
    if not tech_fields:
        report = _abstain_technical()
        await audit.append(
            "agent_failed",
            {"agent": "technical", "message": "missing snapshot"},
            run_id=run.run_id,
        )
        return report
    try:
        report, _ = await _run_technical_agent(
            tech_fields=tech_fields,
            run=run,
            database=database,
            llm_adapter=llm,
            model=model,
            thinking_enabled=thinking,
            user_query=query,
        )
        await audit.append(
            "agent_completed",
            {
                "status": "completed" if not report.abstain else "abstained",
                "stance": report.stance,
                "score": report.score,
            },
            run_id=run.run_id,
            session_id=run.session_ids.get("technical"),
        )
        return report
    except Exception as error:
        await audit.append(
            "agent_failed",
            {
                "agent": "technical",
                "error_type": type(error).__name__,
                "message": str(error),
            },
            run_id=run.run_id,
        )
        return _abstain_agent_failure("technical", error)


async def _rerun_or_abstain_sentiment(
    sent_fields, run, database, llm, model, thinking, query, audit
) -> Report:
    if not sent_fields:
        report = _abstain_sentiment()
        await audit.append(
            "agent_failed",
            {"agent": "sentiment", "message": "missing snapshot"},
            run_id=run.run_id,
        )
        return report
    try:
        report, _ = await _run_sentiment_agent(
            sent_fields=sent_fields,
            run=run,
            database=database,
            llm_adapter=llm,
            model=model,
            thinking_enabled=thinking,
            user_query=query,
        )
        await audit.append(
            "agent_completed",
            {
                "status": "completed" if not report.abstain else "abstained",
                "stance": report.stance,
                "score": report.score,
            },
            run_id=run.run_id,
            session_id=run.session_ids.get("sentiment"),
        )
        return report
    except Exception as error:
        await audit.append(
            "agent_failed",
            {
                "agent": "sentiment",
                "error_type": type(error).__name__,
                "message": str(error),
            },
            run_id=run.run_id,
        )
        return _abstain_agent_failure("sentiment", error)


async def _rerun_or_abstain_macro(
    macro_fields, run, database, llm, model, thinking, query, audit, unbound_policy
) -> Report:
    if not macro_fields:
        report = _abstain_macro()
        await audit.append(
            "agent_failed",
            {"agent": "macro", "message": "missing snapshot"},
            run_id=run.run_id,
        )
        return report
    try:
        report, _ = await _run_macro_agent(
            macro_fields=macro_fields,
            run=run,
            database=database,
            llm_adapter=llm,
            model=model,
            thinking_enabled=thinking,
            user_query=query,
        )
        try:
            bind_macro_report_evidence(
                report, MacroSnapshot(**macro_fields["macro"])
            )
        except EvidenceBindingError:
            if unbound_policy == "abstain":
                report = _abstain_macro("证据绑定失败，改为弃权。")
            else:
                raise
        await audit.append(
            "agent_completed",
            {
                "status": "completed" if not report.abstain else "abstained",
                "stance": report.stance,
                "score": report.score,
            },
            run_id=run.run_id,
            session_id=run.session_ids.get("macro"),
        )
        return report
    except Exception as error:
        await audit.append(
            "agent_failed",
            {
                "agent": "macro",
                "error_type": type(error).__name__,
                "message": str(error),
            },
            run_id=run.run_id,
        )
        return _abstain_agent_failure("macro", error)


async def _commit_intent(
    *,
    parsed,
    slots,
    audit_type: str,
    user_id: str,
    run: RunRecord,
    code: str,
    user_store: UserStore,
    audit: SQLiteAuditLog,
    moment: Moment = "pre_run",
):
    utterance_id = str(uuid4())
    intent = build_intent(
        utterance_id=utterance_id, parsed=parsed, slots=slots
    )
    utterance = build_utterance(
        utterance_id=utterance_id,
        intent=intent,
        parsed=parsed,
        stock_code=code,
        thesis_id=run.thesis_id,
        run_id=run.run_id,
        created_at=utc_now().isoformat(),
        user_id=user_id,
        moment=moment,
    )
    await user_store.save_utterance(utterance)
    await user_store.save_intent(intent)
    await audit.append(
        audit_type,
        {
            "intent_id": intent.intent_id,
            "effect": intent.effect,
            "tags": intent.tags,
        },
        run_id=run.run_id,
    )
    await user_store.persist_remember(intent=intent, utterance=utterance)
    return intent, parsed.body != NONE


async def _prepare_intent(
    *,
    user_prompt: str,
    user_id: str,
    run: RunRecord,
    code: str,
    llm_adapter: LLMAdapter,
    model: str,
    user_store: UserStore,
    audit: SQLiteAuditLog,
    moment: Moment = "pre_run",
):
    parsed = parse_tags(user_prompt)
    slots, audit_type = await extract_slots(llm_adapter, parsed.body, model)
    parsed = merge_parsed_with_slots(parsed, slots)
    slots = fill_tagged_empty_slots(parsed, slots)
    return await _commit_intent(
        parsed=parsed,
        slots=slots,
        audit_type=audit_type,
        user_id=user_id,
        run=run,
        code=code,
        user_store=user_store,
        audit=audit,
        moment=moment,
    )


def _activate_catalog(database: SQLiteDatabase):
    catalog = MethodologyCatalog(database)
    catalog.ensure_seeded()
    return bind_catalog(catalog)


def _decision_constraint(intent, prefs) -> str:
    parts = []
    if intent.decision != NONE:
        parts.append(intent.decision[:200])
    for item in prefs:
        parts.append(item.statement[:80])
    if not parts:
        return NONE
    return "；".join(parts)


async def _prepare_session(
    *,
    database: SQLiteDatabase,
    agent_name: str,
    run: RunRecord,
    stock_code: str,
    user_query: str,
) -> tuple[str, Optional[str], str]:
    parent_id, memory = await prior_session_memory(
        SQLiteStateStore(database),
        agent_name=agent_name,
        thesis_id=run.thesis_id,
    )
    session_id = str(uuid4())
    run.session_ids[agent_name] = session_id
    return session_id, parent_id, attach_prior_memory(user_query, memory)


async def _run_fundamental_agent(
    *,
    snapshot: FundamentalSnapshot,
    run: RunRecord,
    database: SQLiteDatabase,
    artifacts: FileArtifactStore,
    llm_adapter: LLMAdapter,
    model: str,
    thinking_enabled: bool,
    user_query: str,
    runtime_config: Optional[RuntimeConfig] = None,
    return_raw: bool = False,
) -> tuple[Any, str]:
    session_id, parent_id, user_query = await _prepare_session(
        database=database,
        agent_name="fundamental",
        run=run,
        stock_code=snapshot.stock_code,
        user_query=user_query,
    )
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=llm_adapter,
        config=runtime_config
        or fundamental_runtime_config(
            model=model, thinking_enabled=thinking_enabled
        ),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    ctx = FundamentalToolContext(snapshot=snapshot, artifacts=artifacts)
    register_fundamental_tools(agent, ctx)
    token = _activate_catalog(database)
    try:
        final = await agent.run(
            user_query,
            session_id=session_id,
            parent_session_id=parent_id,
            business_context={
                "stock_code": snapshot.stock_code,
                "thesis_id": run.thesis_id,
                "run_id": run.run_id,
            },
        )
    except (InvalidModelOutputError, StateConflictError, ValidationError):
        raise
    finally:
        reset_catalog(token)
    if return_raw:
        return final.output, session_id
    return (
        drop_unretrieved_kb(
            sanitize_report(parse_report(final.output)),
            ctx.retrieved_kb_ids,
        ),
        session_id,
    )


def _severely_missing(snapshot: FundamentalSnapshot) -> bool:
    return all(
        getattr(snapshot, name) is None for name in REQUIRED_FIELDS
    )


def _abstain_for_missing(snapshot: FundamentalSnapshot) -> Report:
    return Report(
        role="fundamental",
        score=3,
        stance="abstain",
        summary="关键财务字段缺失，无法形成基本面判断。",
        citations=[],
        risks=["关键财务数据缺失", "三维未评估，时机不明"],
        degraded=True,
        abstain=True,
        missing_information=list(snapshot.missing_fields),
    )


def _abstain_for_unbound(
    snapshot: FundamentalSnapshot, error: EvidenceBindingError
) -> Report:
    return Report(
        role="fundamental",
        score=3,
        stance="abstain",
        summary="证据绑定失败，改为弃权。",
        citations=[],
        risks=["报告数字无法对应 snapshot", "三维未评估，时机不明"],
        degraded=True,
        abstain=True,
        missing_information=["unbound_numbers"],
    )


def _abstain_technical() -> Report:
    return Report(
        role="technical",
        score=3,
        stance="abstain",
        summary="技术指标数据不足，无法形成技术面判断。",
        citations=[],
        risks=["技术指标缺失", "关键位未评估"],
        degraded=True,
        abstain=True,
        missing_information=["indicator", "kline"],
    )


def _abstain_sentiment() -> Report:
    return Report(
        role="sentiment",
        score=3,
        stance="abstain",
        summary="事件数据不足，无法形成情绪面判断。",
        citations=[],
        risks=["事件信息缺失", "情绪面未评估"],
        degraded=True,
        abstain=True,
        missing_information=["events", "holders"],
    )


def _abstain_macro(summary: str = "宏观数据不足或与个股相关性低，无法形成环境标签。") -> Report:
    return Report(
        role="macro",
        score=2,
        stance="abstain",
        summary=summary,
        citations=[],
        risks=["宏观环境未形成有效参考", "利率数据或行业相关性不足"],
        degraded=True,
        abstain=True,
        missing_information=["macro"],
        cycle_tag="insufficient",
        relevance_to_stock="unknown",
    )


def _abstain_agent_failure(role: str, error: BaseException) -> Report:
    detail = "{}: {}".format(type(error).__name__, error).replace("\n", " ")
    if len(detail) > 240:
        detail = detail[:237] + "..."
    score = 2 if role == "macro" else 3
    extra: Dict[str, Any] = {}
    if role == "macro":
        extra["cycle_tag"] = "insufficient"
        extra["relevance_to_stock"] = "unknown"
    return Report(
        role=role,  # type: ignore[arg-type]
        score=score,
        stance="abstain",
        summary="本维分析失败，原因：{}。".format(detail),
        citations=[],
        risks=["本维未能完成评估", type(error).__name__],
        degraded=True,
        abstain=True,
        missing_information=["agent_error"],
        **extra,
    )


async def _immediate_macro_abstention() -> tuple[Report, str]:
    return _abstain_macro(), ""


async def _run_technical_agent(
    *,
    tech_fields: Dict[str, Any],
    run: RunRecord,
    database: SQLiteDatabase,
    llm_adapter: LLMAdapter,
    model: str,
    thinking_enabled: bool,
    user_query: str,
    runtime_config: Optional[RuntimeConfig] = None,
    return_raw: bool = False,
) -> tuple[Any, str]:
    session_id, parent_id, user_query = await _prepare_session(
        database=database,
        agent_name="technical",
        run=run,
        stock_code=str(tech_fields.get("stock_code") or run.stock_code),
        user_query=user_query,
    )
    indicator = IndicatorSnapshot(**tech_fields["indicator"])
    price = PriceSnapshot(**tech_fields["price"])
    kline = KlineSnapshot(**tech_fields["kline"])
    tech_rules = apply_technical_rules(indicator, price, kline)
    ctx = TechnicalToolContext(
        indicator=indicator, price=price, kline=kline
    )
    agent = AgentInstance(
        name="technical",
        llm_adapter=llm_adapter,
        config=runtime_config
        or technical_runtime_config(
            model=model, thinking_enabled=thinking_enabled
        ),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    register_technical_tools(agent, ctx)
    token = _activate_catalog(database)
    try:
        final = await agent.run(
            user_query,
            session_id=session_id,
            parent_session_id=parent_id,
            business_context={
                "stock_code": indicator.stock_code,
                "thesis_id": run.thesis_id,
                "run_id": run.run_id,
            },
        )
    except (InvalidModelOutputError, StateConflictError, ValidationError):
        raise
    finally:
        reset_catalog(token)
    if return_raw:
        return final.output, session_id
    report = apply_computed_technical_semantics(
        drop_unretrieved_kb(
            sanitize_report(parse_technical_report(final.output)),
            ctx.retrieved_kb_ids,
        ),
        tech_rules,
    )
    return report, session_id


async def _run_sentiment_agent(
    *,
    sent_fields: Dict[str, Any],
    run: RunRecord,
    database: SQLiteDatabase,
    llm_adapter: LLMAdapter,
    model: str,
    thinking_enabled: bool,
    user_query: str,
    runtime_config: Optional[RuntimeConfig] = None,
    return_raw: bool = False,
) -> tuple[Any, str]:
    events = EventSnapshot(**sent_fields["events"])
    holders = HolderChangeSnapshot(**sent_fields["holders"])
    session_id, parent_id, user_query = await _prepare_session(
        database=database,
        agent_name="sentiment",
        run=run,
        stock_code=events.stock_code,
        user_query=user_query,
    )
    ctx = SentimentToolContext(events=events, holders=holders)
    agent = AgentInstance(
        name="sentiment",
        llm_adapter=llm_adapter,
        config=runtime_config
        or sentiment_runtime_config(
            model=model, thinking_enabled=thinking_enabled
        ),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    register_sentiment_tools(agent, ctx)
    token = _activate_catalog(database)
    try:
        final = await agent.run(
            user_query,
            session_id=session_id,
            parent_session_id=parent_id,
            business_context={
                "stock_code": events.stock_code,
                "thesis_id": run.thesis_id,
                "run_id": run.run_id,
            },
        )
    except (InvalidModelOutputError, StateConflictError, ValidationError):
        raise
    finally:
        reset_catalog(token)
    if return_raw:
        return final.output, session_id
    flags = apply_event_rules(events, holders)["flags"]
    report = apply_computed_sentiment_semantics(
        drop_unretrieved_kb(
            sanitize_report(parse_sentiment_report(final.output)),
            ctx.retrieved_kb_ids,
        ),
        flags,
    )
    return report, session_id


async def _run_macro_agent(
    *,
    macro_fields: Dict[str, Any],
    run: RunRecord,
    database: SQLiteDatabase,
    llm_adapter: LLMAdapter,
    model: str,
    thinking_enabled: bool,
    user_query: str,
    runtime_config: Optional[RuntimeConfig] = None,
    return_raw: bool = False,
) -> tuple[Any, str]:
    session_id, parent_id, user_query = await _prepare_session(
        database=database,
        agent_name="macro",
        run=run,
        stock_code=str(macro_fields["stock_code"]),
        user_query=user_query,
    )
    macro = MacroSnapshot(**macro_fields["macro"])
    ctx = MacroToolContext(
        macro=macro,
        industry=macro_fields["industry"],
        stock_code=macro_fields["stock_code"],
        company_name=macro_fields["company_name"],
    )
    agent = AgentInstance(
        name="macro",
        llm_adapter=llm_adapter,
        config=runtime_config
        or macro_runtime_config(model=model, thinking_enabled=thinking_enabled),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    register_macro_tools(agent, ctx)
    token = _activate_catalog(database)
    try:
        final = await agent.run(
            user_query,
            session_id=session_id,
            parent_session_id=parent_id,
            business_context={
                "stock_code": macro_fields["stock_code"],
                "thesis_id": run.thesis_id,
                "run_id": run.run_id,
            },
        )
    except (InvalidModelOutputError, StateConflictError, ValidationError):
        raise
    finally:
        reset_catalog(token)
    if return_raw:
        return final.output, session_id
    report = drop_unretrieved_kb(
        sanitize_report(
            apply_computed_macro_semantics(
                parse_macro_report(final.output),
                macro=macro,
            )
        ),
        ctx.retrieved_kb_ids,
    )
    return report, session_id


async def _complete(
    store: ResearchStore,
    audit: SQLiteAuditLog,
    run: RunRecord,
    snapshot: FundamentalSnapshot,
    fundamental_report: Report,
    technical_report: Optional[Report],
    sentiment_report: Optional[Report],
    macro_report: Optional[Report],
    decision: Decision,
    *,
    status: str,
    intent: Optional[UserIntent] = None,
    show_intent_echo: bool = False,
) -> AnalysisOutcome:
    await store.save_report(run.run_id, "fundamental", fundamental_report)
    if technical_report is not None:
        await store.save_report(run.run_id, "technical", technical_report)
    if sentiment_report is not None:
        await store.save_report(run.run_id, "sentiment", sentiment_report)
    if macro_report is not None:
        await store.save_report(run.run_id, "macro", macro_report)
    await store.save_decision(run.run_id, decision)
    run.status = status
    run.updated_at = utc_now()
    await store.save_run(run)
    await audit.append(
        "decision_written",
        {
            "rating": decision.rating,
            "confidence": decision.confidence,
            "value_score": decision.value_score,
            "timing_score": decision.timing_score,
            "dimensions_used": decision.dimensions_used,
            "dimensions_missing": decision.dimensions_missing,
        },
        run_id=run.run_id,
        session_id=run.session_ids.get("fundamental"),
    )
    return AnalysisOutcome(
        run=run,
        snapshot=snapshot,
        report=fundamental_report,
        technical_report=technical_report,
        sentiment_report=sentiment_report,
        macro_report=macro_report,
        decision=decision,
        intent=intent,
        show_intent_echo=show_intent_echo,
    )


def format_cli_text(outcome: AnalysisOutcome) -> str:
    report = outcome.report
    technical_report = outcome.technical_report
    sentiment_report = outcome.sentiment_report
    macro_report = outcome.macro_report
    decision = outcome.decision
    company = outcome.snapshot.company_name or "-"
    lines = [
        "run_id: {}".format(outcome.run.run_id),
        "thesis_id: {}".format(outcome.run.thesis_id),
        "公司 / 代码: {} / {}".format(company, outcome.run.stock_code),
        "run_status: {}".format(outcome.run.status),
    ]
    if outcome.parent_run_id:
        lines.append("parent_run_id: {}".format(outcome.parent_run_id))
    if outcome.show_intent_echo and outcome.intent is not None:
        lines.append(format_intent_echo(outcome.intent))
    if report:
        lines.extend(
            [
                "基本面 stance / score / abstain: {} / {} / {}".format(
                    report.stance, report.score, report.abstain
                ),
                "summary: {}".format(report.summary),
            ]
        )
    if technical_report:
        lines.extend(
            [
                "技术面 stance / score / abstain: {} / {} / {}".format(
                    technical_report.stance,
                    technical_report.score,
                    technical_report.abstain,
                ),
                "技术面 summary: {}".format(technical_report.summary),
            ]
        )
    if sentiment_report:
        lines.extend(
            [
                "情绪面 stance / abstain: {} / {}".format(
                    sentiment_report.stance, sentiment_report.abstain
                ),
                "情绪面 summary: {}".format(sentiment_report.summary),
            ]
        )
    if macro_report:
        lines.extend(
            [
                "宏观 stance / score / abstain / relevance: {} / {} / {} / {}".format(
                    macro_report.stance,
                    macro_report.score,
                    macro_report.abstain,
                    macro_report.relevance_to_stock or "unknown",
                ),
                "宏观 summary: {}".format(macro_report.summary),
            ]
        )
    if decision:
        timing = (
            "未评估"
            if decision.timing_score is None
            else str(decision.timing_score)
        )
        lines.extend(
            [
                "决策 rating / confidence / value_score / timing_score: "
                "{} / {} / {} / {}".format(
                    decision.rating,
                    decision.confidence,
                    decision.value_score,
                    timing,
                ),
                "falsifiers:",
            ]
        )
        lines.extend("  - {}".format(item) for item in decision.falsifiers)
        lines.append("风险:")
        lines.extend("  - {}".format(item) for item in decision.risks)
        lines.append("一句话建议: {}".format(decision.advice_one_liner))
        if outcome.parent_decision is not None:
            lines.extend(
                [
                    "反馈前 rating / confidence: {} / {}".format(
                        outcome.parent_decision.rating,
                        outcome.parent_decision.confidence,
                    ),
                    "反馈后 rating / confidence: {} / {}".format(
                        decision.rating,
                        decision.confidence,
                    ),
                    "本次重跑: {}".format(
                        "、".join(outcome.rerun_dimensions) or "decision"
                    ),
                ]
            )
    if outcome.error:
        lines.append("error: {}".format(outcome.error))
    if outcome.run.status in {"success", "degraded"} and not outcome.error:
        lines.append(
            '可对本次结果反馈：python -m insightagent feedback {} --prompt "..."'.format(
                outcome.run.run_id
            )
        )
    lines.append("免责：研究辅助，非投资建议")
    return "\n".join(lines)


def format_feedback_result(result: FeedbackResult) -> str:
    if result.skipped:
        return ""
    if result.outcome is not None:
        return format_cli_text(result.outcome)
    lines = []
    if result.show_intent_echo and result.intent is not None:
        lines.append(format_intent_echo(result.intent))
    lines.append(
        "未重跑，父 run {} 结论未改".format(result.parent_run_id or "-")
    )
    if result.noop:
        lines.append("rerun_noop")
    return "\n".join(lines)


def write_run_json(outcome: AnalysisOutcome, database_path: str) -> Path:
    target = Path(database_path).expanduser().resolve().parent / "runs"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "{}.json".format(outcome.run.run_id)
    path.write_text(
        json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
