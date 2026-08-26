from __future__ import annotations

import json
import sqlite3

import pytest

from insightagent.llm import FakeLLMAdapter
from insightagent.persistence import SQLiteDatabase
from insightagent.research_store import ResearchStore
from insightagent.user_contracts import NONE
from insightagent.user_intent import compute_rerun_dims, should_schedule_rerun
from insightagent.user_store import UserStore
from insightagent.workflows.initial_research import (
    FeedbackError,
    feedback_on_run,
    format_cli_text,
)
from test_p0_research import _analyze, _make_four_agent_llms
from test_user_intent import _dump_user_tables, _extract_llm, _slots_payload


async def _parent(tmp_path):
    fund, tech, sent, macro = _make_four_agent_llms()
    outcome = await _analyze(tmp_path, "000858", fund, tech, sent, macro)
    return outcome, fund, tech, sent, macro


def _db(tmp_path):
    return SQLiteDatabase(str(tmp_path / "insightagent.db"))


def _run_count(tmp_path) -> int:
    connection = sqlite3.connect(str(tmp_path / "insightagent.db"))
    try:
        return connection.execute("SELECT count(*) FROM runs").fetchone()[0]
    finally:
        connection.close()


def _utterance_count(tmp_path) -> int:
    connection = sqlite3.connect(str(tmp_path / "insightagent.db"))
    try:
        return connection.execute(
            "SELECT count(*) FROM user_utterances"
        ).fetchone()[0]
    finally:
        connection.close()


def test_rerun_dims_from_tag_and_slot():
    from insightagent.user_intent import build_intent, parse_tags
    from insightagent.user_contracts import LlmIntentSlots

    parsed = parse_tags("#rerun #fundamental")
    slots = LlmIntentSlots.model_validate(_slots_payload())
    intent = build_intent(utterance_id="u", parsed=parsed, slots=slots)
    assert compute_rerun_dims(intent) == ["fundamental"]
    assert should_schedule_rerun(intent) is True

    parsed = parse_tags("#rerun")
    intent = build_intent(utterance_id="u", parsed=parsed, slots=slots)
    assert compute_rerun_dims(intent) == []
    assert should_schedule_rerun(intent) is False


@pytest.mark.asyncio
async def test_feedback_none_skips_everything(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    runs_before = _run_count(tmp_path)
    utterances_before = _utterance_count(tmp_path)
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="none",
        extract_llm_adapter=FakeLLMAdapter([]),
    )
    assert result.skipped is True
    assert result.intent is None
    assert result.outcome is None
    assert _run_count(tmp_path) == runs_before
    assert _utterance_count(tmp_path) == utterances_before
    assert len(fund.requests) == 2
    assert len(tech.requests) == 2


