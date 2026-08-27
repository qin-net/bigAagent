from __future__ import annotations

import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from uuid import uuid4

from pydantic import ValidationError

from .business_contracts import Report
from .kb_contract import (
    MethodologyCard,
    MethodologyToolOutput,
    RetrievedEntry,
    card_from_payload,
    migrate_payload,
    tokenize_query,
)
from .persistence import SQLiteDatabase

TEXT_MAX = 200
MAX_HITS = 3
MAX_MARKDOWN_CHARS = 8000

ALLOWED_FLAGS: Dict[str, frozenset] = {
    "fundamental": frozenset(
        {
            "roe_quality",
            "roe_insufficient_history",
            "cashflow_lag",
            "cashflow_seasonal",
            "cashflow_quality_issue",
            "high_leverage",
            "valuation_rich",
            "valuation_cheap",
            "value_trap_risk",
        }
    ),
    "technical": frozenset(
        {
            "insufficient_bars",
            "ma_bull_align",
            "ma_bear_align",
            "macd_pos",
            "macd_neg",
            "rsi_overbought",
            "rsi_oversold",
            "volume_spike",
        }
    ),
    "sentiment": frozenset(
        {
            "has_reduction",
            "has_buyback",
            "has_inquiry",
            "has_earnings_preview",
            "holder_reduction",
            "no_recent_events",
            "no_material_event",
        }
    ),
    "macro": frozenset({"lpr_missing", "lpr_available"}),
}


def _card(
    *,
    entry_id: str,
    title: str,
    scope: List[str],
    any_flags: List[str],
    canonical_terms: List[str],
    action: str,
    text: str,
    priority: int,
    aliases: Optional[List[str]] = None,
    intent_tags: Optional[List[str]] = None,
    mandatory: bool = False,
    all_flags: Optional[List[str]] = None,
    none_flags: Optional[List[str]] = None,
    required_fields: Optional[List[str]] = None,
    exceptions: Optional[List[str]] = None,
    tests: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "id": entry_id,
        "title": title,
        "type": "rule",
        "scope": scope,
        "status": "approved",
        "version": 1,
        "applicability": {
            "any_flags": any_flags,
            "all_flags": all_flags or [],
            "none_flags": none_flags or [],
            "required_fields": required_fields or [],
        },
        "retrieval": {
            "canonical_terms": canonical_terms,
            "aliases": aliases or [],
            "intent_tags": intent_tags or [],
            "priority": priority,
            "mandatory": mandatory,
        },
        "guidance": {
            "action": action,
            "text": text,
            "exceptions": exceptions or [],
        },
        "source_refs": [],
        "tests": tests or {"should_match": [], "should_not_match": []},
    }
    return MethodologyCard.model_validate(payload).model_dump(mode="json")


