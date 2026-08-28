from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .business_contracts import FundamentalSnapshot, TrackingContext
from .contracts import utc_now
from .data_contracts import (
    EventSnapshot,
    HolderChangeSnapshot,
    IndicatorSnapshot,
    KlineSnapshot,
    MacroSnapshot,
    PriceSnapshot,
)
from .events import apply_event_rules
from .fundamentals import apply_fundamental_rules
from .macros import apply_macro_rules
from .market import synthetic_market_fixture
from .technicals import apply_technical_rules

PRICE_MOVE_THRESHOLD = 0.05
MAX_SKILLS_PER_LOOP = 1

_FUNDAMENTAL_ROUTE = (
    "cashflow_lag",
    "cashflow_quality_issue",
    "value_trap_risk",
    "has_earnings_preview",
)
_SENTIMENT_ROUTE = (
    "has_reduction",
    "holder_reduction",
    "has_inquiry",
    "has_buyback",
)
_TECHNICAL_ROUTE = (
    "volume_spike",
    "rsi_overbought",
    "rsi_oversold",
    "ma_bear_align",
    "ma_bull_align",
)
_MACRO_ROUTE = ("lpr_missing", "lpr_available")


@dataclass
class TrackSnapshots:
    fundamental: FundamentalSnapshot
    technical: Dict[str, Any]
    sentiment: Dict[str, Any]
    macro: Dict[str, Any]


def snapshot_flags(bundle: TrackSnapshots) -> Dict[str, List[str]]:
    fund = apply_fundamental_rules(bundle.fundamental)
    indicator = IndicatorSnapshot(**bundle.technical["indicator"])
    price = PriceSnapshot(**bundle.technical["price"])
    kline = KlineSnapshot(**bundle.technical["kline"])
    tech = apply_technical_rules(indicator, price, kline)
    events = EventSnapshot(**bundle.sentiment["events"])
    holders = HolderChangeSnapshot(**bundle.sentiment["holders"])
    sent = apply_event_rules(events, holders)
    macro = apply_macro_rules(MacroSnapshot(**bundle.macro["macro"]))
    return {
        "fundamental": list(fund.computed_flags),
        "technical": list(tech["flags"]),
        "sentiment": list(sent["flags"]),
        "macro": list(macro["flags"]),
    }


