from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TOKEN_SPLIT = re.compile(r"[\s/,;|，。、]+")
MIN_TOKEN_LEN = 2


def tokenize_query(query: str) -> List[str]:
    return [
        token.lower()
        for token in _TOKEN_SPLIT.split((query or "").strip())
        if len(token) >= MIN_TOKEN_LEN
    ]


class Applicability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    any_flags: List[str] = Field(default_factory=list)
    all_flags: List[str] = Field(default_factory=list)
    none_flags: List[str] = Field(default_factory=list)
    required_fields: List[str] = Field(default_factory=list)


class RetrievalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_terms: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)
    intent_tags: List[str] = Field(default_factory=list)
    priority: int = 50
    mandatory: bool = False


class Guidance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = ""
    text: str = ""
    exceptions: List[str] = Field(default_factory=list)


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    chapter: str = ""
    pages: str = ""
    source_sha256: str = ""


class CardTestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flags: List[str] = Field(default_factory=list)
    query: str = ""
    intent_tags: List[str] = Field(default_factory=list)


class CardTests(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_match: List[CardTestCase] = Field(default_factory=list)
    should_not_match: List[CardTestCase] = Field(default_factory=list)


class MethodologyCard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    type: str = "rule"
    scope: List[str]
    status: str = "candidate"
    version: int = 1
    applicability: Applicability
    retrieval: RetrievalSpec
    guidance: Guidance
    source_refs: List[SourceRef] = Field(default_factory=list)
    tests: CardTests = Field(default_factory=CardTests)

    @field_validator("id")
    @classmethod
    def _id_one_token(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or " " in cleaned:
            raise ValueError("candidate id is required and must be one token")
        return cleaned

    @model_validator(mode="after")
    def _must_bind_flags(self) -> "MethodologyCard":
        if not self.applicability.any_flags and not self.applicability.all_flags:
            raise ValueError("applicability.any_flags or all_flags is required")
        if not self.retrieval.canonical_terms:
            raise ValueError("retrieval.canonical_terms is required")
        if not self.guidance.text.strip():
            raise ValueError("candidate text is required")
        return self


class RetrievedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    title: str = ""
    trigger: str = ""
    text: str
    action: str = ""
    score: int = 0
    reasons: List[str] = Field(default_factory=list)


class MethodologyToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval_id: str
    entries: List[RetrievedEntry]


def coerce_source_refs(raw: Any) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, str):
            if item.strip():
                refs.append(SourceRef(source_id=item.strip()).model_dump())
            continue
        if isinstance(item, dict):
            source_id = str(
                item.get("source_id") or item.get("id") or ""
            ).strip()
            if not source_id:
                continue
            refs.append(
                SourceRef(
                    source_id=source_id,
                    chapter=str(item.get("chapter") or ""),
                    pages=str(item.get("pages") or ""),
                    source_sha256=str(item.get("source_sha256") or ""),
                ).model_dump()
            )
    return refs


def migrate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    if isinstance(data.get("applicability"), dict) and isinstance(
        data.get("retrieval"), dict
    ):
        guidance = data.get("guidance")
        if not isinstance(guidance, dict):
            data["guidance"] = {
                "action": str(data.get("action") or ""),
                "text": str(data.get("text") or ""),
                "exceptions": list(data.get("exceptions") or []),
            }
        data["source_refs"] = coerce_source_refs(data.get("source_refs"))
        return data

    trigger = str(data.get("trigger") or "").strip()
    terms = tokenize_query(trigger)
    if trigger and not terms:
        terms = [trigger]
    any_flags = [str(item) for item in (data.get("evidence_required") or [])]
    data["applicability"] = {
        "any_flags": any_flags,
        "all_flags": [],
        "none_flags": [],
        "required_fields": [],
    }
    data["retrieval"] = {
        "canonical_terms": terms,
        "aliases": [],
        "intent_tags": [],
        "priority": int(data.get("priority") or 50),
        "mandatory": bool(data.get("mandatory") or False),
    }
    data["guidance"] = {
        "action": str(data.get("action") or ""),
        "text": str(data.get("text") or data.get("guidance") or ""),
        "exceptions": list(data.get("exceptions") or []),
    }
    data["source_refs"] = coerce_source_refs(data.get("source_refs"))
    data.setdefault("tests", {"should_match": [], "should_not_match": []})
    return data


def card_from_payload(payload: Dict[str, Any]) -> MethodologyCard:
    return MethodologyCard.model_validate(migrate_payload(payload))
