from __future__ import annotations

from typing import Any, Dict, List, Optional

from .data_contracts import IndicatorSnapshot, KlineSnapshot, PriceSnapshot


def apply_technical_rules(
    indicator: IndicatorSnapshot,
    price: PriceSnapshot,
    kline: KlineSnapshot,
) -> Dict[str, Any]:
    flags: List[str] = []

    if indicator.bars_used < 20 or indicator.ma20 is None:
        flags.append("insufficient_bars")
        return {
            "flags": flags,
            "trend": None,
            "setup": None,
            "key_levels": None,
            "price_vs_ma20": None,
        }

    ma5 = indicator.ma5
    ma10 = indicator.ma10
    ma20 = indicator.ma20
    ma60 = indicator.ma60

    if all(v is not None for v in (ma5, ma10, ma20)) and ma5 > ma10 > ma20:
        if ma60 is None or ma20 > ma60:
            flags.append("ma_bull_align")
    elif all(v is not None for v in (ma5, ma10, ma20)) and ma5 < ma10 < ma20:
        if ma60 is None or ma20 < ma60:
            flags.append("ma_bear_align")

    if indicator.macd is not None and indicator.macd_signal is not None:
        if indicator.macd > indicator.macd_signal:
            flags.append("macd_pos")
        else:
            flags.append("macd_neg")

    if indicator.rsi14 is not None:
        if indicator.rsi14 >= 70:
            flags.append("rsi_overbought")
        elif indicator.rsi14 <= 30:
            flags.append("rsi_oversold")

    if indicator.volume_ratio is not None and indicator.volume_ratio >= 2.0:
        flags.append("volume_spike")

    price_vs_ma20 = None
    if price.price is not None and ma20 is not None:
        price_vs_ma20 = (price.price - ma20) / ma20

    key_levels = _build_key_levels(kline, indicator)
    trend = _describe_trend(flags)
    setup = _describe_setup(flags)

    return {
        "flags": flags,
        "trend": trend,
        "setup": setup,
        "key_levels": key_levels,
        "price_vs_ma20": price_vs_ma20,
    }


def _build_key_levels(kline: KlineSnapshot, indicator: IndicatorSnapshot) -> str:
    highs = [b.high for b in kline.bars if b.high is not None]
    lows = [b.low for b in kline.bars if b.low is not None]
    parts: List[str] = []
    if highs:
        parts.append("近期高点 {:.2f}".format(max(highs[-20:])))
    if lows:
        parts.append("近期低点 {:.2f}".format(min(lows[-20:])))
    if indicator.ma20 is not None:
        parts.append("MA20 {:.2f}".format(indicator.ma20))
    return "；".join(parts) if parts else ""


def _describe_trend(flags: List[str]) -> Optional[str]:
    if "insufficient_bars" in flags:
        return None
    if "ma_bull_align" in flags:
        return "均线多头排列"
    if "ma_bear_align" in flags:
        return "均线空头排列"
    return "均线纠缠，趋势不明"


def _describe_setup(flags: List[str]) -> Optional[str]:
    if "insufficient_bars" in flags:
        return None
    parts: List[str] = []
    if "macd_pos" in flags:
        parts.append("MACD 零轴上方")
    elif "macd_neg" in flags:
        parts.append("MACD 零轴下方")
    if "rsi_overbought" in flags:
        parts.append("RSI 超买")
    elif "rsi_oversold" in flags:
        parts.append("RSI 超卖")
    if "volume_spike" in flags:
        parts.append("放量")
    return "；".join(parts) if parts else "无明显结构信号"
