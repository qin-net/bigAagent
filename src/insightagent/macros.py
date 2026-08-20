from __future__ import annotations

from typing import Any, Dict, Optional

from .data_contracts import MacroSnapshot


RATE_SENSITIVE_TERMS = ("银行", "地产", "房地产", "保险", "证券", "信托")


def apply_macro_rules(
    macro: MacroSnapshot, industry: Optional[str]
) -> Dict[str, Any]:
    flags = []
    if macro.lpr_1y is None:
        flags.append("lpr_missing")

    industry_text = industry or ""
    rate_sensitive = any(term in industry_text for term in RATE_SENSITIVE_TERMS)
    flags.append("rate_sensitive" if rate_sensitive else "low_relevance")

    return {
        "flags": flags,
        "cycle_tag": "rate_data_available"
        if macro.lpr_1y is not None
        else "insufficient",
        "relevance": "high" if rate_sensitive else "low",
    }
