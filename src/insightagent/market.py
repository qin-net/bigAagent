from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from .akshare_map import (
    HIST_VALUE_EM,
    bars_from_mapped,
    map_akshare_row,
    map_akshare_rows,
    map_row,
    parse_quantity,
)
from .contracts import utc_now
from .data_contracts import (
    AnnouncementHit,
    AnnouncementSearchResult,
    CashflowSnapshot,
    EventItem,
    EventSnapshot,
    FieldChange,
    FinancialSnapshot,
    IndicatorSnapshot,
    MacroSnapshot,
    PriceSnapshot,
    SnapshotDiff,
    StockProfile,
    ValuationSnapshot,
)
from .retry import ExponentialBackoff, RetryConfig, RetryableError

STOCK_CODE_RE = re.compile(r"^\d{6}$")
EVENT_KEYWORDS = (
    ("减持", "reduction"),
    ("增持", "increase"),
    ("回购", "buyback"),
    ("问询", "inquiry"),
    ("立案", "investigation"),
    ("诉讼", "lawsuit"),
    ("业绩预告", "earnings_preview"),
    ("业绩快报", "earnings_flash"),
    ("分红", "dividend"),
)


def normalize_stock_code(raw: str) -> str:
    code = raw.strip().upper().split(".")[0]
    if not STOCK_CODE_RE.fullmatch(code):
        raise ValueError("Stock code must be 6 digits, got {!r}".format(raw))
    return code


def to_float(value: Any) -> Optional[float]:
    number, unit = parse_quantity(value)
    if number is None:
        return None
    if unit == "yi_yuan":
        return number * 100_000_000.0
    if unit == "wan":
        return number * 10_000.0
    return number


def scale_accounting_amount(value: Optional[float]) -> Optional[float]:
    """Normalize raw yuan amounts to 亿元; leave already-scaled figures alone."""
    if value is None:
        return None
    if abs(value) >= 1_000_000:
        return round(value / 100_000_000.0, 4)
    return value


def exchange_symbol(stock_code: str) -> str:
    code = normalize_stock_code(stock_code)
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("8", "4")):
        return "bj" + code
    return "sz" + code


def em_report_symbol(stock_code: str) -> str:
    return exchange_symbol(stock_code).upper()


def em_dot_symbol(stock_code: str) -> str:
    code = normalize_stock_code(stock_code)
    suffix = "SH" if code.startswith("6") else "BJ" if code.startswith(("8", "4")) else "SZ"
    return "{}.{}".format(code, suffix)


def normalize_date(value: Any) -> Optional[str]:
    text = _text(value)
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return "{}-{}-{}".format(digits[:4], digits[4:6], digits[6:8])
    return text


def _row_date(row: Dict[str, Any]) -> Optional[datetime]:
    for key in (
        "数据日期",
        "日期",
        "报告期",
        "REPORT_DATE",
        "TRADE_DATE",
        "公告日期",
        "date",
        "报告日",
        "变动日期",
        "发布时间",
        "published_at",
    ):
        text = _text(row.get(key))
        if not text:
            continue
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 8:
            try:
                return datetime.strptime(digits[:8], "%Y%m%d")
            except ValueError:
                continue
    return None


def latest_record(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}
    dated = []
    for row in rows:
        stamp = _row_date(row)
        if stamp is not None:
            dated.append((stamp, row))
    if dated:
        dated.sort(key=lambda item: item[0])
        return dict(dated[-1][1])
    return dict(rows[-1])


def newest_records(rows: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if not rows:
        return []
    dated = [(_row_date(row), row) for row in rows]
    if any(stamp is not None for stamp, _row in dated):
        dated.sort(key=lambda item: item[0] or datetime.min, reverse=True)
        return [dict(row) for _stamp, row in dated[:limit]]
    return [dict(row) for row in rows[:limit]]


def bars_from_value_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return bars_from_mapped(map_row(row, HIST_VALUE_EM) for row in rows)


def missing_of(payload: Dict[str, Any], fields: Sequence[str]) -> List[str]:
    return [name for name in fields if payload.get(name) is None]


def sma(values: Sequence[float], window: int) -> Optional[float]:
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / float(window)


def ema(values: Sequence[float], window: int) -> Optional[List[float]]:
    if window <= 0 or len(values) < window:
        return None
    alpha = 2.0 / (window + 1.0)
    result = [sum(values[:window]) / float(window)]
    for price in values[window:]:
        result.append(alpha * price + (1.0 - alpha) * result[-1])
    return result


def rsi(values: Sequence[float], window: int = 14) -> Optional[float]:
    """Wilder RSI; uses the full series, not a last-window SMA."""
    if window <= 0 or len(values) <= window:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    if len(gains) < window:
        return None
    avg_gain = sum(gains[:window]) / float(window)
    avg_loss = sum(losses[:window]) / float(window)
    for gain, loss in zip(gains[window:], losses[window:]):
        avg_gain = (avg_gain * (window - 1) + gain) / float(window)
        avg_loss = (avg_loss * (window - 1) + loss) / float(window)
    if avg_loss == 0:
        return 100.0
    relative = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative))


def report_period_from_date(value: Any) -> Optional[str]:
    iso = normalize_date(value)
    if not iso or len(iso) < 10:
        return None
    return {
        "03-31": "q1",
        "06-30": "h1",
        "09-30": "q3",
        "12-31": "fy",
    }.get(iso[5:10])


