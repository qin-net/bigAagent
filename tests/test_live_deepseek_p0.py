import os

import pytest

from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.persistence import SQLiteDatabase
from insightagent.workflows.initial_research import analyze_stock


pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_deepseek_fixture_analysis(tmp_path):
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY is not set")

    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    await database.initialize()
    outcome = await analyze_stock(
        "000858",
        database=database,
        llm_adapter=DeepSeekChatAdapter(
            DeepSeekConfig(
                api_key=api_key,
                default_model="deepseek-v4-flash",
            )
        ),
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        model="deepseek-v4-flash",
        thinking_enabled=True,
        unbound_policy="abstain",
    )

    assert outcome.error is None, outcome.error
    assert outcome.report is not None
    assert outcome.decision is not None
    assert outcome.decision.timing_score is None
    assert outcome.decision.confidence <= 0.65
    assert "technical" in outcome.decision.dimensions_missing
    assert outcome.decision.dimensions_used == ["fundamental"]
    if "cashflow_lag" in outcome.snapshot.computed_flags and not outcome.report.abstain:
        assert any(
            citation.kind == "rule" and citation.id == "cashflow_lag"
            for citation in outcome.report.citations
        )
