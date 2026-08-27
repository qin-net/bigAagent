from __future__ import annotations

import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from pydantic import ValidationError

from .business_contracts import Report
from .persistence import SQLiteDatabase

TEXT_MAX = 200
MAX_HITS = 3
MIN_TOKEN_LEN = 2
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

SEED_ENTRIES: List[Dict[str, Any]] = [
    {
        "id": "kb_roe_quality",
        "title": "长期 ROE 不能看单期",
        "type": "rule",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "roe leverage 盈利能力",
        "action": "看多年年度 ROE 与杠杆，不把单期年化写成合格或不合格",
        "evidence_required": ["roe_quality", "roe_insufficient_history"],
        "exceptions": [],
        "source_refs": [],
        "text": "长期 ROE 看多年年度均值与最低值，单期或中报年化不足以判断合格或不合格。",
        "priority": 100,
        "version": 1,
    },
    {
        "id": "kb_cashflow_lag",
        "title": "利润要用经营现金流验证",
        "type": "rule",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "现金流 盈利质量 净利润",
        "action": "先区分季节性与含金量，不要直接写成崩塌",
        "evidence_required": [
            "cashflow_lag",
            "cashflow_seasonal",
            "cashflow_quality_issue",
        ],
        "exceptions": [],
        "source_refs": [],
        "text": "净利润增长但经营现金流为负时，先区分季节性与含金量，再谈盈利质量，不要直接写成崩塌。",
        "priority": 90,
        "version": 1,
    },
    {
        "id": "kb_leverage",
        "title": "高杠杆放大下行风险",
        "type": "rule",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "负债 杠杆 财务风险",
        "action": "降低安全边际评价",
        "evidence_required": ["high_leverage"],
        "exceptions": [],
        "source_refs": [],
        "text": "资产负债率过高会放大下行风险，需降低安全边际评价。",
        "priority": 80,
        "version": 1,
    },
    {
        "id": "kb_valuation",
        "title": "估值对照自身分位",
        "type": "rule",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "估值 pe 分位 安全边际",
        "action": "不要只看绝对 PE",
        "evidence_required": ["valuation_rich", "valuation_cheap"],
        "exceptions": [],
        "source_refs": [],
        "text": "估值应对照自身历史分位，而不是只看单一 PE。",
        "priority": 80,
        "version": 1,
    },
    {
        "id": "kb_value_trap",
        "title": "便宜不能压过现金流质量",
        "type": "rule",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "value_trap 价值陷阱 便宜 现金流",
        "action": "value_trap_risk 时不得买入",
        "evidence_required": ["value_trap_risk"],
        "exceptions": [],
        "source_refs": [],
        "text": "估值便宜不能压过现金流质量问题；value_trap_risk 时不得给出买入。",
        "priority": 95,
        "version": 1,
    },
    {
        "id": "kb_macro_rates",
        "title": "宏观不是买卖理由",
        "type": "rule",
        "scope": ["macro"],
        "status": "approved",
        "trigger": "lpr 利率 宏观",
        "action": "只提供环境标签",
        "evidence_required": ["lpr_missing", "lpr_available"],
        "exceptions": [],
        "source_refs": [],
        "text": "宏观只提供环境标签，不构成个股买卖理由。",
        "priority": 70,
        "version": 1,
    },
    {
        "id": "kb_event_reduction",
        "title": "减持抬升风险感知",
        "type": "rule",
        "scope": ["sentiment", "event"],
        "status": "approved",
        "trigger": "减持 reduction 控股股东 情绪 事件",
        "action": "写入 event_flags，不能单独作为买卖点",
        "evidence_required": ["has_reduction", "holder_reduction"],
        "exceptions": [],
        "source_refs": [],
        "text": "控股股东减持会抬升风险感知，写入 event_flags；不能单独作为买卖点。",
        "priority": 80,
        "version": 1,
    },
    {
        "id": "kb_event_buyback",
        "title": "回购以公告为准",
        "type": "rule",
        "scope": ["sentiment", "event"],
        "status": "approved",
        "trigger": "回购 buyback 增持 问询 inquiry",
        "action": "新闻不能单独支撑非弃权",
        "evidence_required": ["has_buyback", "has_inquiry"],
        "exceptions": [],
        "source_refs": [],
        "text": "回购或增持可对冲减持压力，仍以公告事件为准；新闻不能单独支撑非弃权。",
        "priority": 80,
        "version": 1,
    },
    {
        "id": "kb_ma_align",
        "title": "均线排列不是买卖点",
        "type": "rule",
        "scope": ["technical"],
        "status": "approved",
        "trigger": "均线 多头 ma_bull_align 排列",
        "action": "只描述趋势结构",
        "evidence_required": ["ma_bull_align", "ma_bear_align"],
        "exceptions": [],
        "source_refs": [],
        "text": "均线多头排列描述趋势结构，不构成买卖点；关键位只能引用均线或K线高低。",
        "priority": 80,
        "version": 1,
    },
    {
        "id": "kb_rsi_overbought",
        "title": "RSI 过线才谈超买超卖",
        "type": "rule",
        "scope": ["technical"],
        "status": "approved",
        "trigger": "rsi 超买 overbought macd 超卖",
        "action": "趋势仍以均线结构为准",
        "evidence_required": ["rsi_overbought", "rsi_oversold"],
        "exceptions": [],
        "source_refs": [],
        "text": "RSI 超买或超卖只说明短线过热或超冷，趋势判断仍以均线结构为准。",
        "priority": 85,
        "version": 1,
    },
]

