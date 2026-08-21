from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from .business_contracts import FundamentalSnapshot, RuleHit
from .contracts import utc_now
from .market import AkshareMarketClient, FixtureMarketClient, MarketService

REQUIRED_FIELDS = (
    "pe",
    "pb",
    "roe",
    "debt_ratio",
    "operating_cf",
    "net_profit",
    "profit_yoy",
)

METHODOLOGY_ENTRIES = [
    {
        "id": "kb_roe_quality",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "roe leverage 盈利能力",
        "text": "长期 ROE 高且不依赖过高负债，更接近优秀企业标准。",
    },
    {
        "id": "kb_cashflow_lag",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "现金流 盈利质量 净利润",
        "text": "净利润增长但经营现金流为负或不支持利润时，应检查盈利质量。",
    },
    {
        "id": "kb_leverage",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "负债 杠杆 财务风险",
        "text": "资产负债率过高会放大下行风险，需降低安全边际评价。",
    },
    {
        "id": "kb_valuation",
        "scope": ["fundamental"],
        "status": "approved",
        "trigger": "估值 pe 分位 安全边际",
        "text": "估值应对照自身历史分位，而不是只看单一 PE。",
    },
    {
        "id": "kb_macro_rates",
        "scope": ["macro"],
        "status": "approved",
        "trigger": "lpr 利率 宏观",
        "text": "宏观只提供环境标签，不构成个股买卖理由。",
    },
]


def apply_fundamental_rules(
    snapshot: FundamentalSnapshot,
) -> FundamentalSnapshot:
    hits: List[RuleHit] = []
    flags: List[str] = []

    roe_hit = (
        snapshot.roe is not None
        and snapshot.roe >= 15
        and snapshot.debt_ratio is not None
        and snapshot.debt_ratio < 60
    )
    hits.append(
        RuleHit(
            rule_id="roe_quality",
            hit=bool(roe_hit),
            detail="ROE>=15 and debt_ratio<60",
        )
    )
    if roe_hit:
        flags.append("roe_quality")

    cash_hit = (
        snapshot.profit_yoy is not None
        and snapshot.profit_yoy > 0
        and snapshot.operating_cf is not None
        and snapshot.operating_cf <= 0
    )
    hits.append(
        RuleHit(
            rule_id="cashflow_lag",
            hit=bool(cash_hit),
            detail="profit_yoy>0 and operating_cf<=0",
        )
    )
    if cash_hit:
        flags.append("cashflow_lag")

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


def search_methodology(query: str) -> List[Dict[str, str]]:
    tokens = [token.lower() for token in query.replace("/", " ").split() if token]
    results = []
    for entry in METHODOLOGY_ENTRIES:
        if entry["status"] != "approved":
            continue
        haystack = " ".join(
            [entry["id"], entry["trigger"], entry["text"]]
        ).lower()
        if not tokens or any(token in haystack for token in tokens):
            results.append(
                {
                    "id": entry["id"],
                    "text": entry["text"],
                    "trigger": entry["trigger"],
                }
            )
    return results[:5]


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
