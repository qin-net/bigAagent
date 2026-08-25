from __future__ import annotations

import asyncio
from typing import Any, Protocol

from .contracts import CollectionResult, DailyBar, NoticeHeadline, QuoteInput
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

    def collect_deep(self, stock_code: str) -> tuple[list[DailyBar], list[NoticeHeadline]]:
        import akshare as ak
        bars: list[DailyBar] = []
        hist_error = None
        for function_name, kwargs in (
            ("stock_zh_a_hist_tx", {"symbol": _exchange_symbol(stock_code)}),
            ("stock_zh_a_hist", {"symbol": stock_code, "period": "daily", "adjust": "qfq"}),
        ):
            try:
                table = getattr(ak, function_name)(**kwargs)
                for row in table.to_dict(orient="records")[-250:]:
                    bars.append(DailyBar(stock_code, str(row.get("日期") or row.get("date")), _number(row.get("开盘") or row.get("open")), _number(row.get("最高") or row.get("high")), _number(row.get("最低") or row.get("low")), _number(row.get("收盘") or row.get("close")), _number(row.get("成交量") or row.get("volume")), _number(row.get("成交额") or row.get("amount"))))
                if bars:
                    break
            except Exception as error:
                hist_error = error
        if not bars:
            raise RuntimeError("All daily bar sources failed: {}".format(hist_error))
        notices: list[NoticeHeadline] = []
        try:
            notice_table = ak.stock_individual_notice_report(security=stock_code)
            for row in notice_table.to_dict(orient="records")[:30]:
                title = str(row.get("公告标题") or row.get("标题") or row.get("title") or "").strip()
                if title:
                    notices.append(NoticeHeadline(stock_code, title, str(row.get("公告日期") or row.get("日期") or row.get("published_at") or "") or None, str(row.get("网址") or row.get("url") or "") or None, "akshare.stock_individual_notice_report"))
        except Exception:
            pass
        return bars, notices


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


def _exchange_symbol(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("4", "8", "9")):
        return "bj" + code
    return "sz" + code


async def collect_once(store: BoardStore, collector: QuoteCollector) -> str:
    try:
        result = await asyncio.to_thread(collector.collect)
        return await asyncio.to_thread(store.replace_quotes, result.quotes, source=result.source)
    except Exception as error:
        source = getattr(collector, "source", type(collector).__name__)
        await asyncio.to_thread(store.record_failure, source=source, error="{}: {}".format(type(error).__name__, error))
        raise
