from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from uuid import uuid4

from pydantic import ValidationError

from ..business_contracts import Decision, Report, RunRecord
from ..contracts import utc_now
from ..decision import build_p0_decision
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
    FixtureFundamentalAdapter,
    FundamentalSnapshot,
    MarketDataAdapter,
    apply_fundamental_rules,
)
from ..llm import LLMAdapter
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

STOCK_CODE_RE = re.compile(r"^\d{6}$")
UnboundPolicy = Literal["fail", "abstain"]


class InvalidStockCodeError(ValueError):
    pass


@dataclass
class AnalysisOutcome:
    run: RunRecord
    snapshot: FundamentalSnapshot
    report: Optional[Report]
    decision: Optional[Decision]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run": self.run.model_dump(mode="json"),
            "snapshot": self.snapshot.model_dump(mode="json"),
            "report": (
                self.report.model_dump(mode="json") if self.report else None
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
) -> AnalysisOutcome:
    code = normalize_stock_code(stock_code)
    artifacts = FileArtifactStore(database, artifact_root)
    store = ResearchStore(database)
    audit = SQLiteAuditLog(database)
    data_adapter = adapter or build_market_adapter(
        fixture=fixture, fixtures_dir=fixtures_dir
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

    try:
        snapshot = await fetch_retry.execute(
            data_adapter.fetch_fundamental, code
        )
        snapshot = apply_fundamental_rules(snapshot)
        artifact_ref = await artifacts.put(
            json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False)
        )
        snapshot.artifact_ref = artifact_ref
        run.snapshot_refs = {"fundamental": artifact_ref}
        run.updated_at = utc_now()
        await store.save_run(run)
        await audit.append(
            "snapshot_ready",
            {
                "artifact_ref": artifact_ref,
                "missing_fields": snapshot.missing_fields,
                "computed_flags": snapshot.computed_flags,
            },
            run_id=run.run_id,
        )

        if _severely_missing(snapshot):
            report = _abstain_for_missing(snapshot)
            return await _complete(
                store,
                audit,
                run,
                snapshot,
                report,
                status="degraded",
            )

        report = await _run_fundamental_agent(
            snapshot=snapshot,
            run=run,
            database=database,
            artifacts=artifacts,
            llm_adapter=llm_adapter,
            model=model,
            thinking_enabled=thinking_enabled,
        )
        await audit.append(
            "agent_completed",
            {
                "status": "completed" if not report.abstain else "abstained",
                "stance": report.stance,
                "score": report.score,
            },
            run_id=run.run_id,
            session_id=run.session_ids.get("fundamental"),
        )
        try:
            bind_report_evidence(report, snapshot)
        except EvidenceBindingError as error:
            if unbound_policy == "abstain":
                report = _abstain_for_unbound(snapshot, error)
            else:
                raise

        status = "degraded" if report.degraded or report.abstain else "success"
        return await _complete(
            store, audit, run, snapshot, report, status=status
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
            decision=None,
            error="{}: {}".format(type(error).__name__, error),
        )


async def _run_fundamental_agent(
    *,
    snapshot: FundamentalSnapshot,
    run: RunRecord,
    database: SQLiteDatabase,
    artifacts: FileArtifactStore,
    llm_adapter: LLMAdapter,
    model: str,
    thinking_enabled: bool,
) -> Report:
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
    user_query = json.dumps(
        {
            "run_id": run.run_id,
            "stock_code": snapshot.stock_code,
            "as_of": snapshot.as_of.isoformat(),
            "output_schema_version": "1",
            "instruction": (
                "Analyze this stock from the precomputed snapshot. "
                "Call get_fundamental_snapshot, then return the final JSON."
            ),
        },
        ensure_ascii=False,
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
    return parse_report(final.output)


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


async def _complete(
    store: ResearchStore,
    audit: SQLiteAuditLog,
    run: RunRecord,
    snapshot: FundamentalSnapshot,
    report: Report,
    *,
    status: str,
) -> AnalysisOutcome:
    decision = build_p0_decision(report, snapshot)
    await store.save_report(run.run_id, "fundamental", report)
    await store.save_decision(run.run_id, decision)
    run.status = status
    run.updated_at = utc_now()
    await store.save_run(run)
    await audit.append(
        "decision_written",
        {
            "rating": decision.rating,
            "confidence": decision.confidence,
            "timing_score": decision.timing_score,
        },
        run_id=run.run_id,
        session_id=run.session_ids.get("fundamental"),
    )
    return AnalysisOutcome(
        run=run,
        snapshot=snapshot,
        report=report,
        decision=decision,
    )


def format_cli_text(outcome: AnalysisOutcome) -> str:
    report = outcome.report
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
