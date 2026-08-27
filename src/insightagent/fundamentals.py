from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from .business_contracts import FundamentalSnapshot, RuleHit
from .contracts import utc_now
from .market import AkshareMarketClient, FixtureMarketClient, MarketService
from .methodology import (  # noqa: F401
    METHODOLOGY_ENTRIES,
    search_methodology,
)

REQUIRED_FIELDS = (
    "pe",
    "pb",
    "roe",
    "debt_ratio",
    "operating_cf",
    "net_profit",
    "profit_yoy",
)


def annualize_roe(roe: Optional[float], period: Optional[str]) -> Optional[float]:
    if roe is None or not period:
        return None
    factors = {"q1": 4.0, "h1": 2.0, "q3": 4.0 / 3.0, "fy": 1.0}
    factor = factors.get(period)
    if factor is None:
        return None
    return roe * factor


def apply_fundamental_rules(
    snapshot: FundamentalSnapshot,
) -> FundamentalSnapshot:
    hits: List[RuleHit] = []
    flags: List[str] = []
    debt_ok = snapshot.debt_ratio is not None and snapshot.debt_ratio < 60
    series = [value for value in snapshot.roe_series if value is not None]
    annualized = annualize_roe(snapshot.roe, snapshot.roe_report_period)

    if len(series) >= 3:
        mean_roe = sum(series) / float(len(series))
        min_roe = min(series)
        roe_hit = mean_roe >= 15 and min_roe >= 12 and debt_ok
        detail = (
            "multi-year annual ROE mean={} min={} debt_ratio={}".format(
                mean_roe, min_roe, snapshot.debt_ratio
            )
        )
    elif snapshot.roe_stable is True:
        roe_hit = snapshot.roe is not None and snapshot.roe >= 12 and debt_ok
        detail = "roe_stable and ROE>=12 and debt_ratio<60"
    else:
        roe_hit = False
        flags.append("roe_insufficient_history")
        parts = [
            "single-period is NOT enough to judge long-term ROE quality"
        ]
        if annualized is not None:
            parts.append(
                "annualized_roe={} period={}".format(
                    annualized, snapshot.roe_report_period
                )
            )
        detail = "; ".join(parts)
    hits.append(
        RuleHit(rule_id="roe_quality", hit=bool(roe_hit), detail=detail)
    )
    if roe_hit:
        flags.append("roe_quality")

    cash_hit = (
        snapshot.profit_yoy is not None
        and snapshot.profit_yoy > 0
        and snapshot.operating_cf is not None
        and snapshot.operating_cf <= 0
    )
    lag_detail = "profit_yoy>0 and operating_cf<=0"
    if cash_hit:
        flags.append("cashflow_lag")
        if snapshot.cashflow_yoy is not None and snapshot.cashflow_yoy > 0:
            flags.append("cashflow_seasonal")
        if snapshot.ocf_to_np is not None and snapshot.ocf_to_np < 0.5:
            flags.append("cashflow_quality_issue")
        if snapshot.cashflow_yoy is None and snapshot.ocf_to_np is None:
            lag_detail += "; undetermined"
    hits.append(
        RuleHit(rule_id="cashflow_lag", hit=bool(cash_hit), detail=lag_detail)
    )

    lev_hit = snapshot.debt_ratio is not None and snapshot.debt_ratio >= 70
    hits.append(
        RuleHit(
            rule_id="high_leverage",
            hit=bool(lev_hit),
            detail="debt_ratio>=70",
        )
    )
    if lev_hit:
        flags.append("high_leverage")

    rich_hit = (
        snapshot.pe_percentile_5y is not None
        and snapshot.pe_percentile_5y >= 80
    )
    hits.append(
        RuleHit(
            rule_id="valuation_rich",
            hit=bool(rich_hit),
            detail="pe_percentile_5y>=80",
        )
    )
    if rich_hit:
        flags.append("valuation_rich")

    cheap_hit = (
        snapshot.pe is not None
        and snapshot.pe_percentile_5y is not None
        and snapshot.pe_percentile_5y <= 30
    )
    hits.append(
        RuleHit(
            rule_id="valuation_cheap",
            hit=bool(cheap_hit),
            detail="pe_percentile_5y<=30 and pe present",
        )
    )
    if cheap_hit:
        flags.append("valuation_cheap")

    trap_hit = (
        cheap_hit
        and cash_hit
        and "cashflow_seasonal" not in flags
    )
    hits.append(
        RuleHit(
            rule_id="value_trap_risk",
            hit=bool(trap_hit),
            detail="valuation_cheap and cashflow_lag without seasonal offset",
        )
    )
    if trap_hit:
        flags.append("value_trap_risk")

    missing = [
        name
        for name in REQUIRED_FIELDS
        if getattr(snapshot, name) is None
    ]
    snapshot.missing_fields = missing
    snapshot.rule_hits = hits
    snapshot.computed_flags = flags
    return snapshot


class MarketDataAdapter(Protocol):
    async def fetch_fundamental(self, stock_code: str) -> FundamentalSnapshot:
        ...


