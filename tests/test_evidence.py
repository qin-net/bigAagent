import pytest

from insightagent.business_contracts import Report
from insightagent.evidence import (
    EvidenceBindingError,
    bind_report_evidence,
    extract_candidate_numbers,
    number_is_allowed,
)
from insightagent.fundamental_agent import default_fixtures_dir
from insightagent.fundamentals import FixtureFundamentalAdapter, apply_fundamental_rules


def test_extract_skips_iso_and_chinese_dates():
    text = (
        "截至 2026-08-19，PE 22.14548383；"
        "对照 2026/8/19 与 2026年8月19日；"
        "月份口径 2026-08。"
    )
    assert extract_candidate_numbers(text, "000858") == [22.14548383]


def test_extract_skips_iso_datetime():
    text = "as_of 2026-08-19T16:18:00+08:00 经营现金流 -25.35"
    assert extract_candidate_numbers(text, "000858") == [-25.35]


def test_rounded_pe_is_not_allowed():
    assert not number_is_allowed(22.0, [22.14548383])
    assert not number_is_allowed(22.15, [22.14548383])
    assert number_is_allowed(22.14548383, [22.14548383])


@pytest.mark.asyncio
async def test_bind_accepts_date_with_exact_snapshot_numbers():
    adapter = FixtureFundamentalAdapter.from_directory(default_fixtures_dir())
    snapshot = apply_fundamental_rules(await adapter.fetch_fundamental("000858"))
    report = Report.model_validate(
        {
            "schema_version": "1",
            "role": "fundamental",
            "score": 3,
            "stance": "hold",
            "summary": (
                "截至 2026-08-19，PE {pe}，经营现金流 {cf}。"
            ).format(pe=snapshot.pe, cf=snapshot.operating_cf),
            "citations": [
                {
                    "ref_id": "pe",
                    "kind": "field",
                    "id": "pe",
                    "source": "fundamental_snapshot",
                },
                {
                    "ref_id": "cf",
                    "kind": "rule",
                    "id": "cashflow_lag",
                    "source": "fundamental_snapshot",
                },
            ],
            "risks": ["盈利质量需观察经营现金流", "估值分位仍需对照历史"],
            "degraded": False,
            "abstain": False,
            "missing_information": [],
            "valuation": "PE {pe}。".format(pe=snapshot.pe),
            "financial_health": "资产负债率 {debt}。".format(debt=snapshot.debt_ratio),
            "earnings_quality": "净利润 {profit}。".format(profit=snapshot.net_profit),
        }
    )
    bind_report_evidence(report, snapshot)


@pytest.mark.asyncio
async def test_bind_rejects_rounded_snapshot_number():
    adapter = FixtureFundamentalAdapter.from_directory(default_fixtures_dir())
    snapshot = apply_fundamental_rules(await adapter.fetch_fundamental("000858"))
    report = Report.model_validate(
        {
            "schema_version": "1",
            "role": "fundamental",
            "score": 3,
            "stance": "hold",
            "summary": "PE 19 被写成约数。",
            "citations": [
                {
                    "ref_id": "pe",
                    "kind": "field",
                    "id": "pe",
                    "source": "fundamental_snapshot",
                },
                {
                    "ref_id": "cf",
                    "kind": "rule",
                    "id": "cashflow_lag",
                    "source": "fundamental_snapshot",
                },
            ],
            "risks": ["盈利质量需观察经营现金流", "估值分位仍需对照历史"],
            "degraded": False,
            "abstain": False,
            "missing_information": [],
        }
    )
    with pytest.raises(EvidenceBindingError) as exc:
        bind_report_evidence(report, snapshot)
    assert 19.0 in exc.value.numbers
