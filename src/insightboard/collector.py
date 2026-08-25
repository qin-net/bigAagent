from __future__ import annotations

import asyncio
from typing import Any, Protocol

from .contracts import CollectionResult, QuoteInput
from .store import BoardStore


class QuoteCollector(Protocol):
    def collect(self) -> CollectionResult:
        ...


class AkshareQuoteCollector:
    source = "akshare.stock_zh_a_spot_tx"

    def collect(self) -> CollectionResult:
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("Install board dependencies with pip install -e '.[board]'") from error
        failures = []
        for source, function_name, mapper in (
            ("akshare.stock_zh_a_spot_tx", "stock_zh_a_spot_tx", self._map_tencent_row),
            ("akshare.stock_zh_a_spot", "stock_zh_a_spot", self._map_sina_row),
        ):
            try:
                table = getattr(ak, function_name)()
                quotes = [mapper(row) for row in table.to_dict(orient="records")]
                quotes = [quote for quote in quotes if quote is not None]
                if quotes:
                    return CollectionResult(source=source, quotes=quotes)
                failures.append("{} returned no valid rows".format(function_name))
            except Exception as error:
                failures.append("{}: {}".format(function_name, error))
        raise RuntimeError("All quote sources failed: {}".format("; ".join(failures)))

    @staticmethod
    def _map_tencent_row(row: dict[str, Any]) -> QuoteInput | None:
        raw_code = str(row.get("code") or "").strip().lower()
        code = raw_code[-6:] if len(raw_code) >= 6 else raw_code.zfill(6)
        name = str(row.get("name") or "").strip()
        if not code.isdigit() or len(code) != 6 or not name:
            return None
        return QuoteInput(
            stock_code=code,
            name=name,
            market=_market_for(code),
            price=_number(row.get("zxj")),
            change_pct=_number(row.get("zdf")),
            volume=_number(row.get("volume")),
            # 腾讯接口的 turnover 单位为万元，统一转换为元再入库。
            turnover=_wan_to_yuan(row.get("turnover")),
            pe=_number(row.get("pe_ttm")),
        )

    @staticmethod
    def _map_sina_row(row: dict[str, Any]) -> QuoteInput | None:
        # AKShare's Sina implementation can expose mojibake column labels on Windows.
        values = list(row.values())
        if len(values) < 14:
            return None
        raw_code = str(values[0] or "").strip().lower()
        code = raw_code[-6:] if len(raw_code) >= 6 else raw_code.zfill(6)
        name = str(row.get("名称") or "").strip()
        if not name:
            name = str(values[1] or "").strip()
        if not code.isdigit() or len(code) != 6 or not name:
            return None
        return QuoteInput(
            stock_code=code,
            name=name,
            market=_market_for(code),
            price=_number(values[2]),
            change_pct=_number(values[4]),
            open=_number(values[5]),
            high=_number(values[9]),
            low=_number(values[10]),
            volume=_number(values[11]),
            turnover=_number(values[12]),
        )


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wan_to_yuan(value: Any) -> float | None:
    number = _number(value)
    return number * 10_000 if number is not None else None


def _market_for(code: str) -> str:
    if code.startswith("6"):
        return "sh"
    if code.startswith(("4", "8", "9")):
        return "bj"
    return "sz"


async def collect_once(store: BoardStore, collector: QuoteCollector) -> str:
    try:
        result = await asyncio.to_thread(collector.collect)
        return await asyncio.to_thread(store.replace_quotes, result.quotes, source=result.source)
    except Exception as error:
        source = getattr(collector, "source", type(collector).__name__)
        await asyncio.to_thread(store.record_failure, source=source, error="{}: {}".format(type(error).__name__, error))
        raise