def annual_roe_series(
    rows: Sequence[Dict[str, Any]], limit: int = 5
) -> List[float]:
    series: List[float] = []
    for row in newest_records(list(rows), len(rows) if rows else 0):
        if report_period_from_date(row.get("date")) != "fy":
            continue
        value = to_float(row.get("roe"))
        if value is None:
            continue
        series.append(value)
        if len(series) >= limit:
            break
    return series


def cashflow_yoy_pct(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    newest = newest_records(list(rows), 2)
    if len(newest) < 2:
        return None
    latest = to_float(newest[0].get("operating_cf"))
    previous = to_float(newest[1].get("operating_cf"))
    if latest is None or previous is None or previous == 0:
        return None
    return (latest - previous) / abs(previous) * 100.0


def ocf_to_net_profit(
    operating_cf: Optional[float], net_profit: Optional[float]
) -> Optional[float]:
    if operating_cf is None or net_profit is None or net_profit <= 0:
        return None
    return operating_cf / net_profit


def compute_indicators(
    stock_code: str,
    bars: Sequence[Dict[str, Any]],
    *,
    as_of: Optional[datetime] = None,
) -> IndicatorSnapshot:
    closes = [price for price in (to_float(bar.get("close")) for bar in bars) if price is not None]
    volumes = [
        volume
        for volume in (to_float(bar.get("volume")) for bar in bars)
        if volume is not None
    ]
    macd_line = None
    signal = None
    hist = None
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    if ema12 is not None and ema26 is not None:
        aligned12 = ema12[-len(ema26) :]
        macd_series = [left - right for left, right in zip(aligned12, ema26)]
        signal_series = ema(macd_series, 9)
        if signal_series:
            macd_line = macd_series[-1]
            signal = signal_series[-1]
            hist = macd_line - signal

    volume_ma20 = sma(volumes, 20)
    last_volume = volumes[-1] if volumes else None
    volume_ratio = None
    if last_volume is not None and volume_ma20 not in (None, 0):
        volume_ratio = last_volume / volume_ma20

    payload = {
        "ma5": sma(closes, 5),
        "ma10": sma(closes, 10),
        "ma20": sma(closes, 20),
        "ma60": sma(closes, 60),
        "macd": macd_line,
        "macd_signal": signal,
        "macd_hist": hist,
        "rsi14": rsi(closes, 14),
        "rsi_smoothing": "wilder",
        "volume_ratio": volume_ratio,
    }
    return IndicatorSnapshot(
        stock_code=stock_code,
        as_of=as_of or utc_now(),
        bars_used=len(closes),
        source="computed",
        missing_fields=missing_of(payload, list(payload)),
        **payload,
    )


def classify_event(title: str) -> str:
    for keyword, event_type in EVENT_KEYWORDS:
        if keyword in title:
            return event_type
    return "announcement"


def diff_snapshots(
    base: Dict[str, Any], current: Dict[str, Any]
) -> SnapshotDiff:
    skip = {"schema_version", "as_of", "source", "missing_fields", "rule_hits"}
    keys = sorted(set(base) | set(current))
    changed: List[FieldChange] = []
    unchanged = 0
    for key in keys:
        if key in skip:
            continue
        before = base.get(key)
        after = current.get(key)
        if before != after:
            changed.append(FieldChange(field=key, before=before, after=after))
        else:
            unchanged += 1
    return SnapshotDiff(changed_fields=changed, unchanged_count=unchanged)


def make_event_id(stock_code: str, material: str, *, kind: str = "") -> str:
    digest = hashlib.sha256(str(material).encode("utf-8")).hexdigest()[:12]
    if kind:
        return "event:{}:{}:{}".format(stock_code, kind, digest)
    return "event:{}:{}".format(stock_code, digest)


def events_from_announcements(
    stock_code: str,
    announcements: Iterable[Dict[str, Any]],
) -> EventSnapshot:
    events: List[EventItem] = []
    for item in announcements:
        title = str(item.get("title") or "")
        event_type = classify_event(title)
        if event_type == "announcement":
            continue
        events.append(
            EventItem(
                event_id=make_event_id(stock_code, title),
                event_type=event_type,
                title=title,
                published_at=item.get("published_at"),
                source=str(item.get("source") or "announcement"),
                url=item.get("url"),
                summary=title[:80] or None,
            )
        )
    return EventSnapshot(
        stock_code=stock_code,
        events=events,
        source="announcements",
    )


class MarketClient(Protocol):
    async def profile(self, stock_code: str) -> Dict[str, Any]:
        ...

    async def quote(self, stock_code: str) -> Dict[str, Any]:
        ...

    async def hist_daily(
        self, stock_code: str, *, limit: int = 120
    ) -> List[Dict[str, Any]]:
        ...

    async def valuation(self, stock_code: str) -> Dict[str, Any]:
        ...

    async def financials(self, stock_code: str) -> Dict[str, Any]:
        ...

    async def cashflow(self, stock_code: str) -> Dict[str, Any]:
        ...

    async def announcements(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        ...

    async def macro(self) -> Dict[str, Any]:
        ...

    async def news(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        ...

    async def holder_changes(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        ...


class FixtureMarketClient:
    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.payload = payload or synthetic_market_fixture("000858")

    async def profile(self, stock_code: str) -> Dict[str, Any]:
        return dict(self._for_stock(stock_code).get("profile") or {"stock_code": stock_code})

    async def quote(self, stock_code: str) -> Dict[str, Any]:
        return dict(self._for_stock(stock_code).get("quote") or {"stock_code": stock_code})

    async def hist_daily(
        self, stock_code: str, *, limit: int = 120
    ) -> List[Dict[str, Any]]:
        bars = list(self._for_stock(stock_code).get("hist") or [])
        return bars[-limit:]

    async def valuation(self, stock_code: str) -> Dict[str, Any]:
        return dict(self._for_stock(stock_code).get("valuation") or {"stock_code": stock_code})

    async def financials(self, stock_code: str) -> Dict[str, Any]:
        return dict(self._for_stock(stock_code).get("financials") or {"stock_code": stock_code})

    async def cashflow(self, stock_code: str) -> Dict[str, Any]:
        return dict(self._for_stock(stock_code).get("cashflow") or {"stock_code": stock_code})

    async def announcements(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        items = list(self._for_stock(stock_code).get("announcements") or [])
        return items[:limit]

    async def macro(self) -> Dict[str, Any]:
        return dict(self.payload.get("macro") or {})

    async def news(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        items = list(self._for_stock(stock_code).get("news") or [])
        return items[:limit]

    async def holder_changes(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        items = list(self._for_stock(stock_code).get("holder_changes") or [])
        return items[:limit]

    def _for_stock(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        stocks = self.payload.get("stocks") or {}
        if code in stocks:
            return stocks[code]
        if self.payload.get("stock_code") == code:
            return self.payload
        return {"stock_code": code}


class FlakyMarketClient:
    """Test double: fail N times with RetryableError, then delegate."""

    def __init__(self, inner: MarketClient, *, fail_times: int = 2) -> None:
        self.inner = inner
        self.fail_times = fail_times
        self.calls = 0

    async def quote(self, stock_code: str) -> Dict[str, Any]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RetryableError("transient market error")
        return await self.inner.quote(stock_code)


class AkshareMarketClient:
    def __init__(self) -> None:
        self._hist_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._hist_source: Dict[str, str] = {}
        self._value_cache: Dict[str, List[Dict[str, Any]]] = {}

    async def profile(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        try:
            table = await _akshare_call(
                "stock_individual_info_em", symbol=code
            )
            mapping = _info_map(table)
            mapped = map_akshare_row("stock_individual_info_em", mapping)
            return {
                "stock_code": code,
                "company_name": mapped.get("company_name"),
                "industry": mapped.get("industry"),
                "listing_date": mapped.get("listing_date"),
                "source": "akshare.stock_individual_info_em",
            }
        except Exception:
            industry = None
            try:
                table = await _akshare_call(
                    "stock_zyjs_ths", symbol=code, retries=0
                )
                rows = _records(table)
                latest = rows[-1] if rows else {}
                industry = _text(
                    latest.get("主营业务")
                    or latest.get("所属行业")
                    or latest.get("行业")
                )
            except Exception:
                industry = None
            notices = await self.announcements(code, limit=1)
            name = None
            if notices:
                title = notices[0].get("title") or ""
                name = title.split(":")[0] if title else None
            return {
                "stock_code": code,
                "company_name": _text(name),
                "industry": industry,
                "listing_date": None,
                "source": "akshare.fallback",
            }

    async def quote(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        realtime = await self._quote_from_bid_ask(code)
        if realtime and realtime.get("price") is not None:
            return realtime
        try:
            bars = await self.hist_daily(code, limit=5)
        except Exception:
            bars = []
        if bars:
            last = bars[-1]
            prev = bars[-2] if len(bars) > 1 else {}
            price = to_float(last.get("close"))
            pre_close = to_float(prev.get("close"))
            change_pct = None
            if price is not None and pre_close not in (None, 0):
                change_pct = (price - pre_close) / pre_close * 100.0
            return {
                "stock_code": code,
                "price": price,
                "open": to_float(last.get("open")),
                "high": to_float(last.get("high")),
                "low": to_float(last.get("low")),
                "pre_close": pre_close,
                "change_pct": change_pct,
                "volume": to_float(last.get("volume")),
                "turnover": to_float(last.get("turnover") or last.get("amount")),
                "source": self._hist_source.get(code, "akshare.hist"),
            }
        rows = await self._value_rows(code)
        latest = map_akshare_row("stock_value_em", latest_record(rows))
        return {
            "stock_code": code,
            "price": latest.get("price"),
            "change_pct": latest.get("change_pct"),
            "source": "akshare.stock_value_em",
        }

    async def _quote_from_bid_ask(self, code: str) -> Dict[str, Any]:
        try:
            table = await _akshare_call("stock_bid_ask_em", retries=0, symbol=code)
        except Exception:
            return {}
        mapping = _info_map(table)
        mapped = map_akshare_row("stock_bid_ask_em", mapping)
        if mapped.get("price") is None:
            return {}
        mapped["stock_code"] = code
        mapped["source"] = "akshare.stock_bid_ask_em"
        return mapped

    async def hist_daily(
        self, stock_code: str, *, limit: int = 120
    ) -> List[Dict[str, Any]]:
        code = normalize_stock_code(stock_code)
        cached = self._hist_cache.get(code)
        if cached:
            return cached[-limit:]

        attempts = (
            ("akshare.stock_zh_a_hist", self._hist_from_eastmoney),
            ("akshare.stock_zh_a_hist_tx", self._hist_from_tencent),
        )
        for source, loader in attempts:
            try:
                bars = await loader(code)
            except Exception:
                bars = []
            if bars:
                self._hist_cache[code] = bars
                self._hist_source[code] = source
                return bars[-limit:]

        rows = await self._value_rows(code)
        bars = bars_from_value_rows(rows)
        self._hist_cache[code] = bars
        self._hist_source[code] = "akshare.stock_value_em"
        return bars[-limit:]

    async def _hist_from_eastmoney(self, code: str) -> List[Dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=400)
        table = await _akshare_call(
            "stock_zh_a_hist",
            retries=0,
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
        return _parse_hist_rows(table, "stock_zh_a_hist")

    async def _hist_from_tencent(self, code: str) -> List[Dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=400)
        table = await _akshare_call(
            "stock_zh_a_hist_tx",
            retries=0,
            symbol=exchange_symbol(code),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
        return _parse_hist_rows(table, "stock_zh_a_hist_tx")

    async def _value_rows(self, stock_code: str) -> List[Dict[str, Any]]:
        if stock_code in self._value_cache:
            return self._value_cache[stock_code]
        table = await _akshare_call("stock_value_em", symbol=stock_code)
        rows = _records(table)
        self._value_cache[stock_code] = rows
        return rows

    async def valuation(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        rows = await self._value_rows(code)
        mapped_rows = map_akshare_rows("stock_value_em", rows)
        latest = latest_record(mapped_rows)
        pe_values = [row.get("pe") for row in mapped_rows]
        pb_values = [row.get("pb") for row in mapped_rows]
        pe = latest.get("pe")
        pb = latest.get("pb")
        return {
            "stock_code": code,
            "pe": pe,
            "pb": pb,
            "pe_percentile_5y": _percentile(pe_values[-250:], pe),
            "pb_percentile_5y": _percentile(pb_values[-250:], pb),
            "source": "akshare.stock_value_em",
        }

    async def financials(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        function_name, table = await _akshare_call_first(
            (
                (
                    "stock_financial_abstract_ths",
                    {"symbol": code, "indicator": "按报告期"},
                ),
                (
                    "stock_financial_analysis_indicator",
                    {"symbol": code, "start_year": "2018"},
                ),
                (
                    "stock_financial_analysis_indicator_em",
                    {"symbol": em_dot_symbol(code), "indicator": "按报告期"},
                ),
            )
        )
        mapped = map_akshare_rows(function_name, _records(table))
        latest = latest_record(mapped)
        return {
            "stock_code": code,
            "report_date": normalize_date(latest.get("date")),
            "roe": latest.get("roe"),
            "roe_series": annual_roe_series(mapped),
            "revenue_yoy": latest.get("revenue_yoy"),
            "profit_yoy": latest.get("profit_yoy"),
            "gross_margin": latest.get("gross_margin"),
            "debt_ratio": latest.get("debt_ratio"),
            "current_ratio": latest.get("current_ratio"),
            "net_profit": latest.get("net_profit"),
            "goodwill": latest.get("goodwill"),
            "non_recurring_profit_ratio": latest.get("non_recurring_profit_ratio"),
            "source": "akshare.{}".format(function_name),
        }

    async def cashflow(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        financials = await self.financials(code)
        function_name, table = await _akshare_call_first(
            (
                (
                    "stock_cash_flow_sheet_by_report_em",
                    {"symbol": em_report_symbol(code)},
                ),
                (
                    "stock_financial_report_sina",
                    {"stock": exchange_symbol(code), "symbol": "现金流量表"},
                ),
            )
        )
        mapped = map_akshare_rows(function_name, _records(table))
        latest = latest_record(mapped)
        net_profit = latest.get("net_profit")
        if net_profit is None:
            net_profit = financials.get("net_profit")
        return {
            "stock_code": code,
            "operating_cf": latest.get("operating_cf"),
            "cashflow_yoy": cashflow_yoy_pct(mapped),
            "net_profit": net_profit,
            "profit_yoy": financials.get("profit_yoy"),
            "source": "akshare.{}".format(function_name),
        }

    async def announcements(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        code = normalize_stock_code(stock_code)
        end = datetime.now(timezone.utc)
        begin = end - timedelta(days=180)
        try:
            table = await _akshare_call(
                "stock_individual_notice_report",
                retries=1,
                security=code,
                begin_date=begin.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
        except Exception:
            table = await _akshare_call(
                "stock_individual_notice_report",
                retries=1,
                security=code,
            )
        rows = newest_records(
            map_akshare_rows(
                "stock_individual_notice_report", _records(table)
            ),
            max(limit * 3, 30),
        )
        hits = []
        for item in rows:
            title = item.get("title")
            if not title:
                continue
            hits.append(
                {
                    "title": title,
                    "published_at": item.get("published_at"),
                    "notice_type": item.get("notice_type"),
                    "url": item.get("url"),
                    "source": "akshare.stock_individual_notice_report",
                }
            )
            if len(hits) >= limit:
                break
        return hits

    async def news(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        code = normalize_stock_code(stock_code)
        table = await _akshare_call("stock_news_em", symbol=code, retries=1)
        rows = newest_records(
            map_akshare_rows("stock_news_em", _records(table)), limit
        )
        hits = []
        for item in rows:
            title = item.get("title")
            if not title:
                continue
            hits.append(
                {
                    "title": title,
                    "published_at": item.get("published_at"),
                    "url": item.get("url"),
                    "source": item.get("source") or "akshare.stock_news_em",
                    "summary": item.get("summary"),
                }
            )
        return hits

    async def holder_changes(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        code = normalize_stock_code(stock_code)
        function_name, table = await _akshare_call_first(
            (
                ("stock_shareholder_change_ths", {"symbol": code}),
                ("stock_share_hold_change_szse", {"symbol": code}),
                ("stock_hold_change_cninfo", {"symbol": code}),
            )
        )
        rows = newest_records(
            map_akshare_rows(function_name, _records(table)), limit
        )
        items = []
        for item in rows:
            qty_text = item.get("raw_change") or ""
            note = item.get("note")
            name = item.get("holder_name")
            change_shares = item.get("change_shares")
            change_type = classify_event(
                " ".join(part for part in (qty_text, note, name) if part)
            )
            if change_type == "announcement":
                if "减持" in qty_text:
                    change_type = "reduction"
                elif "增持" in qty_text:
                    change_type = "increase"
                elif change_shares is not None and change_shares < 0:
                    change_type = "reduction"
                elif change_shares is not None and change_shares > 0:
                    change_type = "increase"
                else:
                    change_type = "holder_change"
            items.append(
                {
                    "holder_name": name,
                    "change_type": change_type,
                    "change_shares": change_shares,
                    "published_at": item.get("published_at"),
                    "note": note,
                }
            )
        return items

    async def macro(self) -> Dict[str, Any]:
        lpr_rows = map_akshare_rows(
            "macro_china_lpr", _records(await _akshare_call("macro_china_lpr"))
        )
        latest = latest_record(lpr_rows)
        shibor = _records(await _akshare_call_optional("macro_china_shibor_all"))
        shibor_latest = latest_record(shibor) if shibor else {}
        return {
            "lpr_1y": latest.get("lpr_1y"),
            "lpr_5y": latest.get("lpr_5y"),
            "shibor_overnight": _first_number(
                shibor_latest, ("Overnight_O/N_定价", "O/N", "overnight")
            ),
            "source": "akshare.macro_china_lpr",
        }


class MarketService:
    def __init__(self, client: MarketClient) -> None:
        self.client = client

    async def fetch_stock_profile(self, stock_code: str) -> StockProfile:
        code = normalize_stock_code(stock_code)
        raw = await self.client.profile(code)
        payload = {
            "stock_code": code,
            "company_name": _compact_name(raw.get("company_name")),
            "industry": _text(raw.get("industry")),
            "listing_date": normalize_date(raw.get("listing_date")),
            "source": str(raw.get("source") or "market"),
        }
        payload["missing_fields"] = missing_of(
            payload, ["company_name", "industry"]
        )
        return StockProfile.model_validate(payload)

    async def fetch_quote(self, stock_code: str) -> PriceSnapshot:
        code = normalize_stock_code(stock_code)
        raw = await self.client.quote(code)
        payload = {
            "stock_code": code,
            "price": to_float(raw.get("price")),
            "open": to_float(raw.get("open")),
            "high": to_float(raw.get("high")),
            "low": to_float(raw.get("low")),
            "pre_close": to_float(raw.get("pre_close")),
            "change_pct": to_float(raw.get("change_pct")),
            "volume": to_float(raw.get("volume")),
            "turnover": to_float(raw.get("turnover")),
            "source": str(raw.get("source") or "market"),
        }
        payload["missing_fields"] = missing_of(payload, ["price", "volume"])
        return PriceSnapshot.model_validate(payload)

    async def fetch_valuation(self, stock_code: str) -> ValuationSnapshot:
        code = normalize_stock_code(stock_code)
        raw = await self.client.valuation(code)
        payload = {
            "stock_code": code,
            "pe": to_float(raw.get("pe")),
            "pb": to_float(raw.get("pb")),
            "pe_percentile_5y": to_float(raw.get("pe_percentile_5y")),
            "pb_percentile_5y": to_float(raw.get("pb_percentile_5y")),
            "source": str(raw.get("source") or "market"),
        }
        payload["missing_fields"] = missing_of(payload, ["pe", "pb"])
        return ValuationSnapshot.model_validate(payload)

    async def fetch_financials(self, stock_code: str) -> FinancialSnapshot:
        code = normalize_stock_code(stock_code)
        raw = await self.client.financials(code)
        payload = {
            "stock_code": code,
            "report_date": normalize_date(raw.get("report_date")),
            "roe": to_float(raw.get("roe")),
            "roe_series": [
                value
                for value in (to_float(item) for item in (raw.get("roe_series") or []))
                if value is not None
            ],
            "revenue_yoy": to_float(raw.get("revenue_yoy")),
            "profit_yoy": to_float(raw.get("profit_yoy")),
            "gross_margin": to_float(raw.get("gross_margin")),
            "debt_ratio": to_float(raw.get("debt_ratio")),
            "current_ratio": to_float(raw.get("current_ratio")),
            "net_profit": scale_accounting_amount(to_float(raw.get("net_profit"))),
            "goodwill": scale_accounting_amount(to_float(raw.get("goodwill"))),
            "non_recurring_profit_ratio": to_float(
                raw.get("non_recurring_profit_ratio")
            ),
            "source": str(raw.get("source") or "market"),
        }
        payload["missing_fields"] = missing_of(
            payload, ["roe", "debt_ratio", "profit_yoy", "net_profit"]
        )
        return FinancialSnapshot.model_validate(payload)

    async def fetch_cashflow(self, stock_code: str) -> CashflowSnapshot:
        code = normalize_stock_code(stock_code)
        raw = await self.client.cashflow(code)
        payload = {
            "stock_code": code,
            "operating_cf": scale_accounting_amount(
                to_float(raw.get("operating_cf"))
            ),
            "cashflow_yoy": to_float(raw.get("cashflow_yoy")),
            "net_profit": scale_accounting_amount(to_float(raw.get("net_profit"))),
            "profit_yoy": to_float(raw.get("profit_yoy")),
            "source": str(raw.get("source") or "market"),
        }
        payload["missing_fields"] = missing_of(
            payload, ["operating_cf", "net_profit"]
        )
        return CashflowSnapshot.model_validate(payload)

    async def get_price_snapshot(self, stock_code: str) -> PriceSnapshot:
        return await self.fetch_quote(stock_code)

    async def get_indicator_snapshot(self, stock_code: str) -> IndicatorSnapshot:
        code = normalize_stock_code(stock_code)
        try:
            bars = await self.client.hist_daily(code, limit=120)
        except Exception:
            bars = []
        snapshot = compute_indicators(code, bars)
        source = getattr(self.client, "_hist_source", {}).get(code)
        if source:
            snapshot.source = "computed:{}".format(source)
        return snapshot

    async def get_kline_snapshot(
        self, stock_code: str, limit: int = 30
    ) -> "KlineSnapshot":
        from .data_contracts import KlineBar, KlineSnapshot

        code = normalize_stock_code(stock_code)
        try:
            bars = await self.client.hist_daily(code, limit=max(limit, 60))
        except Exception:
            bars = []
        source = getattr(self.client, "_hist_source", {}).get(code, "market")
        last_close = to_float(bars[-1].get("close")) if bars else None
        recent = bars[-limit:]
        payload = {
            "stock_code": code,
            "source": source,
            "bars_used": len(bars),
            "last_close": last_close,
            "bars": [
                KlineBar(
                    date=_text(bar.get("date")),
                    open=to_float(bar.get("open")),
                    high=to_float(bar.get("high")),
                    low=to_float(bar.get("low")),
                    close=to_float(bar.get("close")),
                    volume=to_float(bar.get("volume")),
                )
                for bar in recent
            ],
        }
        payload["missing_fields"] = [] if last_close is not None else ["last_close"]
        return KlineSnapshot.model_validate(payload)

    async def search_news(
        self, stock_code: str, query: str = "", limit: int = 10
    ) -> "NewsSearchResult":
        from .data_contracts import NewsHit, NewsSearchResult

        code = normalize_stock_code(stock_code)
        items = await self.client.news(code, limit=50)
        tokens = [token.lower() for token in query.split() if token]
        hits = []
        for item in items:
            title = str(item.get("title") or "")
            haystack = " ".join(
                [title, str(item.get("summary") or "")]
            ).lower()
            if tokens and not any(token in haystack for token in tokens):
                continue
            hits.append(
                NewsHit(
                    title=title,
                    published_at=_text(item.get("published_at")),
                    source=str(item.get("source") or "market"),
                    url=_text(item.get("url")),
                    summary=_text(item.get("summary")),
                )
            )
            if len(hits) >= limit:
                break
        return NewsSearchResult(stock_code=code, query=query, hits=hits)

    async def get_holder_changes(
        self, stock_code: str
    ) -> "HolderChangeSnapshot":
        from .data_contracts import HolderChangeItem, HolderChangeSnapshot

        code = normalize_stock_code(stock_code)
        raw_items = await self.client.holder_changes(code, limit=20)
        items = [
            HolderChangeItem(
                holder_name=_text(item.get("holder_name")),
                change_type=_text(item.get("change_type")),
                change_shares=to_float(item.get("change_shares")),
                published_at=_text(item.get("published_at")),
                note=_text(item.get("note")),
            )
            for item in raw_items
        ]
        return HolderChangeSnapshot(
            stock_code=code,
            items=items,
            source="market",
        )

    async def get_event_snapshot(self, stock_code: str) -> EventSnapshot:
        code = normalize_stock_code(stock_code)
        announcements = await self.client.announcements(code, limit=30)
        snapshot = events_from_announcements(code, announcements)
        holders = await self.client.holder_changes(code, limit=10)
        for item in holders:
            title = item.get("note") or item.get("holder_name") or "股东持股变动"
            snapshot.events.append(
                EventItem(
                    event_id=make_event_id(code, str(title), kind="holder"),
                    event_type=str(item.get("change_type") or "holder_change"),
                    title=str(title),
                    published_at=_text(item.get("published_at")),
                    source="holder_changes",
                    summary=_text(item.get("holder_name")),
                )
            )
        return snapshot

    async def get_macro_snapshot(self) -> MacroSnapshot:
        raw = await self.client.macro()
        payload = {
            "lpr_1y": to_float(raw.get("lpr_1y")),
            "lpr_5y": to_float(raw.get("lpr_5y")),
            "shibor_overnight": to_float(raw.get("shibor_overnight")),
            "notes": list(raw.get("notes") or []),
            "source": str(raw.get("source") or "market"),
        }
        payload["missing_fields"] = missing_of(payload, ["lpr_1y"])
        return MacroSnapshot.model_validate(payload)

    async def search_announcements(
        self, stock_code: str, query: str = "", limit: int = 10
    ) -> AnnouncementSearchResult:
        code = normalize_stock_code(stock_code)
        items = await self.client.announcements(code, limit=50)
        tokens = [token.lower() for token in query.split() if token]
        hits = []
        for item in items:
            title = str(item.get("title") or "")
            haystack = title.lower()
            if tokens and not any(token in haystack for token in tokens):
                continue
            hits.append(
                AnnouncementHit(
                    title=title,
                    published_at=_text(item.get("published_at")),
                    notice_type=_text(item.get("notice_type")),
                    url=_text(item.get("url")),
                    source=str(item.get("source") or "market"),
                )
            )
            if len(hits) >= limit:
                break
        return AnnouncementSearchResult(
            stock_code=code, query=query, hits=hits
        )

    async def compose_fundamental_fields(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        profile = await self.fetch_stock_profile(code)
        quote = await self.fetch_quote(code)
        valuation = await self.fetch_valuation(code)
        financials = await self.fetch_financials(code)
        cashflow = await self.fetch_cashflow(code)
        net_profit = cashflow.net_profit or financials.net_profit
        operating_cf = cashflow.operating_cf
        return {
            "stock_code": code,
            "company_name": profile.company_name,
            "price": quote.price,
            "pe": valuation.pe,
            "pb": valuation.pb,
            "pe_percentile_5y": valuation.pe_percentile_5y,
            "pb_percentile_5y": valuation.pb_percentile_5y,
            "roe": financials.roe,
            "roe_series": list(financials.roe_series or []),
            "roe_report_period": report_period_from_date(financials.report_date),
            "revenue_yoy": financials.revenue_yoy,
            "profit_yoy": financials.profit_yoy or cashflow.profit_yoy,
            "gross_margin": financials.gross_margin,
            "debt_ratio": financials.debt_ratio,
            "current_ratio": financials.current_ratio,
            "operating_cf": operating_cf,
            "net_profit": net_profit,
            "cashflow_yoy": cashflow.cashflow_yoy,
            "ocf_to_np": ocf_to_net_profit(operating_cf, net_profit),
            "goodwill": financials.goodwill,
            "non_recurring_profit_ratio": financials.non_recurring_profit_ratio,
        }

    async def compose_technical_fields(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        indicator = await self.get_indicator_snapshot(code)
        kline = await self.get_kline_snapshot(code)
        quote = await self.fetch_quote(code)
        return {
            "indicator": indicator.model_dump(mode="json"),
            "kline": kline.model_dump(mode="json"),
            "price": quote.model_dump(mode="json"),
        }

    async def compose_sentiment_fields(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        events = await self.get_event_snapshot(code)
        holders = await self.get_holder_changes(code)
        news = await self.search_news(code, limit=20)
        announcements = await self.search_announcements(code, limit=20)
        return {
            "events": events.model_dump(mode="json"),
            "holders": holders.model_dump(mode="json"),
            "news": news.model_dump(mode="json"),
            "announcements": announcements.model_dump(mode="json"),
        }

    async def compose_macro_fields(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        macro = await self.get_macro_snapshot()
        profile = await self.fetch_stock_profile(code)
        return {
            "macro": macro.model_dump(mode="json"),
            "industry": profile.industry or "",
            "stock_code": code,
            "company_name": profile.company_name or "",
        }


_FIXTURE_PROFILES = {
    "000858": ("五粮液", "白酒"),
    "600519": ("贵州茅台", "白酒"),
    "000001": ("平安银行", "银行"),
    "600036": ("招商银行", "银行"),
    "601318": ("中国平安", "保险"),
    "000333": ("美的集团", "家用电器"),
}


def synthetic_market_fixture(stock_code: str) -> Dict[str, Any]:
    code = normalize_stock_code(stock_code)
    company_name, industry = _FIXTURE_PROFILES.get(code, ("测试公司", "食品"))
    close = 120.0
    hist = []
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for index in range(80):
        close = close * (1.002 if index % 7 else 0.997)
        volume = 1_200_000 + index * 8000
        day = start + timedelta(days=index)
        hist.append(
            {
                "date": day.date().isoformat(),
                "open": round(close - 0.4, 2),
                "close": round(close, 2),
                "high": round(close + 0.8, 2),
                "low": round(close - 0.9, 2),
                "volume": float(volume),
                "turnover": float(volume * close),
            }
        )
    last = hist[-1]
    return {
        "stock_code": code,
        "profile": {
            "stock_code": code,
            "company_name": company_name,
            "industry": industry,
            "listing_date": "1998-04-27",
            "source": "fixture",
        },
        "quote": {
            "stock_code": code,
            "price": last["close"],
            "open": last["open"],
            "high": last["high"],
            "low": last["low"],
            "pre_close": hist[-2]["close"],
            "change_pct": (last["close"] - hist[-2]["close"])
            / hist[-2]["close"]
            * 100.0,
            "volume": last["volume"],
            "turnover": last["turnover"],
            "source": "fixture",
        },
        "hist": hist,
        "valuation": {
            "stock_code": code,
            "pe": 18.5,
            "pb": 4.2,
            "pe_percentile_5y": 45.0,
            "pb_percentile_5y": 52.0,
            "source": "fixture",
        },
        "financials": {
            "stock_code": code,
            "roe": 16.8,
            "revenue_yoy": 6.4,
            "profit_yoy": 8.5,
            "gross_margin": 74.2,
            "debt_ratio": 32.1,
            "current_ratio": 3.4,
            "net_profit": 80.0,
            "goodwill": 0.0,
            "non_recurring_profit_ratio": 2.1,
            "source": "fixture",
        },
        "cashflow": {
            "stock_code": code,
            "operating_cf": -12.4,
            "net_profit": 80.0,
            "profit_yoy": 8.5,
            "source": "fixture",
        },
        "announcements": [
            {
                "title": "关于控股股东拟减持股份的提示性公告",
                "published_at": "2026-07-20",
                "notice_type": "减持",
                "url": "https://example.invalid/reduce",
                "source": "fixture",
            },
            {
                "title": "2025年年度报告",
                "published_at": "2026-03-30",
                "notice_type": "定期报告",
                "source": "fixture",
            },
            {
                "title": "关于回购公司股份的进展公告",
                "published_at": "2026-08-01",
                "notice_type": "回购",
                "source": "fixture",
            },
        ],
        "macro": {
            "lpr_1y": 3.0,
            "lpr_5y": 3.5,
            "shibor_overnight": 1.4,
            "notes": ["fixture rates"],
            "source": "fixture",
        },
        "news": [
            {
                "title": "{}渠道库存去化进展报道".format(company_name),
                "published_at": "2026-07-28",
                "source": "fixture",
                "summary": "{}渠道库存继续下降".format(industry),
            }
        ],
        "holder_changes": [
            {
                "holder_name": "宜宾市国资",
                "change_type": "increase",
                "change_shares": 1200000,
                "published_at": "2026-08-08",
                "note": "增持公司股份",
            }
        ],
    }


async def _akshare_call(name: str, *, retries: int = 2, **kwargs: Any) -> Any:
    ak = _import_akshare()
    func = getattr(ak, name, None)
    if func is None:
        raise RuntimeError("akshare has no {}".format(name))

    retry = ExponentialBackoff(
        RetryConfig(
            max_retries=retries,
            base_delay=0.8,
            backoff_factor=2.0,
            jitter_min=0.0,
            jitter_max=0.3,
            max_delay=5.0,
        )
    )

    async def operation():
        return await _to_thread(func, **kwargs)

    return await retry.execute(operation)


async def _akshare_call_optional(name: str, **kwargs: Any) -> Any:
    try:
        return await _akshare_call(name, **kwargs)
    except Exception:
        return None


async def _akshare_call_first(
    candidates: Sequence[tuple], *, retries: int = 0
) -> Tuple[str, Any]:
    last_error = None
    ak = _import_akshare()
    for name, kwargs in candidates:
        if getattr(ak, name, None) is None:
            continue
        try:
            return name, await _akshare_call(name, retries=retries, **kwargs)
        except Exception as error:
            last_error = error
            continue
    if last_error:
        raise last_error
    raise RuntimeError("no available akshare candidate")


async def _to_thread(func, **kwargs: Any) -> Any:
    import asyncio

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_call_with_retryable_errors, func, kwargs),
            timeout=45.0,
        )
    except asyncio.TimeoutError as error:
        raise RetryableError("akshare timeout") from error


def _call_with_retryable_errors(func, kwargs: Dict[str, Any]) -> Any:
    try:
        return func(**kwargs)
    except TypeError:
        if "symbol" in kwargs and "stock" not in kwargs:
            retry_kwargs = dict(kwargs)
            retry_kwargs["stock"] = retry_kwargs.pop("symbol")
            return func(**retry_kwargs)
        raise
    except Exception as error:
        if _is_transient_market_error(error):
            raise RetryableError(str(error)) from error
        raise


def _is_transient_market_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True
    message = str(error).lower()
    tokens = (
        "timeout",
        "timed out",
        "connection aborted",
        "remotely closed",
        "remote end closed",
        "连接",
        "频繁",
        "429",
        "max retries",
        "protocolerror",
    )
    return any(token in message for token in tokens)


def _import_akshare():
    try:
        import akshare as ak
    except ImportError as error:
        raise RuntimeError("akshare is not installed; use fixture tools") from error
    return ak


def _records(table: Any) -> List[Dict[str, Any]]:
    if table is None:
        return []
    if isinstance(table, list):
        return [item for item in table if isinstance(item, dict)]
    if hasattr(table, "to_dict"):
        try:
            return table.to_dict(orient="records")
        except Exception:
            return []
    return []


def _parse_hist_rows(table: Any, function_name: str) -> List[Dict[str, Any]]:
    return [
        bar
        for bar in bars_from_mapped(
            map_akshare_rows(function_name, _records(table))
        )
        if bar.get("close") is not None
    ]


def _info_map(table: Any) -> Dict[str, Any]:
    rows = _records(table)
    mapping: Dict[str, Any] = {}
    for row in rows:
        key = row.get("item") or row.get("指标") or row.get("name")
        value = row.get("value") or row.get("数值") or row.get("value")
        if key is not None:
            mapping[str(key)] = value
    if not mapping and hasattr(table, "set_index"):
        try:
            series = table.set_index("item")["value"]
            mapping = {str(key): series[key] for key in series.index}
        except Exception:
            mapping = {}
    return mapping


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compact_name(value: Any) -> Optional[str]:
    text = _text(value)
    if not text:
        return None
    return re.sub(r"\s+", "", text)


def _first_number(row: Dict[str, Any], names: Sequence[str]) -> Optional[float]:
    for name in names:
        if name in row:
            number = to_float(row.get(name))
            if number is not None:
                return number
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        number = to_float(lowered.get(name.lower()))
        if number is not None:
            return number
    for name in names:
        for key, value in row.items():
            key_text = str(key)
            if key_text.startswith(name + "(") or key_text.startswith(name + "_"):
                number = to_float(value)
                if number is not None:
                    return number
    return None


def _percentile(values: Sequence[Optional[float]], current: Optional[float]) -> Optional[float]:
    clean = [value for value in values if value is not None]
    if current is None or not clean:
        return None
    below = sum(1 for value in clean if value <= current)
    return round(below / len(clean) * 100.0, 2)
