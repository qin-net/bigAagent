from __future__ import annotations

import pytest

from insightagent.business_contracts import FundamentalSnapshot, Report
from insightagent.data_contracts import IndicatorSnapshot, KlineSnapshot, PriceSnapshot
from insightagent.evidence import EvidenceBindingError, bind_report_evidence
from insightagent.fundamental_agent import default_fixtures_dir
from insightagent.fundamentals import FixtureFundamentalAdapter, apply_fundamental_rules
from insightagent.market import cashflow_yoy_pct, report_period_from_date, rsi
from insightagent.technicals import apply_technical_rules


def _fundamental(**overrides):
    payload = {
        "stock_code": "000858",
        "pe": 18.5,
        "pb": 4.2,
        "pe_percentile_5y": 45.0,
        "roe": 16.8,
        "debt_ratio": 32.1,
        "operating_cf": 10.0,
        "net_profit": 80.0,
        "profit_yoy": 8.5,
    }
    payload.update(overrides)
    return apply_fundamental_rules(FundamentalSnapshot.model_validate(payload))


def _report(snapshot: FundamentalSnapshot, extra_citations=None) -> Report:
    citations = [
        {
            "ref_id": "roe",
            "kind": "field",
            "id": "roe",
            "source": "fundamental_snapshot",
        }
    ]
    if "cashflow_lag" in snapshot.computed_flags:
        citations.append(
            {
                "ref_id": "cf",
                "kind": "rule",
                "id": "cashflow_lag",
                "source": "fundamental_snapshot",
            }
        )
    citations.extend(extra_citations or [])
    return Report.model_validate(
        {
            "schema_version": "1",
            "role": "fundamental",
            "score": 3,
            "stance": "hold",
            "summary": "ROE {roe} PE {pe}".format(roe=snapshot.roe, pe=snapshot.pe),
            "citations": citations,
            "risks": ["经营现金流"],
            "degraded": False,
            "abstain": False,
            "missing_information": [],
        }
    )


def test_wilder_rsi_uses_full_history_not_last_window_sma():
    prices = list(range(1, 16)) + [14]
    expected = 100.0 - (100.0 / 14.0)
    assert abs(rsi(prices, 14) - expected) < 1e-9

    declining = [100.0 - index for index in range(30)]
    mixed_tail = [
        1.0,
        -0.6,
        1.4,
        -0.4,
        0.9,
        -1.1,
        1.6,
        -0.3,
        0.5,
        -0.8,
        1.2,
        -0.5,
        0.7,
        0.2,
    ]
    rebound = declining[:]
    for delta in mixed_tail:
        rebound.append(rebound[-1] + delta)
    wilder = rsi(rebound, 14)
    last_window_gains = 0.0
    last_window_losses = 0.0
    for index in range(-14, 0):
        delta = rebound[index] - rebound[index - 1]
        if delta >= 0:
            last_window_gains += delta
        else:
            last_window_losses -= delta
    sma_style = 100.0 - (
        100.0
        / (1.0 + (last_window_gains / 14.0) / (last_window_losses / 14.0))
    )
    assert last_window_losses > 0
    assert abs(wilder - sma_style) > 1.0


def test_report_period_and_cashflow_yoy():
    assert report_period_from_date("2026-06-30") == "h1"
    assert report_period_from_date("2025-12-31") == "fy"
    yoy = cashflow_yoy_pct(
        [
            {"date": "2026-06-30", "operating_cf": -10.0},
            {"date": "2025-06-30", "operating_cf": -20.0},
        ]
    )
    assert yoy == 50.0


def test_single_period_h1_roe_is_not_long_term_fail():
    snapshot = _fundamental(
        roe=6.5,
        roe_stable=None,
        roe_report_period="h1",
        roe_series=[],
    )
    assert "roe_quality" not in snapshot.computed_flags
    assert "roe_insufficient_history" in snapshot.computed_flags
    detail = next(hit.detail for hit in snapshot.rule_hits if hit.rule_id == "roe_quality")
    assert "NOT enough" in detail
    assert "annualized_roe=13.0" in detail
    assert "period=h1" in detail


