from __future__ import annotations

from typing import List

from .business_contracts import FundamentalSnapshot
from .data_contracts import (
    AnnouncementSearchResult,
    CashflowSnapshot,
    EmptyInput,
    EventSnapshot,
    FinancialSnapshot,
    HolderChangeSnapshot,
    IndicatorSnapshot,
    KlineInput,
    KlineSnapshot,
    MacroSnapshot,
    NewsSearchResult,
    PriceSnapshot,
    SearchAnnouncementsInput,
    SearchNewsInput,
    SnapshotDiff,
    SnapshotDiffInput,
    StockCodeInput,
    StockProfile,
    ValuationSnapshot,
)
from .fundamentals import apply_fundamental_rules
from .market import MarketService, diff_snapshots
from .resources import FunctionResource


def build_market_tools(service: MarketService) -> List[FunctionResource]:
    async def fetch_stock_profile(stock_code: str) -> StockProfile:
        return await service.fetch_stock_profile(stock_code)

    async def fetch_quote(stock_code: str) -> PriceSnapshot:
        return await service.fetch_quote(stock_code)

    async def fetch_valuation(stock_code: str) -> ValuationSnapshot:
        return await service.fetch_valuation(stock_code)

    async def fetch_financials(stock_code: str) -> FinancialSnapshot:
        return await service.fetch_financials(stock_code)

    async def fetch_cashflow(stock_code: str) -> CashflowSnapshot:
        return await service.fetch_cashflow(stock_code)

    async def get_price_snapshot(stock_code: str) -> PriceSnapshot:
        return await service.get_price_snapshot(stock_code)

    async def get_indicator_snapshot(stock_code: str) -> IndicatorSnapshot:
        return await service.get_indicator_snapshot(stock_code)

    async def get_kline_snapshot(stock_code: str, limit: int = 30) -> KlineSnapshot:
        return await service.get_kline_snapshot(stock_code, limit=limit)

    async def get_event_snapshot(stock_code: str) -> EventSnapshot:
        return await service.get_event_snapshot(stock_code)

    async def get_macro_snapshot(dummy: str = "") -> MacroSnapshot:
        return await service.get_macro_snapshot()

    async def search_announcements(
        stock_code: str, query: str = "", limit: int = 10
    ) -> AnnouncementSearchResult:
        return await service.search_announcements(
            stock_code, query=query, limit=limit
        )

    async def search_news(
        stock_code: str, query: str = "", limit: int = 10
    ) -> NewsSearchResult:
        return await service.search_news(stock_code, query=query, limit=limit)

    async def get_holder_changes(stock_code: str) -> HolderChangeSnapshot:
        return await service.get_holder_changes(stock_code)

    async def get_fundamental_snapshot(stock_code: str) -> FundamentalSnapshot:
        payload = await service.compose_fundamental_fields(stock_code)
        return apply_fundamental_rules(FundamentalSnapshot.model_validate(payload))

    def diff_snapshot(base: dict, current: dict) -> SnapshotDiff:
        return diff_snapshots(base, current)

    specs = [
        (
            fetch_stock_profile,
            "fetch_stock_profile",
            "Fetch A-share company profile: name, industry, listing date.",
            StockCodeInput,
            StockProfile,
        ),
        (
            fetch_quote,
            "fetch_quote",
            "Fetch latest daily quote: price, open/high/low, volume.",
            StockCodeInput,
            PriceSnapshot,
        ),
        (
            fetch_valuation,
            "fetch_valuation",
            "Fetch PE/PB and 5-year percentile if available.",
            StockCodeInput,
            ValuationSnapshot,
        ),
        (
            fetch_financials,
            "fetch_financials",
            "Fetch financial quality fields: ROE, margins, leverage, yoy.",
            StockCodeInput,
            FinancialSnapshot,
        ),
        (
            fetch_cashflow,
            "fetch_cashflow",
            "Fetch operating cash flow versus net profit, scaled to 亿元.",
            StockCodeInput,
            CashflowSnapshot,
        ),
        (
            get_price_snapshot,
            "get_price_snapshot",
            "Read the current price snapshot for technical or tracking use.",
            StockCodeInput,
            PriceSnapshot,
        ),
        (
            get_indicator_snapshot,
            "get_indicator_snapshot",
            "Read precomputed MA/MACD/RSI/volume-ratio from daily bars.",
            StockCodeInput,
            IndicatorSnapshot,
        ),
        (
            get_kline_snapshot,
            "get_kline_snapshot",
            "Read recent daily OHLC bars. Uses Tencent/valuation fallbacks.",
            KlineInput,
            KlineSnapshot,
        ),
        (
            get_event_snapshot,
            "get_event_snapshot",
            "Read reduction/buyback/inquiry and holder-change events.",
            StockCodeInput,
            EventSnapshot,
        ),
        (
            get_macro_snapshot,
            "get_macro_snapshot",
            "Read lightweight macro labels such as LPR.",
            EmptyInput,
            MacroSnapshot,
        ),
        (
            search_announcements,
            "search_announcements",
            "Search recent company announcements by keyword.",
            SearchAnnouncementsInput,
            AnnouncementSearchResult,
        ),
        (
            search_news,
            "search_news",
            "Search company news headlines from East Money.",
            SearchNewsInput,
            NewsSearchResult,
        ),
        (
            get_holder_changes,
            "get_holder_changes",
            "Read recent shareholder increase/decrease records.",
            StockCodeInput,
            HolderChangeSnapshot,
        ),
        (
            get_fundamental_snapshot,
            "get_fundamental_snapshot",
            "Compose profile, quote, valuation, financials and cashflow into one snapshot.",
            StockCodeInput,
            FundamentalSnapshot,
        ),
        (
            diff_snapshot,
            "diff_snapshot",
            "Diff two snapshot dicts and list changed scalar fields.",
            SnapshotDiffInput,
            SnapshotDiff,
        ),
    ]
    tools = []
    for func, name, description, input_model, output_model in specs:
        tools.append(
            FunctionResource(
                func=func,
                name=name,
                description=description,
                input_model=input_model,
                output_model=output_model,
                timeout_seconds=45.0,
                retry_policy="market",
            )
        )
    return tools