SEED_ENTRIES: List[Dict[str, Any]] = [
    _card(
        entry_id="kb_roe_quality",
        title="长期 ROE 不能看单期",
        scope=["fundamental"],
        any_flags=["roe_quality", "roe_insufficient_history"],
        canonical_terms=["roe", "leverage", "盈利能力"],
        action="看多年年度 ROE 与杠杆，不把单期年化写成合格或不合格",
        text="长期 ROE 看多年年度均值与最低值，单期或中报年化不足以判断合格或不合格。",
        priority=100,
        intent_tags=["quality_check"],
    ),
    _card(
        entry_id="kb_cashflow_lag",
        title="利润需要经营现金流验证",
        scope=["fundamental"],
        any_flags=["cashflow_lag", "cashflow_quality_issue"],
        canonical_terms=["现金流", "净利润", "盈利质量"],
        aliases=["经营性现金流", "利润含金量", "现金利润"],
        intent_tags=["quality_check", "falsifier"],
        mandatory=True,
        priority=90,
        required_fields=["operating_cf", "net_profit"],
        action="区分季节性和盈利含金量",
        text="净利润增长但经营现金流为负时，应先判断季节性和现金流质量。",
        exceptions=["明确存在可验证的行业季节性"],
        tests={
            "should_match": [
                {"flags": ["cashflow_lag"], "query": "利润含金量"},
            ],
            "should_not_match": [
                {"flags": ["ma_bull_align"], "query": "现金流"},
            ],
        },
    ),
    _card(
        entry_id="kb_leverage",
        title="高杠杆放大下行风险",
        scope=["fundamental"],
        any_flags=["high_leverage"],
        canonical_terms=["负债", "杠杆", "财务风险"],
        action="降低安全边际评价",
        text="资产负债率过高会放大下行风险，需降低安全边际评价。",
        priority=80,
    ),
    _card(
        entry_id="kb_valuation",
        title="估值对照自身分位",
        scope=["fundamental"],
        any_flags=["valuation_rich", "valuation_cheap"],
        canonical_terms=["估值", "pe", "分位", "安全边际"],
        action="不要只看绝对 PE",
        text="估值应对照自身历史分位，而不是只看单一 PE。",
        priority=80,
    ),
    _card(
        entry_id="kb_value_trap",
        title="便宜不能压过现金流质量",
        scope=["fundamental"],
        any_flags=["value_trap_risk"],
        canonical_terms=["value_trap", "价值陷阱", "便宜", "现金流"],
        action="value_trap_risk 时不得买入",
        text="估值便宜不能压过现金流质量问题；value_trap_risk 时不得给出买入。",
        priority=95,
        mandatory=True,
        intent_tags=["falsifier"],
    ),
    _card(
        entry_id="kb_macro_rates",
        title="宏观不是买卖理由",
        scope=["macro"],
        any_flags=["lpr_missing", "lpr_available"],
        canonical_terms=["lpr", "利率", "宏观"],
        action="只提供环境标签",
        text="宏观只提供环境标签，不构成个股买卖理由。",
        priority=70,
    ),
    _card(
        entry_id="kb_event_reduction",
        title="减持抬升风险感知",
        scope=["sentiment", "event"],
        any_flags=["has_reduction", "holder_reduction"],
        canonical_terms=["减持", "reduction", "控股股东", "情绪", "事件"],
        action="写入 event_flags，不能单独作为买卖点",
        text="控股股东减持会抬升风险感知，写入 event_flags；不能单独作为买卖点。",
        priority=80,
    ),
    _card(
        entry_id="kb_event_buyback",
        title="回购以公告为准",
        scope=["sentiment", "event"],
        any_flags=["has_buyback", "has_inquiry"],
        canonical_terms=["回购", "buyback", "增持", "问询", "inquiry"],
        action="新闻不能单独支撑非弃权",
        text="回购或增持可对冲减持压力，仍以公告事件为准；新闻不能单独支撑非弃权。",
        priority=80,
    ),
    _card(
        entry_id="kb_ma_align",
        title="均线排列不是买卖点",
        scope=["technical"],
        any_flags=["ma_bull_align", "ma_bear_align"],
        canonical_terms=["均线", "多头", "ma_bull_align", "排列"],
        action="只描述趋势结构",
        text="均线多头排列描述趋势结构，不构成买卖点；关键位只能引用均线或K线高低。",
        priority=80,
    ),
    _card(
        entry_id="kb_rsi_overbought",
        title="RSI 过线才谈超买超卖",
        scope=["technical"],
        any_flags=["rsi_overbought", "rsi_oversold"],
        canonical_terms=["rsi", "超买", "overbought", "macd", "超卖"],
        action="趋势仍以均线结构为准",
        text="RSI 超买或超卖只说明短线过热或超冷，趋势判断仍以均线结构为准。",
        priority=85,
    ),
]

METHODOLOGY_ENTRIES = SEED_ENTRIES

_catalog: ContextVar[Optional["MethodologyCatalog"]] = ContextVar(
    "methodology_catalog", default=None
)


def bind_catalog(catalog: "MethodologyCatalog"):
    return _catalog.set(catalog)


def reset_catalog(token) -> None:
    _catalog.reset(token)


def _flag_set(flags: Optional[Sequence[str]]) -> Set[str]:
    return {str(item) for item in (flags or []) if item}


def _applicable(
    card: MethodologyCard,
    flags: Set[str],
    present_fields: Optional[Set[str]],
) -> bool:
    required_any = set(card.applicability.any_flags)
    required_all = set(card.applicability.all_flags)
    forbidden = set(card.applicability.none_flags)
    if required_all and not required_all.issubset(flags):
        return False
    if required_any and not (required_any & flags):
        return False
    if forbidden & flags:
        return False
    needed = set(card.applicability.required_fields)
    if present_fields is not None and needed and not needed.issubset(present_fields):
        return False
    return True


