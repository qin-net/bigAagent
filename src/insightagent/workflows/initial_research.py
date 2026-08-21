from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import ValidationError

from ..business_contracts import Decision, Report, RunRecord
from ..contracts import utc_now
from ..data_contracts import EventSnapshot, HolderChangeSnapshot, IndicatorSnapshot, KlineSnapshot, PriceSnapshot
from ..decision import build_multi_factor_decision
from ..evidence import EvidenceBindingError, bind_report_evidence
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
    AkshareSentimentAdapter,
    AkshareTechnicalAdapter,
    FixtureFundamentalAdapter,
    FixtureSentimentAdapter,
    FixtureTechnicalAdapter,
    FundamentalSnapshot,
    MarketDataAdapter,
    apply_fundamental_rules,
)
from ..llm import LLMAdapter
from ..market import synthetic_market_fixture
from ..persistence import (
    FileArtifactStore,
    SQLiteAuditLog,
    SQLiteContextArchive,
    SQLiteDatabase,
    SQLiteStateStore,
)
from ..research_store import ResearchStore
from ..retry import ExponentialBackoff
from ..runtime import AgentInstance, InvalidModelOutputError
from ..state import StateConflictError
from ..sentiment_agent import (
    SentimentToolContext,
    parse_sentiment_report,
    register_sentiment_tools,
    sentiment_runtime_config,
)
from ..user_contracts import NONE
from ..user_intent import (
    build_expert_user_query,
    build_intent,
    build_utterance,
    extract_slots,
    parse_tags,
)
from ..user_store import UserStore
from ..technical_agent import (
    TechnicalToolContext,
    parse_technical_report,
    register_technical_tools,
    technical_runtime_config,
)

STOCK_CODE_RE = re.compile(r"^\d{6}$")
UnboundPolicy = Literal["fail", "abstain"]


class InvalidStockCodeError(ValueError):
    pass


