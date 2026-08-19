import json
import sys
from pathlib import Path

import pytest

from insightagent.business_contracts import Report
from insightagent.contracts import LLMResponse, LLMToolCall
from insightagent.decision import build_p0_decision
from insightagent.evidence import EvidenceBindingError, bind_report_evidence
from insightagent.fundamental_agent import default_fixtures_dir
from insightagent.fundamentals import (
    FixtureFundamentalAdapter,
    apply_fundamental_rules,
)
from insightagent.persistence import SQLiteDatabase
from insightagent.research_store import ResearchStore
from insightagent.workflows.initial_research import analyze_stock, format_cli_text

FIXTURES = default_fixtures_dir()


class ScriptedFundamentalLLM:
    def __init__(self, *, unbound: bool = False) -> None:
        self.unbound = unbound
        self.phase = "tool"
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if self.phase == "tool":
            self.phase = "final"
            return LLMResponse(
                id="tool-1",
                model="fake",
                content="reading snapshot",
                reasoning_content="need snapshot first",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="get_fundamental_snapshot",
                        arguments="{}",
                    )
                ],
                finish_reason="tool_calls",
            )

        snapshot = _snapshot_from_request(request)
        report = _scripted_report(snapshot, unbound=self.unbound)
        payload = {
            "status": "abstained" if report["abstain"] else "completed",
            "output": {"report": report},
            "reflection": {"what_worked": ["used snapshot tool"]},
            "state_patch": {
                "set": {},
                "append": {},
                "remove": {},
            },
        }
        self.phase = "done"
        return LLMResponse(
            id="final-1",
            model="fake",
            content=json.dumps(payload, ensure_ascii=False),
            reasoning_content="conclude from snapshot",
            finish_reason="stop",
        )


def _snapshot_from_request(request) -> dict:
    for message in reversed(request.messages):
        if message.role != "tool" or not message.content:
            continue
        payload = json.loads(message.content)
        data = payload.get("data") or payload
        if isinstance(data, dict) and "stock_code" in data:
            return data
    raise AssertionError("Scripted LLM did not receive a snapshot")


def _scripted_report(snapshot: dict, *, unbound: bool) -> dict:
    flags = snapshot.get("computed_flags") or []
    citations = [
        {
            "ref_id": "roe",
            "kind": "field",
            "id": "roe",
            "source": "fundamental_snapshot",
            "note": "ROE from snapshot",
        }
    ]
    if "cashflow_lag" in flags:
        citations.append(
            {
                "ref_id": "cf",
                "kind": "rule",
                "id": "cashflow_lag",
                "source": "fundamental_snapshot",
                "note": "profit grew while operating cash flow did not",
            }
        )
    summary = (
        "ROE {roe}，PE {pe}，经营现金流 {cf}，净利润同比 {profit_yoy}。"
    ).format(
        roe=snapshot.get("roe"),
        pe=snapshot.get("pe"),
        cf=snapshot.get("operating_cf"),
        profit_yoy=snapshot.get("profit_yoy"),
    )
    if unbound:
        summary += " PE 999.9 被写成极低估值。"
    return {
        "schema_version": "1",
        "role": "fundamental",
        "score": 3,
        "stance": "hold",
        "summary": summary,
        "citations": citations,
        "risks": ["盈利质量需观察经营现金流", "估值分位仍需对照历史"],
        "degraded": False,
        "abstain": False,
        "missing_information": [],
        "valuation": "PE {pe}，五年分位 {pct}。".format(
            pe=snapshot.get("pe"),
            pct=snapshot.get("pe_percentile_5y"),
        ),
        "financial_health": "资产负债率 {debt}。".format(
            debt=snapshot.get("debt_ratio")
        ),
        "earnings_quality": "净利润 {profit}，经营现金流 {cf}。".format(
            profit=snapshot.get("net_profit"),
            cf=snapshot.get("operating_cf"),
        ),
    }