def _lexical_hits(terms: Sequence[str], query: str) -> List[str]:
    haystack = (query or "").strip().lower()
    if not haystack:
        return []
    tokens = tokenize_query(query)
    matched: List[str] = []
    for term in terms:
        needle = str(term).strip().lower()
        if not needle:
            continue
        if needle in haystack or any(
            token in needle or needle in token for token in tokens
        ):
            matched.append(str(term))
    return matched


def _score_card(
    card: MethodologyCard,
    flags: Set[str],
    query: str,
    intent_tags: Optional[Sequence[str]],
) -> tuple:
    reasons: List[str] = []
    score = 0
    if card.retrieval.mandatory:
        score += 1000
        reasons.append("mandatory")
    matched_flags = sorted(
        flags
        & (set(card.applicability.any_flags) | set(card.applicability.all_flags))
    )
    score += len(matched_flags) * 100
    reasons.extend("flag:{}".format(item) for item in matched_flags)
    canonical = _lexical_hits(card.retrieval.canonical_terms, query)
    score += len(canonical) * 20
    reasons.extend("canonical:{}".format(item) for item in canonical)
    aliases = _lexical_hits(card.retrieval.aliases, query)
    score += len(aliases) * 10
    reasons.extend("alias:{}".format(item) for item in aliases)
    wanted = {str(item) for item in (intent_tags or []) if item}
    intents = sorted(wanted & set(card.retrieval.intent_tags))
    score += len(intents) * 10
    reasons.extend("intent:{}".format(item) for item in intents)
    score += int(card.retrieval.priority)
    reasons.append("priority:{}".format(card.retrieval.priority))
    reasons.append("version:{}".format(card.version))
    return score, reasons


def _hit_payload(card: MethodologyCard, score: int, reasons: List[str]) -> Dict[str, Any]:
    trigger = " ".join(card.retrieval.canonical_terms)
    return RetrievedEntry(
        id=card.id,
        version=str(card.version),
        title=card.title,
        trigger=trigger,
        text=card.guidance.text[:TEXT_MAX],
        action=card.guidance.action,
        score=score,
        reasons=reasons,
    ).model_dump(mode="json")


def search_entries(
    entries: Sequence[Dict[str, Any]],
    query: str,
    *,
    scope: Optional[str] = None,
    flags: Optional[Sequence[str]] = None,
    intent_tags: Optional[Sequence[str]] = None,
    present_fields: Optional[Sequence[str]] = None,
    statuses: Iterable[str] = ("approved",),
    limit: int = MAX_HITS,
) -> List[Dict[str, Any]]:
    tokens = tokenize_query(query)
    flag_set = None if flags is None else _flag_set(flags)
    if flag_set is None and not tokens:
        return []
    allowed_status = set(statuses)
    fields = None if present_fields is None else {str(item) for item in present_fields}
    scored: List[tuple] = []
    for raw in entries:
        if raw.get("status") not in allowed_status:
            continue
        try:
            card = card_from_payload(raw)
        except ValidationError:
            continue
        if scope is not None and scope not in card.scope:
            continue
        if flag_set is None:
            haystack = " ".join(
                [card.id]
                + card.retrieval.canonical_terms
                + card.retrieval.aliases
            ).lower()
            if not any(token in haystack for token in tokens):
                continue
            score, reasons = _score_card(card, set(), query, intent_tags)
        else:
            if not _applicable(card, flag_set, fields):
                continue
            score, reasons = _score_card(card, flag_set, query, intent_tags)
        scored.append((score, card.id, card, reasons))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        _hit_payload(card, score, reasons)
        for score, _, card, reasons in scored[:limit]
    ]


