from __future__ import annotations

from typing import Any, Dict

from .business_contracts import Report
from .data_contracts import MacroSnapshot


def apply_macro_rules(macro: MacroSnapshot) -> Dict[str, Any]:
    flags = []
    if macro.lpr_1y is None:
        flags.append("lpr_missing")
    else:
        flags.append("lpr_available")
    return {
        "flags": flags,
        "cycle_tag": "rate_data_available"
        if macro.lpr_1y is not None
        else "insufficient",
    }


def apply_computed_macro_semantics(
    report: Report,
    *,
    macro: MacroSnapshot,
) -> Report:
    """Normalize report against LPR availability and the model's relevance call.

    Relevance is the expert's judgment, not an industry keyword list.
    """
    updates: Dict[str, Any] = {}
    if macro.lpr_1y is None:
        updates["abstain"] = True
        updates["stance"] = "abstain"
        updates["degraded"] = True
        if report.relevance_to_stock is None:
            updates["relevance_to_stock"] = "unknown"
        if report.cycle_tag is None:
            updates["cycle_tag"] = "insufficient"
        return report.model_copy(update=updates)

    relevance = report.relevance_to_stock
    if relevance is None or relevance == "unknown":
        updates["relevance_to_stock"] = "unknown"
        updates["abstain"] = True
        updates["stance"] = "abstain"
        updates["degraded"] = True
    elif relevance == "low":
        updates["abstain"] = True
        updates["stance"] = "abstain"
        updates["degraded"] = False
    else:
        updates["abstain"] = False
        if report.stance == "abstain":
            updates["stance"] = "hold"
        updates["degraded"] = False
    if report.cycle_tag is None:
        updates["cycle_tag"] = "rate_data_available"
    return report.model_copy(update=updates)