@pytest.mark.asyncio
async def test_fixture_000858_still_hits_stable_roe_quality():
    adapter = FixtureFundamentalAdapter.from_directory(default_fixtures_dir())
    snapshot = await adapter.fetch_fundamental("000858")
    assert "roe_quality" in snapshot.computed_flags
    assert "roe_insufficient_history" not in snapshot.computed_flags
    assert "cashflow_lag" in snapshot.computed_flags


def test_multi_year_roe_layers():
    passed = _fundamental(roe=6.5, roe_series=[16.0, 15.0, 17.0], debt_ratio=40.0)
    assert "roe_quality" in passed.computed_flags
    assert "roe_insufficient_history" not in passed.computed_flags

    failed = _fundamental(roe=16.0, roe_series=[16.0, 22.0, 10.0], debt_ratio=40.0)
    assert "roe_quality" not in failed.computed_flags
    assert "roe_insufficient_history" not in failed.computed_flags


def test_cashflow_attribution_and_value_trap():
    trap = _fundamental(
        pe=10.0,
        pe_percentile_5y=20.0,
        operating_cf=-12.4,
        profit_yoy=8.5,
    )
    assert "cashflow_lag" in trap.computed_flags
    assert "value_trap_risk" in trap.computed_flags
    assert "cashflow_seasonal" not in trap.computed_flags

    seasonal = _fundamental(
        pe=10.0,
        pe_percentile_5y=20.0,
        operating_cf=-12.4,
        profit_yoy=8.5,
        cashflow_yoy=12.0,
    )
    assert "cashflow_seasonal" in seasonal.computed_flags
    assert "value_trap_risk" not in seasonal.computed_flags

    quality = _fundamental(
        operating_cf=-12.4,
        profit_yoy=8.5,
        ocf_to_np=0.2,
    )
    assert "cashflow_quality_issue" in quality.computed_flags

    no_lag = _fundamental(operating_cf=20.0, profit_yoy=8.5, pe_percentile_5y=20.0)
    assert "cashflow_lag" not in no_lag.computed_flags
    assert "value_trap_risk" not in no_lag.computed_flags
    assert "cashflow_seasonal" not in no_lag.computed_flags


def test_value_trap_requires_rule_citation():
    snapshot = _fundamental(
        pe=10.0,
        pe_percentile_5y=20.0,
        operating_cf=-12.4,
        profit_yoy=8.5,
    )
    with pytest.raises(EvidenceBindingError):
        bind_report_evidence(_report(snapshot), snapshot)
    bind_report_evidence(
        _report(
            snapshot,
            extra_citations=[
                {
                    "ref_id": "trap",
                    "kind": "rule",
                    "id": "value_trap_risk",
                    "source": "fundamental_snapshot",
                }
            ],
        ),
        snapshot,
    )


def _empty_kline():
    return KlineSnapshot(stock_code="000858", bars=[], bars_used=60, last_close=100.0)


def test_ma_bear_align_requires_four_strict_lines():
    weak = apply_technical_rules(
        IndicatorSnapshot(
            stock_code="000858",
            ma5=90.0,
            ma10=95.0,
            ma20=100.0,
            ma60=80.0,
            bars_used=60,
        ),
        PriceSnapshot(stock_code="000858", price=88.0),
        _empty_kline(),
    )
    assert "ma_bear_align" not in weak["flags"]
    assert "走弱" in (weak["trend"] or "")
    assert "空头排列" not in (weak["trend"] or "")

    bear = apply_technical_rules(
        IndicatorSnapshot(
            stock_code="000858",
            ma5=80.0,
            ma10=90.0,
            ma20=100.0,
            ma60=110.0,
            bars_used=60,
        ),
        PriceSnapshot(stock_code="000858", price=78.0),
        _empty_kline(),
    )
    assert "ma_bear_align" in bear["flags"]
    assert "空头排列" in (bear["trend"] or "")

    missing_ma60 = apply_technical_rules(
        IndicatorSnapshot(
            stock_code="000858",
            ma5=80.0,
            ma10=90.0,
            ma20=100.0,
            ma60=None,
            bars_used=60,
        ),
        PriceSnapshot(stock_code="000858", price=78.0),
        _empty_kline(),
    )
    assert "ma_bear_align" not in missing_ma60["flags"]
    assert "空头排列" not in (missing_ma60["trend"] or "")
