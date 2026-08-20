from __future__ import annotations

from typing import Any, Dict, List, Optional

from .data_contracts import EventSnapshot, HolderChangeSnapshot


def apply_event_rules(
    events: EventSnapshot,
    holders: HolderChangeSnapshot,
) -> Dict[str, Any]:
    flags: List[str] = []
    event_types = {e.event_type for e in events.events if e.event_type}

    if "reduction" in event_types:
        flags.append("has_reduction")
    if "buyback" in event_types:
        flags.append("has_buyback")
    if "inquiry" in event_types:
        flags.append("has_inquiry")
    if "earnings_preview" in event_types or "earnings_flash" in event_types:
        flags.append("has_earnings_preview")

    holder_types = {h.change_type for h in holders.items if h.change_type}
    if "reduction" in holder_types:
        flags.append("holder_reduction")

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
    }
