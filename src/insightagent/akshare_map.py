"""Map AKShare vendor tables onto InsightAgent canonical fields.

AKShare does not unify East Money, Tencent, THS, or Sina schemas.
Callers must map columns and convert units before snapshots reach an Agent.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

Unit = str

# Snapshot fields use these units. Agents never see vendor column names.
CANONICAL_UNITS: Dict[str, Unit] = {
    "date": "date",
    "price": "yuan",
    "open": "yuan",
    "high": "yuan",
    "low": "yuan",
    "close": "yuan",
    "pre_close": "yuan",
    "change_pct": "percent",
    "volume": "lot",
    "turnover": "yuan",
    "pe": "ratio",
    "pb": "ratio",
    "roe": "percent",
    "revenue_yoy": "percent",
    "profit_yoy": "percent",
    "gross_margin": "percent",
    "debt_ratio": "percent",
    "current_ratio": "ratio",
    "net_profit": "yi_yuan",
    "operating_cf": "yi_yuan",
    "goodwill": "yi_yuan",
    "change_shares": "share",
    "lpr_1y": "percent",
    "lpr_5y": "percent",
}

PREFIX_RE = re.compile(r"^(增持|减持|持股)")
UNIT_SUFFIXES: Tuple[Tuple[str, Unit, float], ...] = (
    ("亿元", "yi_yuan", 1.0),
    ("万手", "lot", 10_000.0),
    ("万股", "share", 10_000.0),
    ("亿", "yi_yuan", 1.0),
    ("万", "wan", 1.0),
    ("手", "lot", 1.0),
    ("股", "share", 1.0),
    ("元", "yuan", 1.0),
)
TEXT_FIELDS = {
    "title",
    "notice_type",
    "url",
    "source",
    "summary",
    "holder_name",
    "change_type",
    "note",
    "company_name",
    "industry",
    "raw_change",
}
DATE_FIELDS = {"date", "listing_date"}


def parse_quantity(value: Any) -> Tuple[Optional[float], Optional[Unit]]:
    """Return (magnitude, unit). Suffix 亿/万/% names the unit; do not convert yet."""
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number:
            return None, None
        return number, None
    text = str(value).strip().replace(",", "")
    text = PREFIX_RE.sub("", text).strip()
    unit: Optional[Unit] = None
    multiplier = 1.0
    if text.endswith("%"):
        text = text[:-1].strip()
        unit = "percent"
    else:
        for suffix, suffix_unit, scale in UNIT_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                unit = suffix_unit
                multiplier = scale
                break
    if text in {"", "-", "None", "nan", "NaN", "False", "True"}:
        return None, None
    try:
        return float(text) * multiplier, unit
    except ValueError:
        return None, None


def convert_unit(
    value: Any, *, declared: Unit, canonical: Unit
) -> Optional[float]:
    number, detected = parse_quantity(value)
    if number is None:
        return None
    source_unit = detected or declared
    if source_unit == "wan":
        if canonical == "share":
            return number * 10_000.0
        if canonical == "lot":
            return number * 100.0
        if canonical == "yi_yuan":
            return round(number * 10_000.0 / 100_000_000.0, 4)
        if canonical == "yuan":
            return number * 10_000.0
        source_unit = "yuan"
    if source_unit == canonical:
        if canonical == "yi_yuan":
            return round(number, 4)
        return number
    if source_unit == "yuan" and canonical == "yi_yuan":
        return round(number / 100_000_000.0, 4)
    if source_unit == "yi_yuan" and canonical == "yuan":
        return number * 100_000_000.0
    if source_unit == "lot" and canonical == "share":
        return number * 100.0
    if source_unit == "share" and canonical == "lot":
        return number / 100.0
    return number


def _lookup(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            value = row[key]
            if isinstance(value, bool):
                continue
            if isinstance(value, float) and value != value:
                continue
            if str(value).strip() == "":
                continue
            return value
    lowered = {str(name).lower(): value for name, value in row.items()}
    for key in keys:
        if key.lower() in lowered:
            value = lowered[key.lower()]
            if value is not None and str(value).strip() != "":
                return value
    return None


def map_row(
    row: Mapping[str, Any],
    spec: Mapping[str, Tuple[Sequence[str], Unit]],
) -> Dict[str, Any]:
    mapped: Dict[str, Any] = {}
    for field, (keys, declared) in spec.items():
        raw = _lookup(row, keys)
        if raw is None:
            mapped[field] = None
            continue
        if declared == "date" or field in DATE_FIELDS:
            mapped[field] = _as_date(raw)
            continue
        if declared == "text" or field in TEXT_FIELDS:
            text = _as_text(raw)
            if field in {"company_name", "holder_name"} and text:
                text = re.sub(r"\s+", "", text)
            mapped[field] = text
            continue
        canonical = CANONICAL_UNITS.get(field) or declared
        mapped[field] = convert_unit(raw, declared=declared, canonical=canonical)
    return mapped


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_date(value: Any) -> Optional[str]:
    text = _as_text(value)
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return "{}-{}-{}".format(digits[:4], digits[4:6], digits[6:8])
    return text


def map_rows(
    rows: Iterable[Mapping[str, Any]],
    spec: Mapping[str, Tuple[Sequence[str], Unit]],
) -> List[Dict[str, Any]]:
    return [map_row(row, spec) for row in rows]


HIST_EASTMONEY: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("日期",), "date"),
    "open": (("开盘",), "yuan"),
    "close": (("收盘",), "yuan"),
    "high": (("最高",), "yuan"),
    "low": (("最低",), "yuan"),
    "volume": (("成交量",), "lot"),
    "turnover": (("成交额",), "yuan"),
}

# Tencent `turnover` is 换手率, not 成交额. Amount is the money field.
HIST_TENCENT: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("date",), "date"),
    "open": (("open",), "yuan"),
    "close": (("close",), "yuan"),
    "high": (("high",), "yuan"),
    "low": (("low",), "yuan"),
    "volume": (("volume",), "lot"),
    "turnover": (("amount",), "yuan"),
}

# Valuation series has close only; do not pretend OHLC or turnover exist.
HIST_VALUE_EM: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("数据日期",), "date"),
    "close": (("当日收盘价",), "yuan"),
    "volume": (("成交量",), "lot"),
}

QUOTE_BID_ASK_EM: Dict[str, Tuple[Sequence[str], Unit]] = {
    "price": (("最新",), "yuan"),
    "open": (("今开",), "yuan"),
    "high": (("最高",), "yuan"),
    "low": (("最低",), "yuan"),
    "pre_close": (("昨收",), "yuan"),
    "change_pct": (("涨幅",), "percent"),
    "volume": (("总手",), "lot"),
    "turnover": (("金额",), "yuan"),
}

VALUATION_EM: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("数据日期",), "date"),
    "price": (("当日收盘价",), "yuan"),
    "change_pct": (("当日涨跌幅",), "percent"),
    "pe": (("PE(TTM)",), "ratio"),
    "pb": (("市净率",), "ratio"),
}

THS_FINANCIAL_ABSTRACT: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("报告期",), "date"),
    "roe": (("净资产收益率",), "percent"),
    "revenue_yoy": (("营业总收入同比增长率",), "percent"),
    "profit_yoy": (("净利润同比增长率",), "percent"),
    "gross_margin": (("销售毛利率",), "percent"),
    "debt_ratio": (("资产负债率",), "percent"),
    "current_ratio": (("流动比率",), "ratio"),
    "net_profit": (("净利润",), "yi_yuan"),
}

SINA_FINANCIAL_INDICATOR: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("日期",), "date"),
    "roe": (("加权净资产收益率(%)", "净资产收益率(%)"), "percent"),
    "revenue_yoy": (("主营业务收入增长率(%)",), "percent"),
    "profit_yoy": (("净利润增长率(%)",), "percent"),
    "gross_margin": (("销售毛利率(%)",), "percent"),
    "debt_ratio": (("资产负债率(%)",), "percent"),
    "current_ratio": (("流动比率",), "ratio"),
    "net_profit": (("净利润",), "yuan"),
}

EM_FINANCIAL_INDICATOR: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("REPORT_DATE",), "date"),
    "roe": (("ROEJQ",), "percent"),
    "revenue_yoy": (("TOTALOPERATEREVETZ",), "percent"),
    "profit_yoy": (("PARENTNETPROFITTZ",), "percent"),
    "gross_margin": (("XSMLL",), "percent"),
    "debt_ratio": (("ZCFZL",), "percent"),
    "current_ratio": (("LD",), "ratio"),
    "net_profit": (("PARENTNETPROFIT",), "yuan"),
}

EM_CASHFLOW: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("REPORT_DATE",), "date"),
    "operating_cf": (("NETCASH_OPERATE",), "yuan"),
    "net_profit": (("NETPROFIT",), "yuan"),
}

SINA_CASHFLOW: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("报告日",), "date"),
    "operating_cf": (("经营活动产生的现金流量净额",), "yuan"),
    "net_profit": (("净利润",), "yuan"),
}

PROFILE_EM: Dict[str, Tuple[Sequence[str], Unit]] = {
    "company_name": (("股票简称", "名称", "证券简称"), "text"),
    "industry": (("行业",), "text"),
    "listing_date": (("上市时间", "上市日期"), "date"),
    "price": (("最新",), "yuan"),
}

NOTICE_EM: Dict[str, Tuple[Sequence[str], Unit]] = {
    "title": (("公告标题", "标题"), "text"),
    "published_at": (("公告日期", "日期"), "date"),
    "notice_type": (("公告类型", "类型"), "text"),
    "url": (("网址", "url", "链接"), "text"),
}

NEWS_EM: Dict[str, Tuple[Sequence[str], Unit]] = {
    "title": (("新闻标题", "标题"), "text"),
    "published_at": (("发布时间", "日期", "时间"), "text"),
    "url": (("新闻链接", "url", "链接"), "text"),
    "source": (("文章来源", "来源"), "text"),
    "summary": (("新闻内容", "内容"), "text"),
}

THS_HOLDER: Dict[str, Tuple[Sequence[str], Unit]] = {
    "holder_name": (("变动股东", "股东名称"), "text"),
    "change_shares": (("变动数量",), "share"),
    "published_at": (("公告日期", "变动日期"), "date"),
    "note": (("变动途径", "变动原因", "增减"), "text"),
    "raw_change": (("变动数量",), "text"),
}

MACRO_LPR: Dict[str, Tuple[Sequence[str], Unit]] = {
    "date": (("TRADE_DATE",), "date"),
    "lpr_1y": (("LPR1Y", "1年期LPR"), "percent"),
    "lpr_5y": (("LPR5Y", "5年期以上LPR"), "percent"),
}

FUNCTION_SPECS = {
    "stock_zh_a_hist": HIST_EASTMONEY,
    "stock_zh_a_hist_tx": HIST_TENCENT,
    "stock_value_em": VALUATION_EM,
    "stock_bid_ask_em": QUOTE_BID_ASK_EM,
    "stock_individual_info_em": PROFILE_EM,
    "stock_financial_abstract_ths": THS_FINANCIAL_ABSTRACT,
    "stock_financial_analysis_indicator": SINA_FINANCIAL_INDICATOR,
    "stock_financial_analysis_indicator_em": EM_FINANCIAL_INDICATOR,
    "stock_cash_flow_sheet_by_report_em": EM_CASHFLOW,
    "stock_financial_report_sina": SINA_CASHFLOW,
    "stock_individual_notice_report": NOTICE_EM,
    "stock_news_em": NEWS_EM,
    "stock_shareholder_change_ths": THS_HOLDER,
    "stock_share_hold_change_szse": THS_HOLDER,
    "stock_hold_change_cninfo": THS_HOLDER,
    "macro_china_lpr": MACRO_LPR,
}


def spec_for(akshare_function: str) -> Mapping[str, Tuple[Sequence[str], Unit]]:
    try:
        return FUNCTION_SPECS[akshare_function]
    except KeyError as error:
        raise KeyError("no field map for akshare function {}".format(akshare_function)) from error


def map_akshare_row(akshare_function: str, row: Mapping[str, Any]) -> Dict[str, Any]:
    return map_row(row, spec_for(akshare_function))


def map_akshare_rows(
    akshare_function: str, rows: Iterable[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    return [map_akshare_row(akshare_function, row) for row in rows]


def bars_from_mapped(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    for row in rows:
        close = row.get("close")
        if close is None:
            continue
        bars.append(
            {
                "date": row.get("date"),
                "open": row.get("open") if row.get("open") is not None else close,
                "high": row.get("high") if row.get("high") is not None else close,
                "low": row.get("low") if row.get("low") is not None else close,
                "close": close,
                "volume": row.get("volume"),
                "turnover": row.get("turnover"),
            }
        )
    return bars
