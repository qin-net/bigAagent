from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .contracts import utc_now


class StockCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dummy: str = Field(
        default="",
        description="Unused placeholder. Send an empty string.",
    )


class SearchAnnouncementsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str
    query: str = ""
    limit: int = Field(default=10, ge=1, le=50)


class SearchNewsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str
    query: str = ""
    limit: int = Field(default=10, ge=1, le=50)


class KlineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str
    limit: int = Field(default=30, ge=5, le=120)


class SnapshotDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: Dict[str, Any]
    current: Dict[str, Any]


class StockProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    company_name: Optional[str] = None
    industry: Optional[str] = None
    listing_date: Optional[str] = None
    source: str = "unknown"
    as_of: datetime = Field(default_factory=utc_now)
    missing_fields: List[str] = Field(default_factory=list)


class PriceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    price: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    pre_close: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    source: str = "unknown"
    missing_fields: List[str] = Field(default_factory=list)


class IndicatorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    rsi14: Optional[float] = None
    volume_ratio: Optional[float] = None
    bars_used: int = 0
    source: str = "computed"
    missing_fields: List[str] = Field(default_factory=list)


class ValuationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    pe: Optional[float] = None
    pb: Optional[float] = None
    pe_percentile_5y: Optional[float] = None
    pb_percentile_5y: Optional[float] = None
    source: str = "unknown"
    missing_fields: List[str] = Field(default_factory=list)


class FinancialSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    roe: Optional[float] = None
    revenue_yoy: Optional[float] = None
    profit_yoy: Optional[float] = None
    gross_margin: Optional[float] = None
    debt_ratio: Optional[float] = None
    current_ratio: Optional[float] = None
    net_profit: Optional[float] = None
    goodwill: Optional[float] = None
    non_recurring_profit_ratio: Optional[float] = None
    source: str = "unknown"
    missing_fields: List[str] = Field(default_factory=list)


class CashflowSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    operating_cf: Optional[float] = None
    net_profit: Optional[float] = None
    profit_yoy: Optional[float] = None
    source: str = "unknown"
    missing_fields: List[str] = Field(default_factory=list)


class EventItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    title: str
    published_at: Optional[str] = None
    source: str = "unknown"
    url: Optional[str] = None
    summary: Optional[str] = None


class EventSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    events: List[EventItem] = Field(default_factory=list)
    source: str = "unknown"


class MacroSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    as_of: datetime = Field(default_factory=utc_now)
    lpr_1y: Optional[float] = None
    lpr_5y: Optional[float] = None
    shibor_overnight: Optional[float] = None
    notes: List[str] = Field(default_factory=list)
    source: str = "unknown"
    missing_fields: List[str] = Field(default_factory=list)


class AnnouncementHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    published_at: Optional[str] = None
    notice_type: Optional[str] = None
    url: Optional[str] = None
    source: str = "unknown"


class AnnouncementSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    query: str
    hits: List[AnnouncementHit] = Field(default_factory=list)


class NewsHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    published_at: Optional[str] = None
    source: str = "unknown"
    url: Optional[str] = None
    summary: Optional[str] = None


class NewsSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    query: str
    hits: List[NewsHit] = Field(default_factory=list)


class HolderChangeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holder_name: Optional[str] = None
    change_type: Optional[str] = None
    change_shares: Optional[float] = None
    published_at: Optional[str] = None
    note: Optional[str] = None


class HolderChangeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    items: List[HolderChangeItem] = Field(default_factory=list)
    source: str = "unknown"


class KlineBar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: Optional[str] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None


class KlineSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    stock_code: str
    as_of: datetime = Field(default_factory=utc_now)
    source: str = "unknown"
    bars_used: int = 0
    last_close: Optional[float] = None
    bars: List[KlineBar] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)


class FieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    before: Any = None
    after: Any = None


class SnapshotDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    changed_fields: List[FieldChange] = Field(default_factory=list)
    unchanged_count: int = 0