def search_methodology(
    query: str = "",
    *,
    scope: Optional[str] = None,
    flags: Optional[Sequence[str]] = None,
    intent_tags: Optional[Sequence[str]] = None,
    present_fields: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    catalog = _catalog.get()
    if catalog is not None:
        return catalog.search(
            query,
            scope=scope,
            flags=flags,
            intent_tags=intent_tags,
            present_fields=present_fields,
        )
    return search_entries(
        SEED_ENTRIES,
        query,
        scope=scope,
        flags=flags,
        intent_tags=intent_tags,
        present_fields=present_fields,
    )


def drop_unretrieved_kb(report: Report, retrieved_ids: Set[str]) -> Report:
    kept = [
        citation
        for citation in report.citations
        if citation.kind != "kb" or citation.id in retrieved_ids
    ]
    if not kept and not report.abstain:
        kept = [
            citation
            for citation in report.citations
            if citation.kind != "kb"
        ]
        if not kept:
            return report
    try:
        return report.model_copy(update={"citations": kept})
    except ValidationError:
        return report


def record_search(
    retrieved_ids: Set[str],
    query: str,
    *,
    scope: str,
    flags: Optional[Sequence[str]] = None,
    intent_tags: Optional[Sequence[str]] = None,
    present_fields: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    entries = search_methodology(
        query,
        scope=scope,
        flags=flags,
        intent_tags=intent_tags,
        present_fields=present_fields,
    )
    for item in entries:
        retrieved_ids.add(item["id"])
    return MethodologyToolOutput(
        retrieval_id="kb-search-{}".format(uuid4().hex[:8]),
        entries=[RetrievedEntry.model_validate(item) for item in entries],
    ).model_dump(mode="json")


def allowed_flags_for(scope: str) -> List[str]:
    return sorted(ALLOWED_FLAGS.get(scope, frozenset()))


def _collect_flags(card: MethodologyCard) -> List[str]:
    return (
        list(card.applicability.any_flags)
        + list(card.applicability.all_flags)
        + list(card.applicability.none_flags)
    )


def validate_candidate_payload(
    payload: Dict[str, Any], *, scope: Optional[str] = None
) -> Dict[str, Any]:
    migrated = migrate_payload(payload)
    scopes = migrated.get("scope") or []
    if isinstance(scopes, str):
        scopes = [item.strip() for item in scopes.split(",") if item.strip()]
    if not scopes:
        if scope:
            scopes = [scope]
        else:
            raise ValueError("candidate scope is required")
    if scope and scope not in scopes:
        raise ValueError("candidate scope must include {}".format(scope))
    migrated["scope"] = list(scopes)
    migrated.setdefault("title", migrated.get("id") or "")
    text = str((migrated.get("guidance") or {}).get("text") or "").strip()
    if text and len(text) > TEXT_MAX:
        migrated.setdefault("guidance", {})
        migrated["guidance"]["text"] = text[:TEXT_MAX]
    try:
        card = MethodologyCard.model_validate(migrated)
    except ValidationError as error:
        raise ValueError(str(error)) from error
    allowed: Set[str] = set()
    for item in card.scope:
        allowed |= set(ALLOWED_FLAGS.get(item, frozenset()))
    unknown = [item for item in _collect_flags(card) if item not in allowed]
    if unknown:
        raise ValueError(
            "evidence_required not in frozen flags: {}".format(
                ", ".join(unknown)
            )
        )
    now = datetime.now(timezone.utc).isoformat()
    dumped = card.model_dump(mode="json")
    dumped["status"] = "candidate"
    dumped["created_at"] = now
    dumped["updated_at"] = now
    dumped["change_note"] = str(payload.get("change_note") or "distill")
    return dumped


def parse_entry_markdown(text: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        rest = text[3:]
        end = rest.find("\n---")
        if end >= 0:
            header = rest[:end].strip()
            body = rest[end + 4 :].strip()
            for line in header.splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                payload[key.strip()] = _parse_scalar(raw.strip())
    if body and "text" not in payload:
        payload["text"] = body
    return payload


def _parse_scalar(raw: str) -> Any:
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",")]
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw.strip("'\"")


def resolve_markdown_path(path: str, roots: Sequence[Path]) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.suffix.lower() != ".md" or not resolved.is_file():
        raise ValueError("markdown file not found")
    for root in roots:
        base = root.expanduser().resolve()
        if resolved == base or base in resolved.parents:
            return resolved
    raise ValueError("markdown path is outside the whitelist")


def read_whitelisted_markdown(path: str, roots: Sequence[Path]) -> str:
    target = resolve_markdown_path(path, roots)
    text = target.read_text(encoding="utf-8")
    if len(text) > MAX_MARKDOWN_CHARS:
        text = text[:MAX_MARKDOWN_CHARS]
    return text


@dataclass
class MethodologyCatalog:
    database: SQLiteDatabase

    def ensure_seeded(self) -> None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM methodology_entries"
            ).fetchone()
            if row and int(row["n"]) > 0:
                return
            for entry in SEED_ENTRIES:
                self._upsert_sync(connection, dict(entry), status="approved")
        finally:
            connection.close()

    def search(
        self,
        query: str,
        *,
        scope: Optional[str] = None,
        flags: Optional[Sequence[str]] = None,
        intent_tags: Optional[Sequence[str]] = None,
        present_fields: Optional[Sequence[str]] = None,
        statuses: Iterable[str] = ("approved",),
    ) -> List[Dict[str, Any]]:
        return search_entries(
            self.list_payloads(statuses=statuses),
            query,
            scope=scope,
            flags=flags,
            intent_tags=intent_tags,
            present_fields=present_fields,
            statuses=statuses,
        )

    def list_payloads(
        self, *, statuses: Optional[Iterable[str]] = None
    ) -> List[Dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT e.entry_id, e.status, e.current_version, "
                "e.title, e.scope_json, v.payload_json "
                "FROM methodology_entries e "
                "JOIN methodology_versions v "
                "ON v.entry_id = e.entry_id AND v.version = e.current_version"
            ).fetchall()
        finally:
            connection.close()
        allowed = set(statuses) if statuses is not None else None
        entries = []
        for row in rows:
            if allowed is not None and row["status"] not in allowed:
                continue
            payload = json.loads(row["payload_json"])
            payload["status"] = row["status"]
            payload["version"] = row["current_version"]
            payload["title"] = row["title"]
            payload["scope"] = json.loads(row["scope_json"])
            payload["id"] = row["entry_id"]
            entries.append(payload)
        return entries

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        for item in self.list_payloads():
            if item["id"] == entry_id:
                return item
        return None

    def import_markdown(self, path: Path) -> Dict[str, Any]:
        payload = parse_entry_markdown(
            Path(path).read_text(encoding="utf-8")
        )
        cleaned = validate_candidate_payload(payload)
        return self.submit_candidate(cleaned)

    def submit_candidate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = validate_candidate_payload(payload)
        connection = self.database.connect()
        try:
            existing = connection.execute(
                "SELECT status, current_version FROM methodology_entries "
                "WHERE entry_id = ?",
                (cleaned["id"],),
            ).fetchone()
            if existing and existing["status"] == "approved":
                raise ValueError(
                    "cannot overwrite approved entry {}".format(cleaned["id"])
                )
            stored = self._upsert_sync(
                connection, cleaned, status="candidate"
            )
        finally:
            connection.close()
        return stored

    def approve(self, entry_id: str) -> Dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT status FROM methodology_entries WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if not row:
                raise ValueError("unknown entry {}".format(entry_id))
            connection.execute(
                "UPDATE methodology_entries SET status = ?, updated_at = ? "
                "WHERE entry_id = ?",
                (
                    "approved",
                    datetime.now(timezone.utc).isoformat(),
                    entry_id,
                ),
            )
        finally:
            connection.close()
        item = self.get(entry_id)
        assert item is not None
        return item

    def _upsert_sync(
        self,
        connection,
        payload: Dict[str, Any],
        *,
        status: str,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        entry_id = payload["id"]
        existing = connection.execute(
            "SELECT current_version FROM methodology_entries WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        version = 1 if not existing else int(existing["current_version"]) + 1
        payload = dict(payload)
        payload["status"] = status
        payload["version"] = version
        payload["updated_at"] = now
        body = json.dumps(payload, ensure_ascii=False)
        if existing:
            connection.execute(
                "UPDATE methodology_entries SET status = ?, current_version = ?, "
                "title = ?, scope_json = ?, updated_at = ? WHERE entry_id = ?",
                (
                    status,
                    version,
                    payload["title"],
                    json.dumps(payload["scope"], ensure_ascii=False),
                    now,
                    entry_id,
                ),
            )
        else:
            connection.execute(
                "INSERT INTO methodology_entries("
                "entry_id, status, current_version, title, scope_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    status,
                    version,
                    payload["title"],
                    json.dumps(payload["scope"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
        connection.execute(
            "INSERT INTO methodology_versions("
            "entry_id, version, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (entry_id, version, body, now),
        )
        return payload