async def _analyze(tmp_path: Path, stock_code: str, llm, **kwargs):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    await database.initialize()
    return await analyze_stock(
        stock_code,
        database=database,
        llm_adapter=llm,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        fixtures_dir=str(FIXTURES),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_fixture_stock_persists_legal_report_and_decision(tmp_path):
    outcome = await _analyze(tmp_path, "000858", ScriptedFundamentalLLM())
    assert outcome.error is None
    assert outcome.report is not None
    assert outcome.decision is not None
    assert outcome.run.status in {"success", "degraded"}
    assert outcome.decision.timing_score is None
    assert outcome.decision.confidence <= 0.65
    assert "technical" in outcome.decision.dimensions_missing
    assert "sentiment" in outcome.decision.dimensions_missing
    assert "macro" in outcome.decision.dimensions_missing

    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    stored = await ResearchStore(database).get_run(outcome.run.run_id)
    assert stored is not None
    assert stored["reports"][0]["stance"] == outcome.report.stance
    assert stored["decisions"][0]["rating"] == outcome.decision.rating
    text = format_cli_text(outcome)
    assert "非投资建议" in text
    assert "未评估" in text


@pytest.mark.asyncio
async def test_missing_financials_abstain_without_llm(tmp_path):
    llm = ScriptedFundamentalLLM()
    outcome = await _analyze(tmp_path, "999999", llm)
    assert outcome.report is not None
    assert outcome.report.abstain is True
    assert outcome.report.stance == "abstain"
    assert outcome.decision is not None
    assert outcome.decision.rating == "abstain"
    assert outcome.decision.timing_score is None
    assert outcome.decision.confidence <= 0.65
    assert llm.requests == []
    assert outcome.run.status == "degraded"


@pytest.mark.asyncio
async def test_cashflow_lag_is_flagged_and_cited(tmp_path):
    adapter = FixtureFundamentalAdapter.from_directory(FIXTURES)
    snapshot = await adapter.fetch_fundamental("000858")
    assert "cashflow_lag" in snapshot.computed_flags

    outcome = await _analyze(tmp_path, "000858", ScriptedFundamentalLLM())
    cited = [
        citation
        for citation in outcome.report.citations
        if citation.kind == "rule" and citation.id == "cashflow_lag"
    ]
    assert cited


@pytest.mark.asyncio
async def test_unbound_number_fails_the_run(tmp_path):
    outcome = await _analyze(
        tmp_path,
        "000858",
        ScriptedFundamentalLLM(unbound=True),
        unbound_policy="fail",
    )
    assert outcome.run.status == "failed"
    assert outcome.decision is None
    assert "EvidenceBindingError" in (outcome.error or "")


@pytest.mark.asyncio
async def test_decision_timing_and_confidence_rules():
    adapter = FixtureFundamentalAdapter.from_directory(FIXTURES)
    snapshot = apply_fundamental_rules(
        await adapter.fetch_fundamental("000858")
    )
    report = Report.model_validate(
        _scripted_report(snapshot.model_dump(mode="json"), unbound=False)
    )
    bind_report_evidence(report, snapshot)
    decision = build_p0_decision(report, snapshot)
    assert decision.timing_score is None
    assert decision.confidence <= 0.65
    assert decision.dimensions_used == ["fundamental"]
    assert set(decision.dimensions_missing) >= {
        "technical",
        "sentiment",
        "macro",
    }


@pytest.mark.asyncio
async def test_fixture_path_does_not_import_akshare(tmp_path):
    sys.modules.pop("akshare", None)
    await _analyze(tmp_path, "000858", ScriptedFundamentalLLM())
    assert "akshare" not in sys.modules


@pytest.mark.asyncio
async def test_unbound_summary_is_rejected():
    adapter = FixtureFundamentalAdapter.from_directory(FIXTURES)
    snapshot = await adapter.fetch_fundamental("000858")
    report = Report.model_validate(
        _scripted_report(snapshot.model_dump(mode="json"), unbound=True)
    )
    with pytest.raises(EvidenceBindingError):
        bind_report_evidence(report, snapshot)