@pytest.mark.asyncio
async def test_this_run_does_not_change_parent_or_call_experts(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    parent_rating = parent.decision.rating
    runs_before = _run_count(tmp_path)
    fund_before = len(fund.requests)
    secret = "SECRETXYZ别只看便宜"
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt=secret,
        extract_llm_adapter=_extract_llm(
            _slots_payload(
                fundamental="估值不要只看价格低，须对照经营现金流"
            )
        ),
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert result.outcome is None
    assert result.intent is not None
    assert result.intent.fundamental != NONE
    assert result.intent.effect == "this_run"
    assert _run_count(tmp_path) == runs_before
    assert len(fund.requests) == fund_before
    stored = await ResearchStore(_db(tmp_path)).get_run(parent.run.run_id)
    assert stored["decisions"][0]["rating"] == parent_rating
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    assert secret not in dumped


@pytest.mark.asyncio
async def test_remember_without_rerun_writes_preference(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    fund_before = len(fund.requests)
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="#remember #fundamental 估值对照经营现金流不要只看PE",
        extract_llm_adapter=_extract_llm(
            _slots_payload(
                fundamental="估值不要只看PE，须对照经营现金流"
            )
        ),
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert result.outcome is None
    assert len(fund.requests) == fund_before
    prefs = await UserStore(_db(tmp_path)).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    assert len(prefs) == 1
    assert "现金流" in prefs[0].statement


@pytest.mark.asyncio
async def test_rerun_fundamental_only_freezes_other_reports(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    parent_tech = parent.technical_report.model_dump(mode="json")
    parent_sent = parent.sentiment_report.model_dump(mode="json")
    parent_macro = parent.macro_report.model_dump(mode="json")
    parent_rating = parent.decision.rating
    tech_before = len(tech.requests)
    sent_before = len(sent.requests)
    macro_before = len(macro.requests)
    fund_before = len(fund.requests)
    fund.phase = "tool"
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="#rerun #fundamental 对照季节性再看现金流",
        extract_llm_adapter=_extract_llm(
            _slots_payload(
                fundamental="经营现金流为负须对照季节性，不能直接写成质量崩了"
            )
        ),
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert result.outcome is not None
    child = result.outcome
    assert child.error is None
    assert child.run.run_id != parent.run.run_id
    assert child.run.parent_run_id == parent.run.run_id
    assert child.run.thesis_id == parent.run.thesis_id
    assert child.rerun_dimensions == ["fundamental"]
    assert "technical" in child.copied_dimensions
    assert len(fund.requests) == fund_before + 2
    assert len(tech.requests) == tech_before
    assert len(sent.requests) == sent_before
    assert len(macro.requests) == macro_before
    assert child.technical_report.model_dump(mode="json") == parent_tech
    assert child.sentiment_report.model_dump(mode="json") == parent_sent
    assert child.macro_report.model_dump(mode="json") == parent_macro
    assert child.decision is not None
    text = format_cli_text(child)
    assert "反馈前" in text
    assert "反馈后" in text
    assert "fundamental" in text or "本次重跑" in text
    store = ResearchStore(_db(tmp_path))
    parent_stored = await store.get_run(parent.run.run_id)
    child_stored = await store.get_run(child.run.run_id)
    assert parent_stored["decisions"][0]["rating"] == parent_rating
    assert child_stored["decisions"]
    assert parent_stored["run"]["run_id"] == parent.run.run_id


@pytest.mark.asyncio
async def test_rerun_with_empty_slots_is_noop(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    runs_before = _run_count(tmp_path)
    fund_before = len(fund.requests)
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="#rerun",
        extract_llm_adapter=FakeLLMAdapter([]),
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert result.noop is True
    assert result.outcome is None
    assert _run_count(tmp_path) == runs_before
    assert len(fund.requests) == fund_before


@pytest.mark.asyncio
async def test_missing_or_failed_parent_errors(tmp_path):
    await _parent(tmp_path)
    with pytest.raises(FeedbackError):
        await feedback_on_run(
            "missing-run",
            database=_db(tmp_path),
            llm_adapter=FakeLLMAdapter([]),
            artifact_root=str(tmp_path / "artifacts"),
            user_prompt="#rerun #fundamental",
        )
    fund, tech, sent, macro = _make_four_agent_llms()
    failed = await _analyze(tmp_path, "000858", fund, tech, sent, macro)
    store = ResearchStore(_db(tmp_path))
    failed.run.status = "failed"
    await store.save_run(failed.run)
    with pytest.raises(FeedbackError):
        await feedback_on_run(
            failed.run.run_id,
            database=_db(tmp_path),
            llm_adapter=fund,
            artifact_root=str(tmp_path / "artifacts"),
            user_prompt="#rerun #fundamental",
            extract_llm_adapter=FakeLLMAdapter([]),
        )
    runs_after = _run_count(tmp_path)
    assert runs_after == 2


@pytest.mark.asyncio
async def test_nl_rewrite_without_hash_reruns_fundamental(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    fund_before = len(fund.requests)
    tech_before = len(tech.requests)
    fund.phase = "tool"
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="基本面能不能再给一版像样的",
        extract_llm_adapter=_extract_llm(
            _slots_payload(effect="rerun", dims="fundamental")
        ),
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert result.outcome is not None
    assert result.outcome.rerun_dimensions == ["fundamental"]
    assert len(fund.requests) == fund_before + 2
    assert len(tech.requests) == tech_before


@pytest.mark.asyncio
async def test_llm_end_does_not_create_child_run(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    runs_before = _run_count(tmp_path)
    fund_before = len(fund.requests)
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="这次到此为止吧我不看了",
        extract_llm_adapter=_extract_llm(_slots_payload(effect="end")),
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert result.outcome is None
    assert result.intent is not None
    assert result.intent.effect == "this_run"
    assert "end" in json.loads(result.intent.tags)
    assert _run_count(tmp_path) == runs_before
    assert len(fund.requests) == fund_before


@pytest.mark.asyncio
async def test_llm_technical_rerun_does_not_call_fundamental(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    fund_before = len(fund.requests)
    tech_before = len(tech.requests)
    tech.phase = "tool"
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="技术面再梳理一版结构",
        extract_llm_adapter=_extract_llm(
            _slots_payload(effect="rerun", dims="technical")
        ),
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert result.outcome is not None
    assert result.outcome.rerun_dimensions == ["technical"]
    assert "fundamental" in result.outcome.copied_dimensions
    assert len(fund.requests) == fund_before
    assert len(tech.requests) == tech_before + 2
