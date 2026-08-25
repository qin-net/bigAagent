from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class QuoteInput:
    stock_code: str
    name: str
    industry: Optional[str] = None
    market: Optional[str] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None


@dataclass(frozen=True)
class CollectionResult:
    source: str
    quotes: list[QuoteInput]


@dataclass(frozen=True)
class DailyBar:
    stock_code: str
    trade_date: str
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]
    turnover: Optional[float]


@dataclass(frozen=True)
class NoticeHeadline:
    stock_code: str
    title: str
    published_at: Optional[str]
    url: Optional[str]
    source: str
