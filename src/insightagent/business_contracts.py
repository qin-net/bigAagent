from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import StatePatch, utc_now


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_id: str
    kind: Literal["field", "computed", "rule", "kb", "event", "artifact"]
    id: str
    observed_at: datetime = Field(default_factory=utc_now)
    source: str
    note: Optional[str] = None


class RuleHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    hit: bool
    detail: str


class FundamentalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    company_name: Optional[str] = None
    as_of: datetime = Field(default_factory=utc_now)
    price: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None
    pe_percentile_5y: Optional[float] = None
    pb_percentile_5y: Optional[float] = None
    roe: Optional[float] = None
    roe_stable: Optional[bool] = None
    roe_series: List[float] = Field(default_factory=list)
    roe_report_period: Optional[str] = None
    revenue_yoy: Optional[float] = None
    profit_yoy: Optional[float] = None
    gross_margin: Optional[float] = None
    debt_ratio: Optional[float] = None
    current_ratio: Optional[float] = None
    operating_cf: Optional[float] = None
    net_profit: Optional[float] = None
    cashflow_yoy: Optional[float] = None
    ocf_to_np: Optional[float] = None
    goodwill: Optional[float] = None
    non_recurring_profit_ratio: Optional[float] = None
    missing_fields: List[str] = Field(default_factory=list)
    computed_flags: List[str] = Field(default_factory=list)
    rule_hits: List[RuleHit] = Field(default_factory=list)
    artifact_ref: str = ""


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    role: Literal["fundamental", "technical", "sentiment", "macro"]
    score: int = Field(ge=1, le=5)
    stance: Literal["buy", "hold", "sell", "abstain"]
    summary: str
    citations: List[EvidenceRef] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    degraded: bool = False
    abstain: bool = False
    missing_information: List[str] = Field(default_factory=list)
    falsifiers: List[str] = Field(default_factory=list)
    valuation: Optional[str] = None
    financial_health: Optional[str] = None
    earnings_quality: Optional[str] = None
    trend: Optional[str] = None
    setup: Optional[str] = None
    key_levels: Optional[str] = None
    event_flags: List[str] = Field(default_factory=list)
    crowd_risk: Optional[Literal["low", "medium", "high"]] = None
    cycle_tag: Optional[Literal["rate_data_available", "insufficient"]] = None
    market_bias: Optional[Literal["neutral", "unclear"]] = None
    relevance_to_stock: Optional[Literal["high", "low", "unknown"]] = None

    @model_validator(mode="after")
    def validate_abstain_and_citations(self) -> "Report":
        if self.abstain and self.stance != "abstain":
            raise ValueError("abstain=true requires stance=abstain")
        if not self.abstain and not self.citations:
            raise ValueError("non-abstain report requires citations")
        if self.role == "macro" and self.stance not in {"hold", "abstain"}:
            raise ValueError("macro report stance must be hold or abstain")
        return self


_ROLE_ONLY_FIELDS = {
    "fundamental": ("valuation", "financial_health", "earnings_quality"),
    "technical": ("trend", "setup", "key_levels"),
    "sentiment": ("event_flags", "crowd_risk"),
    "macro": ("cycle_tag", "market_bias", "relevance_to_stock"),
}
_ALL_ROLE_FIELDS = (
    "valuation",
    "financial_health",
    "earnings_quality",
    "trend",
    "setup",
    "key_levels",
    "event_flags",
    "crowd_risk",
    "cycle_tag",
    "market_bias",
    "relevance_to_stock",
)


def sanitize_report(report: Report) -> Report:
    """Drop other-role fields and collapse mashed citation ids."""
    keep = set(_ROLE_ONLY_FIELDS[report.role])
    updates = {}
    for name in _ALL_ROLE_FIELDS:
        if name in keep:
            continue
        updates[name] = [] if name == "event_flags" else None
    citations = []
    seen = set()
    for citation in report.citations:
        cid = (citation.id or "").split("/")[0].strip()
        if not cid:
            continue
        cleaned = citation.model_copy(update={"id": cid})
        key = (cleaned.kind, cleaned.id, cleaned.ref_id)
        if key in seen:
            continue
        seen.add(key)
        citations.append(cleaned)
    updates["citations"] = citations
    return report.model_copy(update=updates)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    rating: Literal["buy", "hold", "sell", "abstain"]
    value_score: Optional[int] = Field(default=None, ge=1, le=5)
    timing_score: Optional[int] = Field(default=None, ge=1, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    disagreements: List[str] = Field(default_factory=list)
    falsifiers: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    advice_one_liner: str
    citations: List[EvidenceRef] = Field(default_factory=list)
    dimensions_used: List[str] = Field(default_factory=list)
    dimensions_missing: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_p0_decision(self) -> "Decision":
        if not self.falsifiers:
            raise ValueError("decision requires at least one falsifier")
        if not (2 <= len(self.risks) <= 3):
            raise ValueError("decision requires 2-3 risks")
        return self


class InitialAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    output_schema_version: str = "1"


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    stock_code: str
    thesis_id: str
    mode: Literal["research", "track_day"] = "research"
    status: Literal["running", "success", "failed", "degraded"]
    snapshot_refs: dict = Field(default_factory=dict)
    session_ids: dict = Field(default_factory=dict)
    parent_run_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    schema_version: str = "1"


class AnalysisDeliverable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["completed", "abstained", "degraded"]
    report: Report
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)
    missing_information: List[str] = Field(default_factory=list)
    thesis_impact: Literal[
        "none", "strengthen", "weaken", "invalidate", "uncertain"
    ] = "none"
    reflection: dict = Field(default_factory=dict)
    state_patch: StatePatch


class TrackingUserOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = "本次跟踪更新"
    summary: str
    holding_advice: Literal["unchanged", "review", "invalidate"]
    key_changes: List[str] = Field(default_factory=list)
    next_watch_items: List[str] = Field(default_factory=list)


class AgentSkillCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["fundamental", "technical", "sentiment", "macro"]
    question: str
    required_context_refs: List[str] = Field(default_factory=list)
    reason: str
    status: Literal["success", "failed", "rejected"] = "success"


class NextCheckSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urgency: Literal["low", "medium", "high"] = "low"
    reason: str = ""


class ExpertEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["fundamental", "technical", "sentiment", "macro"]
    reliability: Literal["high", "medium", "low", "unusable"] = "medium"
    verdict: Literal["accept", "discount", "reject", "insufficient"] = "accept"
    gaps: List[str] = Field(default_factory=list)
    notes: str = ""


class TrackingContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str
    thesis_id: str
    as_of: datetime = Field(default_factory=utc_now)
    baseline_run_id: str
    baseline_decision_ref: str = ""
    current_thesis: str = ""
    falsifiers: List[str] = Field(default_factory=list)
    latest_market_delta: Dict[str, Any] = Field(default_factory=dict)
    new_events: List[str] = Field(default_factory=list)
    recent_timeline_refs: List[str] = Field(default_factory=list)
    agent_state_summaries: Dict[str, Any] = Field(default_factory=dict)


class TrackingDeliverable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["unchanged", "review", "invalidate"]
    work_summary: str
    thinking: str = ""
    synthesis: str = ""
    expert_evaluations: List[ExpertEvaluation] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    triggers_hit: List[str] = Field(default_factory=list)
    agent_skill_calls: List[AgentSkillCallRecord] = Field(default_factory=list)
    decision_required: bool = False
    user_output: TrackingUserOutput
    next_check_suggestion: NextCheckSuggestion = Field(
        default_factory=NextCheckSuggestion
    )
