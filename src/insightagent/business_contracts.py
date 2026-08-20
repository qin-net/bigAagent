from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

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
    revenue_yoy: Optional[float] = None
    profit_yoy: Optional[float] = None
    gross_margin: Optional[float] = None
    debt_ratio: Optional[float] = None
    current_ratio: Optional[float] = None
    operating_cf: Optional[float] = None
    net_profit: Optional[float] = None
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
    valuation: Optional[str] = None
    financial_health: Optional[str] = None
    earnings_quality: Optional[str] = None
    trend: Optional[str] = None
    setup: Optional[str] = None
    key_levels: Optional[str] = None
    event_flags: List[str] = Field(default_factory=list)
    crowd_risk: Optional[Literal["low", "medium", "high"]] = None

    @model_validator(mode="after")
    def validate_abstain_and_citations(self) -> "Report":
        if self.abstain and self.stance != "abstain":
            raise ValueError("abstain=true requires stance=abstain")
        if not self.abstain and not self.citations:
            raise ValueError("non-abstain report requires citations")
        return self


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
    mode: Literal["research"] = "research"
    status: Literal["running", "success", "failed", "degraded"]
    snapshot_refs: dict = Field(default_factory=dict)
    session_ids: dict = Field(default_factory=dict)
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