class FixtureFundamentalAdapter:
    def __init__(self, fixtures: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.fixtures = fixtures or {}

    @classmethod
    def from_directory(cls, directory: Path) -> "FixtureFundamentalAdapter":
        fixtures: Dict[str, Dict[str, Any]] = {}
        if directory.exists():
            for path in directory.glob("*.json"):
                payload = json.loads(path.read_text(encoding="utf-8"))
                fixtures[str(payload["stock_code"])] = payload
        return cls(fixtures)

    async def fetch_fundamental(self, stock_code: str) -> FundamentalSnapshot:
        payload = self.fixtures.get(stock_code)
        if payload is None:
            snapshot = FundamentalSnapshot(
                stock_code=stock_code,
                as_of=utc_now(),
                missing_fields=list(REQUIRED_FIELDS),
            )
            return apply_fundamental_rules(snapshot)
        snapshot = FundamentalSnapshot.model_validate(payload)
        return apply_fundamental_rules(snapshot)


class AkshareFundamentalAdapter:
    async def fetch_fundamental(self, stock_code: str) -> FundamentalSnapshot:
        from .market import AkshareMarketClient, MarketService

        service = MarketService(AkshareMarketClient())
        payload = await service.compose_fundamental_fields(stock_code)
        return apply_fundamental_rules(FundamentalSnapshot.model_validate(payload))


def _lg_indicator(ak: Any, stock_code: str) -> Any:
    for name in ("stock_a_indicator_lg", "stock_a_lg_indicator"):
        func = getattr(ak, name, None)
        if func is not None:
            return func(symbol=stock_code)
    return None


async def _call_akshare(func):
    import asyncio

    return await asyncio.to_thread(func)


def _map_akshare(stock_code: str, info: Any, indicators: Any) -> Dict[str, Any]:
    company_name = None
    if info is not None and hasattr(info, "set_index"):
        try:
            table = info.set_index("item")["value"]
            company_name = str(table.get("股票简称") or table.get("名称") or "")
        except Exception:
            company_name = None

    latest: Dict[str, Any] = {}
    if indicators is not None and hasattr(indicators, "iloc") and len(indicators):
        row = indicators.iloc[-1]
        latest = {str(key): row[key] for key in indicators.columns}

    def _num(*names: str) -> Optional[float]:
        for name in names:
            value = latest.get(name)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number == number:  # not NaN
                return number
        return None

    return {
        "stock_code": stock_code,
        "company_name": company_name or None,
        "price": _num("close", "收盘价"),
        "pe": _num("pe", "pe_ttm", "市盈率"),
        "pb": _num("pb", "市净率"),
        "roe": _num("roe", "净资产收益率"),
        "revenue_yoy": _num("revenue_yoy", "营业总收入同比"),
        "profit_yoy": _num("netprofit_yoy", "净利润同比"),
        "gross_margin": _num("grossprofit_margin", "毛利率"),
        "debt_ratio": _num("debt_asset_ratio", "资产负债率"),
        "current_ratio": _num("current_ratio", "流动比率"),
        "operating_cf": _num("ocf", "经营现金流"),
        "net_profit": _num("netprofit", "净利润"),
        "goodwill": _num("goodwill", "商誉"),
    }


class AkshareTechnicalAdapter:
    def __init__(self) -> None:
        self.service = MarketService(AkshareMarketClient())

    async def fetch_technical(self, stock_code: str) -> Dict[str, Any]:
        return await self.service.compose_technical_fields(stock_code)


class FixtureTechnicalAdapter:
    def __init__(self, fixtures: Optional[Dict[str, Any]] = None) -> None:
        self.service = MarketService(FixtureMarketClient(fixtures))

    async def fetch_technical(self, stock_code: str) -> Dict[str, Any]:
        return await self.service.compose_technical_fields(stock_code)


class AkshareSentimentAdapter:
    def __init__(self) -> None:
        self.service = MarketService(AkshareMarketClient())

    async def fetch_sentiment(self, stock_code: str) -> Dict[str, Any]:
        return await self.service.compose_sentiment_fields(stock_code)


class FixtureSentimentAdapter:
    def __init__(self, fixtures: Optional[Dict[str, Any]] = None) -> None:
        self.service = MarketService(FixtureMarketClient(fixtures))

    async def fetch_sentiment(self, stock_code: str) -> Dict[str, Any]:
        return await self.service.compose_sentiment_fields(stock_code)


class AkshareMacroAdapter:
    def __init__(self) -> None:
        self.service = MarketService(AkshareMarketClient())

    async def fetch_macro(self, stock_code: str) -> Dict[str, Any]:
        return await self.service.compose_macro_fields(stock_code)


class FixtureMacroAdapter:
    def __init__(self, fixtures: Optional[Dict[str, Any]] = None) -> None:
        self.service = MarketService(FixtureMarketClient(fixtures))

    async def fetch_macro(self, stock_code: str) -> Dict[str, Any]:
        return await self.service.compose_macro_fields(stock_code)