@dataclass
class AnalysisOutcome:
    run: RunRecord
    snapshot: FundamentalSnapshot
    report: Optional[Report]
    technical_report: Optional[Report] = None
    sentiment_report: Optional[Report] = None
    decision: Optional[Decision] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
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
            "decision": (
                self.decision.model_dump(mode="json")
                if self.decision
                else None
            ),
            "error": self.error,
        }


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
    technical_llm_adapter: Optional[LLMAdapter] = None,
    sentiment_llm_adapter: Optional[LLMAdapter] = None,
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
    intent = await _prepare_intent(
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
    decision_prefs = await user_store.active_preferences(
        user_id=user_id, scope="decision", stock_code=code
    )

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
        technical_adapter = build_market_adapter_for(
            fixture=fixture,
            dimension="technical",
            fixture_payload=tech_fixture_payload,
        )
        sentiment_adapter = build_market_adapter_for(
            fixture=fixture,
            dimension="sentiment",
            fixture_payload=sent_fixture_payload,
        )

        tech_fields = await fetch_retry.execute(
            technical_adapter.fetch_technical, code
        )
        sent_fields = await fetch_retry.execute(
            sentiment_adapter.fetch_sentiment, code
        )
        tech_artifact_ref = await artifacts.put(
            json.dumps(tech_fields, ensure_ascii=False)
        )
        sent_artifact_ref = await artifacts.put(
            json.dumps(sent_fields, ensure_ascii=False)
        )
        run.snapshot_refs = {
            "fundamental": fundamental_artifact_ref,
            "technical": tech_artifact_ref,
            "sentiment": sent_artifact_ref,
        }
        run.updated_at = utc_now()
        await store.save_run(run)
        await audit.append(
            "snapshot_ready",
            {
                "fundamental_artifact_ref": fundamental_artifact_ref,
                "technical_artifact_ref": tech_artifact_ref,
                "sentiment_artifact_ref": sent_artifact_ref,
                "missing_fields": snapshot.missing_fields,
                "computed_flags": snapshot.computed_flags,
            },
            run_id=run.run_id,
        )

        if _severely_missing(snapshot):
            fundamental_report = _abstain_for_missing(snapshot)
            technical_report = _abstain_technical()
            sentiment_report = _abstain_sentiment()
            decision = build_multi_factor_decision(
                fundamental_report,
                technical_report,
                sentiment_report,
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
                decision,
                status="degraded",
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
            artifacts=artifacts,
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
            artifacts=artifacts,
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

        fundamental_result, technical_result, sentiment_result = (
            await asyncio.gather(
                fundamental_future,
                technical_future,
                sentiment_future,
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

        if fundamental_exception is not None:
            raise fundamental_exception

        fundamental_report, _ = fundamental_result
        technical_report = (
            technical_result[0]
            if not isinstance(technical_result, Exception)
            else _abstain_technical()
        )
        sentiment_report = (
            sentiment_result[0]
            if not isinstance(sentiment_result, Exception)
            else _abstain_sentiment()
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

        try:
            bind_report_evidence(fundamental_report, snapshot)
        except EvidenceBindingError as error:
            if unbound_policy == "abstain":
                fundamental_report = _abstain_for_unbound(snapshot, error)
            else:
                raise

        decision = build_multi_factor_decision(
            fundamental_report,
            technical_report,
            sentiment_report,
            snapshot,
            user_constraint=_decision_constraint(intent, decision_prefs),
        )
        status = "success"
        if any(
            r.abstain or r.degraded
            for r in [
                fundamental_report,
                technical_report,
                sentiment_report,
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
            decision,
            status=status,
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
        return AnalysisOutcome(
            run=run,
            snapshot=locals().get(
                "snapshot",
                FundamentalSnapshot(stock_code=code),
            ),
            report=None,
            technical_report=None,
            sentiment_report=None,
            decision=None,
            error="{}: {}".format(type(error).__name__, error),
        )


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
):
    parsed = parse_tags(user_prompt)
    utterance_id = str(uuid4())
    slots, audit_type = await extract_slots(llm_adapter, parsed.body, model)
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
    return intent


def _decision_constraint(intent, prefs) -> str:
    parts = []
    if intent.decision != NONE:
        parts.append(intent.decision[:200])
    for item in prefs:
        parts.append(item.statement[:80])
    if not parts:
        return NONE
    return "；".join(parts)


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
) -> tuple[Report, str]:
    session_id = str(uuid4())
    run.session_ids["fundamental"] = session_id
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=llm_adapter,
        config=fundamental_runtime_config(
            model=model, thinking_enabled=thinking_enabled
        ),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    register_fundamental_tools(
        agent, FundamentalToolContext(snapshot=snapshot, artifacts=artifacts)
    )
    try:
        final = await agent.run(
            user_query,
            session_id=session_id,
            business_context={
                "stock_code": snapshot.stock_code,
                "thesis_id": run.thesis_id,
                "run_id": run.run_id,
            },
        )
    except (InvalidModelOutputError, StateConflictError, ValidationError):
        raise
    return parse_report(final.output), session_id


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


async def _run_technical_agent(
    *,
    tech_fields: Dict[str, Any],
    run: RunRecord,
    database: SQLiteDatabase,
    artifacts: FileArtifactStore,
    llm_adapter: LLMAdapter,
    model: str,
    thinking_enabled: bool,
    user_query: str,
) -> tuple[Report, str]:
    session_id = str(uuid4())
    run.session_ids["technical"] = session_id
    indicator = IndicatorSnapshot(**tech_fields["indicator"])
    price = PriceSnapshot(**tech_fields["price"])
    kline = KlineSnapshot(**tech_fields["kline"])
    agent = AgentInstance(
        name="technical",
        llm_adapter=llm_adapter,
        config=technical_runtime_config(
            model=model, thinking_enabled=thinking_enabled
        ),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    register_technical_tools(
        agent,
        TechnicalToolContext(
            indicator=indicator, price=price, kline=kline, artifacts=artifacts
        ),
    )
    try:
        final = await agent.run(
            user_query,
            session_id=session_id,
            business_context={
                "stock_code": indicator.stock_code,
                "thesis_id": run.thesis_id,
                "run_id": run.run_id,
            },
        )
    except (InvalidModelOutputError, StateConflictError, ValidationError):
        raise
    return parse_technical_report(final.output), session_id


async def _run_sentiment_agent(
    *,
    sent_fields: Dict[str, Any],
    run: RunRecord,
    database: SQLiteDatabase,
    artifacts: FileArtifactStore,
    llm_adapter: LLMAdapter,
    model: str,
    thinking_enabled: bool,
    user_query: str,
) -> tuple[Report, str]:
    session_id = str(uuid4())
    run.session_ids["sentiment"] = session_id
    events = EventSnapshot(**sent_fields["events"])
    holders = HolderChangeSnapshot(**sent_fields["holders"])
    agent = AgentInstance(
        name="sentiment",
        llm_adapter=llm_adapter,
        config=sentiment_runtime_config(
            model=model, thinking_enabled=thinking_enabled
        ),
        state_store=SQLiteStateStore(database),
        context_archive=SQLiteContextArchive(database),
    )
    register_sentiment_tools(
        agent,
        SentimentToolContext(
            events=events, holders=holders, artifacts=artifacts
        ),
    )
    try:
        final = await agent.run(
            user_query,
            session_id=session_id,
            business_context={
                "stock_code": events.stock_code,
                "thesis_id": run.thesis_id,
                "run_id": run.run_id,
            },
        )
    except (InvalidModelOutputError, StateConflictError, ValidationError):
        raise
    return parse_sentiment_report(final.output), session_id


async def _complete(
    store: ResearchStore,
    audit: SQLiteAuditLog,
    run: RunRecord,
    snapshot: FundamentalSnapshot,
    fundamental_report: Report,
    technical_report: Optional[Report],
    sentiment_report: Optional[Report],
    decision: Decision,
    *,
    status: str,
) -> AnalysisOutcome:
    await store.save_report(run.run_id, "fundamental", fundamental_report)
    if technical_report is not None:
        await store.save_report(run.run_id, "technical", technical_report)
    if sentiment_report is not None:
        await store.save_report(run.run_id, "sentiment", sentiment_report)
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
        decision=decision,
    )


def format_cli_text(outcome: AnalysisOutcome) -> str:
    report = outcome.report
    technical_report = outcome.technical_report
    sentiment_report = outcome.sentiment_report
    decision = outcome.decision
    company = outcome.snapshot.company_name or "-"
    lines = [
        "run_id: {}".format(outcome.run.run_id),
        "thesis_id: {}".format(outcome.run.thesis_id),
        "公司 / 代码: {} / {}".format(company, outcome.run.stock_code),
        "run_status: {}".format(outcome.run.status),
    ]
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
    if outcome.error:
        lines.append("error: {}".format(outcome.error))
    lines.append("免责：研究辅助，非投资建议")
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
