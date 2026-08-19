from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence

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
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number:  # NaN
            return None
        return number
    text = str(value).strip().replace(",", "")
    for prefix in ("增持", "减持", "持股"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = text.replace("%", "").strip()
    if text in {"", "-", "None", "nan", "NaN", "False", "True"}:
        return None
    multiplier = 1.0
    if text.endswith("亿元"):
        text = text[:-2].strip()
        multiplier = 100_000_000.0
    elif text.endswith("亿"):
        text = text[:-1].strip()
        multiplier = 100_000_000.0
    elif text.endswith("万"):
        text = text[:-1].strip()
        multiplier = 10_000.0
    try:
        return float(text) * multiplier
    except ValueError:
        return None


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
    bars: List[Dict[str, Any]] = []
    for row in rows:
        close = to_float(row.get("当日收盘价") or row.get("close"))
        if close is None:
            continue
        bars.append(
            {
                "date": _text(row.get("数据日期") or row.get("date")),
                "open": close,
                "close": close,
                "high": close,
                "low": close,
                "volume": to_float(row.get("成交量") or row.get("volume")),
                "turnover": to_float(row.get("总市值")),
            }
        )
    return bars


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
    if len(values) <= window:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(-window, 0):
        delta = values[index] - values[index - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    relative = (gains / window) / (losses / window)
    return 100.0 - (100.0 / (1.0 + relative))


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
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
        events.append(
            EventItem(
                event_id="{}-{}".format(stock_code, digest),
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
            name = (
                mapping.get("股票简称")
                or mapping.get("名称")
                or mapping.get("证券简称")
            )
            return {
                "stock_code": code,
                "company_name": _text(name),
                "industry": _text(mapping.get("行业")),
                "listing_date": normalize_date(
                    mapping.get("上市时间") or mapping.get("上市日期")
                ),
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
        latest = latest_record(rows)
        return {
            "stock_code": code,
            "price": to_float(latest.get("当日收盘价")),
            "change_pct": to_float(latest.get("当日涨跌幅")),
            "source": "akshare.stock_value_em",
        }

    async def _quote_from_bid_ask(self, code: str) -> Dict[str, Any]:
        try:
            table = await _akshare_call("stock_bid_ask_em", retries=0, symbol=code)
        except Exception:
            return {}
        mapping = _info_map(table)
        price = to_float(mapping.get("最新"))
        if price is None:
            return {}
        return {
            "stock_code": code,
            "price": price,
            "open": to_float(mapping.get("今开")),
            "high": to_float(mapping.get("最高")),
            "low": to_float(mapping.get("最低")),
            "pre_close": to_float(mapping.get("昨收")),
            "change_pct": to_float(mapping.get("涨幅")),
            "volume": to_float(mapping.get("总手")),
            "turnover": to_float(mapping.get("金额")),
            "source": "akshare.stock_bid_ask_em",
        }

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
        return _parse_hist_rows(table)

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
        return _parse_hist_rows(table)

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
        latest = latest_record(rows)
        pe_values = [to_float(row.get("PE(TTM)")) for row in rows]
        pb_values = [to_float(row.get("市净率")) for row in rows]
        pe = to_float(latest.get("PE(TTM)"))
        pb = to_float(latest.get("市净率"))
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
        table = await _akshare_call_first(
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
        rows = _records(table)
        latest = latest_record(rows)
        payload = {
            "stock_code": code,
            "roe": _first_number(
                latest,
                ("加权净资产收益率(%)", "净资产收益率(%)", "净资产收益率", "ROEJQ", "roe"),
            ),
            "revenue_yoy": _first_number(
                latest,
                (
                    "营业总收入同比增长率",
                    "主营业务收入增长率(%)",
                    "TOTALOPERATEREVETZ",
                    "revenue_yoy",
                    "营收同比",
                ),
            ),
            "profit_yoy": _first_number(
                latest,
                (
                    "净利润同比增长率",
                    "净利润增长率(%)",
                    "PARENTNETPROFITTZ",
                    "netprofit_yoy",
                    "净利润同比",
                ),
            ),
            "gross_margin": _first_number(
                latest, ("销售毛利率(%)", "销售毛利率", "XSMLL", "毛利率", "grossprofit_margin")
            ),
            "debt_ratio": _first_number(
                latest, ("资产负债率(%)", "资产负债率", "ZCFZL", "debt_asset_ratio")
            ),
            "current_ratio": _first_number(latest, ("流动比率", "LD", "current_ratio")),
            "net_profit": _first_number(
                latest,
                (
                    "净利润",
                    "PARENTNETPROFIT",
                    "扣除非经常性损益后的净利润(元)",
                    "netprofit",
                ),
            ),
            "goodwill": _first_number(latest, ("商誉", "goodwill")),
            "non_recurring_profit_ratio": _first_number(
                latest, ("非经常性损益占比", "non_recurring_profit_ratio")
            ),
            "source": "akshare.financials",
        }
        return payload

    async def cashflow(self, stock_code: str) -> Dict[str, Any]:
        code = normalize_stock_code(stock_code)
        financials = await self.financials(code)
        table = await _akshare_call_first(
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
        rows = _records(table)
        latest = latest_record(rows)
        operating = _first_number(
            latest,
            (
                "经营活动产生的现金流量净额",
                "NETCASH_OPERATE",
                "经营现金流",
                "operating_cf",
            ),
        )
        net_profit = _first_number(latest, ("净利润", "NETPROFIT", "net_profit")) or financials.get(
            "net_profit"
        )
        return {
            "stock_code": code,
            "operating_cf": operating,
            "net_profit": net_profit,
            "profit_yoy": financials.get("profit_yoy"),
            "source": "akshare.cashflow",
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
        rows = newest_records(_records(table), max(limit * 3, 30))
        hits = []
        for row in rows:
            title = _text(
                row.get("公告标题")
                or row.get("标题")
                or row.get("title")
                or row.get("新闻标题")
            )
            if not title:
                continue
            hits.append(
                {
                    "title": title,
                    "published_at": normalize_date(
                        row.get("公告日期") or row.get("日期") or row.get("发布时间")
                    ),
                    "notice_type": _text(row.get("公告类型") or row.get("类型")),
                    "url": _text(row.get("网址") or row.get("url") or row.get("链接")),
                    "source": "akshare.announcement",
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
        rows = newest_records(_records(table), limit)
        hits = []
        for row in rows:
            title = _text(
                row.get("新闻标题") or row.get("标题") or row.get("title")
            )
            if not title:
                continue
            hits.append(
                {
                    "title": title,
                    "published_at": _text(
                        row.get("发布时间") or row.get("日期") or row.get("时间")
                    ),
                    "url": _text(row.get("新闻链接") or row.get("url") or row.get("链接")),
                    "source": _text(row.get("文章来源") or row.get("来源"))
                    or "akshare.stock_news_em",
                    "summary": _text(row.get("新闻内容") or row.get("内容")),
                }
            )
        return hits

    async def holder_changes(
        self, stock_code: str, *, limit: int = 20
    ) -> List[Dict[str, Any]]:
        code = normalize_stock_code(stock_code)
        table = await _akshare_call_first(
            (
                ("stock_shareholder_change_ths", {"symbol": code}),
                ("stock_share_hold_change_szse", {"symbol": code}),
                ("stock_hold_change_cninfo", {"symbol": code}),
            )
        )
        rows = newest_records(_records(table), limit)
        items = []
        for row in rows:
            note = _text(
                row.get("变动原因")
                or row.get("增减")
                or row.get("变动类型")
                or row.get("公告标题")
                or row.get("变动途径")
            )
            name = _text(
                row.get("股东名称")
                or row.get("变动股东")
                or row.get("持股人")
                or row.get("姓名")
            )
            qty_text = _text(row.get("变动数量") or row.get("增减数量"))
            change_shares = _first_number(
                row, ("变动数量", "变动股数", "增减数量", "change_shares")
            )
            change_type = classify_event(" ".join(part for part in (qty_text, note, name) if part))
            if change_type == "announcement":
                if qty_text and "减持" in qty_text:
                    change_type = "reduction"
                elif qty_text and "增持" in qty_text:
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
                    "published_at": _text(
                        row.get("变动日期") or row.get("公告日期") or row.get("日期")
                    ),
                    "note": note,
                }
            )
        return items

    async def macro(self) -> Dict[str, Any]:
        lpr = _records(await _akshare_call("macro_china_lpr"))
        latest = latest_record(lpr)
        shibor = _records(await _akshare_call_optional("macro_china_shibor_all"))
        shibor_latest = latest_record(shibor) if shibor else {}
        return {
            "lpr_1y": _first_number(latest, ("LPR1Y", "1年期LPR", "lpr_1y", "LPR_1Y")),
            "lpr_5y": _first_number(latest, ("LPR5Y", "5年期以上LPR", "lpr_5y")),
            "shibor_overnight": _first_number(
                shibor_latest, ("Overnight_O/N_定价", "O/N", "overnight")
            ),
            "source": "akshare.macro",
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
            "roe": to_float(raw.get("roe")),
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
            digest = hashlib.sha256(str(title).encode("utf-8")).hexdigest()[:12]
            snapshot.events.append(
                EventItem(
                    event_id="{}-holder-{}".format(code, digest),
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
        return {
            "stock_code": code,
            "company_name": profile.company_name,
            "price": quote.price,
            "pe": valuation.pe,
            "pb": valuation.pb,
            "pe_percentile_5y": valuation.pe_percentile_5y,
            "pb_percentile_5y": valuation.pb_percentile_5y,
            "roe": financials.roe,
            "revenue_yoy": financials.revenue_yoy,
            "profit_yoy": financials.profit_yoy or cashflow.profit_yoy,
            "gross_margin": financials.gross_margin,
            "debt_ratio": financials.debt_ratio,
            "current_ratio": financials.current_ratio,
            "operating_cf": cashflow.operating_cf,
            "net_profit": cashflow.net_profit or financials.net_profit,
            "goodwill": financials.goodwill,
            "non_recurring_profit_ratio": financials.non_recurring_profit_ratio,
        }


def synthetic_market_fixture(stock_code: str) -> Dict[str, Any]:
    code = normalize_stock_code(stock_code)
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
            "company_name": "五粮液",
            "industry": "白酒",
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
                "published_at": "2026-06-01",
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
                "published_at": "2026-05-12",
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
                "title": "五粮液渠道库存去化进展报道",
                "published_at": "2026-06-02",
                "source": "fixture",
                "summary": "白酒渠道库存继续下降",
            }
        ],
        "holder_changes": [
            {
                "holder_name": "宜宾市国资",
                "change_type": "increase",
                "change_shares": 1200000,
                "published_at": "2026-06-08",
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
) -> Any:
    last_error = None
    ak = _import_akshare()
    for name, kwargs in candidates:
        if getattr(ak, name, None) is None:
            continue
        try:
            return await _akshare_call(name, retries=retries, **kwargs)
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


def _parse_hist_rows(table: Any) -> List[Dict[str, Any]]:
    bars = []
    for row in _records(table):
        close = to_float(
            row.get("收盘") or row.get("close") or row.get("收盘价")
        )
        if close is None:
            continue
        bars.append(
            {
                "date": _text(row.get("日期") or row.get("date") or row.get("时间")),
                "open": to_float(row.get("开盘") or row.get("open")),
                "close": close,
                "high": to_float(row.get("最高") or row.get("high")),
                "low": to_float(row.get("最低") or row.get("low")),
                "volume": to_float(row.get("成交量") or row.get("volume")),
                "turnover": to_float(row.get("成交额") or row.get("amount")),
            }
        )
    return bars


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
