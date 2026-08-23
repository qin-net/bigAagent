from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from .business_contracts import Report
from .data_contracts import EventSnapshot, HolderChangeSnapshot

EVENT_WINDOW_DAYS = 60
NO_RECENT_EVENTS = "no_recent_events"


def apply_event_rules(
    events: EventSnapshot,
    holders: HolderChangeSnapshot,
    *,
    window_days: int = EVENT_WINDOW_DAYS,
) -> Dict[str, Any]:
    as_of = _as_date(events.as_of) or date.today()
    for item in events.events:
        age, in_window = _age_and_window(item.published_at, as_of, window_days)
        item.age_days = age
        item.in_window = in_window
    for item in holders.items:
        age, in_window = _age_and_window(item.published_at, as_of, window_days)
        item.age_days = age
        item.in_window = in_window

    recent_events = [item for item in events.events if item.in_window]
    recent_holders = [item for item in holders.items if item.in_window]
    flags: List[str] = []
    event_types = {e.event_type for e in recent_events if e.event_type}

    if "reduction" in event_types:
        flags.append("has_reduction")
    if "buyback" in event_types:
        flags.append("has_buyback")
    if "inquiry" in event_types:
        flags.append("has_inquiry")
    if "earnings_preview" in event_types or "earnings_flash" in event_types:
        flags.append("has_earnings_preview")

    holder_types = {h.change_type for h in recent_holders if h.change_type}
    if "reduction" in holder_types:
        flags.append("holder_reduction")

    has_any = bool(events.events or holders.items)
    has_recent = bool(recent_events or recent_holders)
    if has_any and not has_recent:
        flags.append(NO_RECENT_EVENTS)
    if not flags:
        flags.append("no_material_event")

    high_risk = {"reduction", "investigation", "lawsuit", "holder_reduction"}
    medium_risk = {"buyback", "inquiry", "earnings_preview", "earnings_flash"}
    if high_risk & set(flags):
        crowd_risk = "high"
    elif medium_risk & set(flags):
        crowd_risk = "medium"
    else:
        crowd_risk = "low"

    return {
        "flags": flags,
        "crowd_risk": crowd_risk,
        "event_count": len(events.events),
        "holder_change_count": len(holders.items),
        "recent_event_count": len(recent_events),
        "recent_holder_count": len(recent_holders),
        "window_days": window_days,
    }


def apply_computed_sentiment_semantics(
    report: Report, flags: List[str]
) -> Report:
    updates: Dict[str, Any] = {}
    missing = list(report.missing_information)
    if NO_RECENT_EVENTS in flags:
        if NO_RECENT_EVENTS not in missing:
            missing.append(NO_RECENT_EVENTS)
        updates.update(
            {
                "abstain": True,
                "stance": "abstain",
                "degraded": False,
                "missing_information": missing,
            }
        )
        if "窗口" not in (report.summary or ""):
            updates["summary"] = (
                "近{}个自然日内无有效事件，情绪面弃权，不形成方向。".format(
                    EVENT_WINDOW_DAYS
                )
            )
    elif "no_material_event" in flags and report.abstain:
        updates["degraded"] = True
        if "events" not in missing:
            missing.append("events")
        updates["missing_information"] = missing
    if not updates:
        return report
    return report.model_copy(update=updates)


def _age_and_window(
    published_at: Optional[str], as_of: date, window_days: int
) -> Tuple[Optional[int], bool]:
    published = _as_date(published_at)
    if published is None:
        return None, False
    age = (as_of - published).days
    return age, 0 <= age <= window_days


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None
