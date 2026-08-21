from __future__ import annotations

from typing import Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field

NONE = "none"
SCHEMA = "1"

DIMS: Tuple[str, ...] = (
    "fundamental",
    "technical",
    "sentiment",
    "macro",
    "decision",
    "tracking",
)

TAG_ALIASES = {
    "#fundamental": "fundamental",
    "#基本面": "fundamental",
    "#technical": "technical",
    "#技术面": "technical",
    "#sentiment": "sentiment",
    "#情绪": "sentiment",
    "#macro": "macro",
    "#宏观": "macro",
    "#decision": "decision",
    "#决策": "decision",
    "#global": "decision",
    "#tracking": "tracking",
    "#追踪": "tracking",
    "#remember": "remember",
    "#记住": "remember",
    "#rerun": "rerun",
    "#重跑": "rerun",
}

Moment = Literal["pre_run", "post_decision", "post_track"]
Effect = Literal["this_run", "remember", "rerun", "remember_rerun"]
PrefStatus = Literal["active", "retired"]
PrefKind = Literal["preference", "constraint", "anti_pattern"]


class UserUtterance(BaseModel):
    """One prompt event. Never stores raw user text."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA, min_length=1)
    utterance_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    moment: Moment
    effect: Effect
    tags: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    stock_code: str = Field(min_length=1)
    thesis_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)


class LlmIntentSlots(BaseModel):
    """Keys the extractor LLM is allowed to emit."""

    model_config = ConfigDict(extra="forbid")

    fundamental: str = Field(min_length=1)
    technical: str = Field(min_length=1)
    sentiment: str = Field(min_length=1)
    macro: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    tracking: str = Field(min_length=1)
    not_evidence: str = Field(min_length=1)


class UserIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA, min_length=1)
    intent_id: str = Field(min_length=1)
    utterance_id: str = Field(min_length=1)
    effect: Effect
    tags: str = Field(min_length=1)
    fundamental: str = Field(min_length=1)
    technical: str = Field(min_length=1)
    sentiment: str = Field(min_length=1)
    macro: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    tracking: str = Field(min_length=1)
    not_evidence: str = Field(min_length=1)
    created_at: str = Field(min_length=1)


class UserPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA, min_length=1)
    preference_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    status: PrefStatus
    current_version: str = Field(min_length=1)
    kind: PrefKind
    scope: str = Field(min_length=1)
    stock_code: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    title: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_utterance_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class TagParse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: str = Field(min_length=1)
    effect: Effect
    body: str = Field(min_length=1)
    tagged_dims: str = Field(min_length=1)