def _flag_delta(
    baseline: Dict[str, List[str]], current: Dict[str, List[str]]
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    added: Dict[str, List[str]] = {}
    removed: Dict[str, List[str]] = {}
    for role in ("fundamental", "technical", "sentiment", "macro"):
        before = set(baseline.get(role) or [])
        after = set(current.get(role) or [])
        added[role] = sorted(after - before)
        removed[role] = sorted(before - after)
    return added, removed


def _price_change_pct(baseline: TrackSnapshots, current: TrackSnapshots) -> Optional[float]:
    old = baseline.fundamental.price
    new = current.fundamental.price
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old


def prescreen(
    *,
    baseline: TrackSnapshots,
    current: TrackSnapshots,
    prior_timeline: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    base_flags = snapshot_flags(baseline)
    current_flags = snapshot_flags(current)
    added, removed = _flag_delta(base_flags, current_flags)
    price_pct = _price_change_pct(baseline, current)
    triggers: List[str] = []
    for role, flags in added.items():
        triggers.extend("flag_added:{}:{}".format(role, item) for item in flags)
    for role, flags in removed.items():
        triggers.extend("flag_removed:{}:{}".format(role, item) for item in flags)
    if price_pct is not None and abs(price_pct) >= PRICE_MOVE_THRESHOLD:
        triggers.append("price_move:{:.4f}".format(price_pct))

    suggested = _route_agent(added, price_pct)
    cooldown_blocked = False
    if suggested and _cooldown_hit(prior_timeline, suggested, triggers):
        cooldown_blocked = True
        suggested = None

    new_events = list(added.get("sentiment") or [])
    return {
        "triggers": triggers,
        "suggested_agent": suggested,
        "suggested_reason": _reason(suggested, triggers),
        "cooldown_blocked": cooldown_blocked,
        "price_change_pct": price_pct,
        "added_flags": added,
        "removed_flags": removed,
        "current_flags": current_flags,
        "new_events": new_events,
    }


def _route_agent(
    added: Dict[str, List[str]], price_pct: Optional[float]
) -> Optional[str]:
    fund = set(added.get("fundamental") or [])
    sent = set(added.get("sentiment") or [])
    tech = set(added.get("technical") or [])
    macro = set(added.get("macro") or [])
    if fund & set(_FUNDAMENTAL_ROUTE) or "has_earnings_preview" in sent:
        return "fundamental"
    if sent & set(_SENTIMENT_ROUTE):
        return "sentiment"
    if tech & set(_TECHNICAL_ROUTE) or (
        price_pct is not None and abs(price_pct) >= PRICE_MOVE_THRESHOLD
    ):
        return "technical"
    if macro & set(_MACRO_ROUTE):
        return "macro"
    return None


def _reason(agent: Optional[str], triggers: Sequence[str]) -> str:
    if not agent:
        if not triggers:
            return "no material delta versus baseline"
        return "delta present but no expert route"
    return "route {} from {}".format(agent, ", ".join(triggers[:6]) or "delta")


def _cooldown_hit(
    prior_timeline: Sequence[Dict[str, Any]],
    agent: str,
    triggers: Sequence[str],
) -> bool:
    if not prior_timeline:
        return False
    latest = prior_timeline[0]
    last_agent = latest.get("suggested_agent")
    last_triggers = set(latest.get("triggers") or latest.get("triggers_hit") or [])
    return last_agent == agent and last_triggers == set(triggers)


def assemble_context(
    *,
    stock_code: str,
    thesis_id: str,
    baseline_run_id: str,
    decision: Dict[str, Any],
    reports: Sequence[Dict[str, Any]],
    prescreen_payload: Dict[str, Any],
    timeline: Sequence[Dict[str, Any]],
) -> TrackingContext:
    summaries = {}
    for report in reports:
        role = str(report.get("role") or "")
        if not role:
            continue
        summaries[role] = {
            "stance": report.get("stance"),
            "score": report.get("score"),
            "summary": str(report.get("summary") or "")[:200],
            "abstain": report.get("abstain"),
        }
    return TrackingContext(
        stock_code=stock_code,
        thesis_id=thesis_id,
        as_of=utc_now(),
        baseline_run_id=baseline_run_id,
        baseline_decision_ref="{}-decision".format(baseline_run_id),
        current_thesis=str(
            decision.get("advice_one_liner") or decision.get("rationale") or ""
        )[:300],
        falsifiers=list(decision.get("falsifiers") or []),
        latest_market_delta={
            "price_change_pct": prescreen_payload.get("price_change_pct"),
            "added_flags": prescreen_payload.get("added_flags") or {},
            "removed_flags": prescreen_payload.get("removed_flags") or {},
        },
        new_events=list(prescreen_payload.get("new_events") or []),
        recent_timeline_refs=[
            str(item.get("timeline_id") or "") for item in timeline[:5]
        ],
        agent_state_summaries=summaries,
    )


async def fetch_track_snapshots(
    stock_code: str,
    *,
    fixture: bool = True,
    fixtures_dir: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> TrackSnapshots:
    from .workflows.initial_research import build_market_adapter_for

    market_payload = payload
    if fixture and market_payload is None:
        market_payload = synthetic_market_fixture(stock_code)
    fundamental = apply_fundamental_rules(
        await build_market_adapter_for(
            fixture=fixture,
            dimension="fundamental",
            fixtures_dir=fixtures_dir,
        ).fetch_fundamental(stock_code)
    )
    technical = await build_market_adapter_for(
        fixture=fixture,
        dimension="technical",
        fixture_payload=market_payload,
    ).fetch_technical(stock_code)
    sentiment = await build_market_adapter_for(
        fixture=fixture,
        dimension="sentiment",
        fixture_payload=market_payload,
    ).fetch_sentiment(stock_code)
    macro = await build_market_adapter_for(
        fixture=fixture,
        dimension="macro",
        fixture_payload=market_payload,
    ).fetch_macro(stock_code)
    return TrackSnapshots(
        fundamental=fundamental,
        technical=technical,
        sentiment=sentiment,
        macro=macro,
    )


def snapshots_from_artifacts(
    fund_payload: Dict[str, Any],
    tech_fields: Dict[str, Any],
    sent_fields: Dict[str, Any],
    macro_fields: Dict[str, Any],
) -> TrackSnapshots:
    return TrackSnapshots(
        fundamental=apply_fundamental_rules(
            FundamentalSnapshot.model_validate(fund_payload)
        ),
        technical=tech_fields,
        sentiment=sent_fields,
        macro=macro_fields,
    )