METHODOLOGY_ENTRIES = SEED_ENTRIES

_TOKEN_SPLIT = re.compile(r"[\s/,;|，。、]+")
_catalog: ContextVar[Optional["MethodologyCatalog"]] = ContextVar(
    "methodology_catalog", default=None
)


def bind_catalog(catalog: "MethodologyCatalog"):
    return _catalog.set(catalog)


def reset_catalog(token) -> None:
    _catalog.reset(token)


def tokenize_query(query: str) -> List[str]:
    return [
        token.lower()
        for token in _TOKEN_SPLIT.split((query or "").strip())
        if len(token) >= MIN_TOKEN_LEN
    ]


def search_entries(
    entries: Sequence[Dict[str, Any]],
    query: str,
    *,
    scope: Optional[str] = None,
    flags: Optional[Sequence[str]] = None,
    statuses: Iterable[str] = ("approved",),
    limit: int = MAX_HITS,
) -> List[Dict[str, str]]:
    tokens = tokenize_query(query)
    if not tokens:
        return []
    allowed_status = set(statuses)
    flag_set = set(flags) if flags is not None else None
    scored: List[tuple] = []
    for entry in entries:
        if entry.get("status") not in allowed_status:
            continue
        scopes = entry.get("scope") or []
        if scope is not None and scope not in scopes:
            continue
        required = [str(item) for item in entry.get("evidence_required") or []]
        if flag_set is not None:
            if not required or not (set(required) & flag_set):
                continue
        haystack = " ".join(
            [str(entry.get("id") or ""), str(entry.get("trigger") or "")]
        ).lower()
        if not any(token in haystack for token in tokens):
            continue
        priority = int(entry.get("priority") or 0)
        scored.append((priority, entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    hits = []
    for _, entry in scored[:limit]:
        hits.append(
            {
                "id": str(entry["id"]),
                "version": str(entry.get("version") or 1),
                "trigger": str(entry.get("trigger") or ""),
                "text": str(entry.get("text") or "")[:TEXT_MAX],
            }
        )
    return hits


def search_methodology(
    query: str,
    *,
    scope: Optional[str] = None,
    flags: Optional[Sequence[str]] = None,
) -> List[Dict[str, str]]:
    catalog = _catalog.get()
    if catalog is not None:
        return catalog.search(query, scope=scope, flags=flags)
    return search_entries(SEED_ENTRIES, query, scope=scope, flags=flags)


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
) -> Dict[str, Any]:
    entries = search_methodology(query, scope=scope, flags=flags)
    for item in entries:
        retrieved_ids.add(item["id"])
    return {"entries": entries}


def allowed_flags_for(scope: str) -> List[str]:
    return sorted(ALLOWED_FLAGS.get(scope, frozenset()))


def validate_candidate_payload(
    payload: Dict[str, Any], *, scope: Optional[str] = None
) -> Dict[str, Any]:
    entry_id = str(payload.get("id") or "").strip()
    if not entry_id or " " in entry_id:
        raise ValueError("candidate id is required and must be one token")
    scopes = payload.get("scope") or []
    if isinstance(scopes, str):
        scopes = [item.strip() for item in scopes.split(",") if item.strip()]
    if not scopes:
        if scope:
            scopes = [scope]
        else:
            raise ValueError("candidate scope is required")
    if scope and scope not in scopes:
        raise ValueError("candidate scope must include {}".format(scope))
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("candidate text is required")
    if len(text) > TEXT_MAX:
        text = text[:TEXT_MAX]
    required = [str(item) for item in payload.get("evidence_required") or []]
    if not required:
        raise ValueError("evidence_required is required")
    allowed: Set[str] = set()
    for item in scopes:
        allowed |= set(ALLOWED_FLAGS.get(item, frozenset()))
    unknown = [item for item in required if item not in allowed]
    if unknown:
        raise ValueError(
            "evidence_required not in frozen flags: {}".format(
                ", ".join(unknown)
            )
        )
    trigger = str(payload.get("trigger") or "").strip()
    if not trigger:
        raise ValueError("trigger is required")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": entry_id,
        "title": str(payload.get("title") or entry_id),
        "type": str(payload.get("type") or "rule"),
        "scope": list(scopes),
        "status": "candidate",
        "trigger": trigger,
        "action": str(payload.get("action") or ""),
        "evidence_required": required,
        "exceptions": list(payload.get("exceptions") or []),
        "source_refs": list(payload.get("source_refs") or []),
        "text": text,
        "priority": int(payload.get("priority") or 50),
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "change_note": str(payload.get("change_note") or "distill"),
    }


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
        statuses: Iterable[str] = ("approved",),
    ) -> List[Dict[str, str]]:
        return search_entries(
            self.list_payloads(statuses=statuses),
            query,
            scope=scope,
            flags=flags,
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
