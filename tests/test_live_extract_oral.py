import os

import pytest

from insightagent.env import load_dotenv
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.persistence import SQLiteDatabase
from insightagent.user_contracts import DIMS, NONE
from insightagent.user_intent import extract_slots, passes_memory_gate
from insightagent.user_store import UserStore
from insightagent.workflows.initial_research import format_cli_text
from test_p0_research import _analyze, _make_four_agent_llms
from test_user_intent import _dump_user_tables, _user_payload

pytestmark = pytest.mark.live

MODEL = "deepseek-v4-flash"


def _deepseek():
    load_dotenv()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY is not set")
    return DeepSeekChatAdapter(
        DeepSeekConfig(api_key=api_key, default_model=MODEL)
    )


def _analysis_none(slots) -> bool:
    return all(getattr(slots, name) == NONE for name in DIMS)


@pytest.mark.asyncio
async def test_live_extract_cheap_goes_to_fundamental():
    body = "别光看便宜"
    slots, event = await extract_slots(_deepseek(), body, MODEL)
    assert event == "intent_parsed"
    assert slots.fundamental != NONE
    assert slots.fundamental != body
    assert (
        "估值" in slots.fundamental
        or "PE" in slots.fundamental
        or "pe" in slots.fundamental.lower()
        or "价格" in slots.fundamental
    )
    assert slots.technical == NONE
    assert slots.sentiment == NONE
    assert slots.not_evidence == NONE


@pytest.mark.asyncio
async def test_live_extract_look_around_is_all_none():
    slots, event = await extract_slots(_deepseek(), "帮我看看", MODEL)
    assert event == "intent_parsed"
    assert _analysis_none(slots)
    assert slots.not_evidence == NONE


@pytest.mark.asyncio
async def test_live_extract_too_conservative_is_all_none():
    slots, event = await extract_slots(_deepseek(), "写得太保守了", MODEL)
    assert event == "intent_parsed"
    assert _analysis_none(slots)


@pytest.mark.asyncio
async def test_live_extract_rumor_is_not_evidence():
    raw = "我同事说要涨"
    slots, event = await extract_slots(_deepseek(), raw, MODEL)
    assert event == "intent_parsed"
    assert _analysis_none(slots)
    assert slots.not_evidence != NONE
    assert slots.not_evidence != raw


@pytest.mark.asyncio
async def test_live_extract_fake_profit_mentions_cash_or_quality():
    raw = "利润好看会不会假"
    slots, event = await extract_slots(_deepseek(), raw, MODEL)
    assert event == "intent_parsed"
    assert slots.fundamental != NONE
    assert slots.fundamental != raw
    assert any(
        token in slots.fundamental for token in ("现金", "质量", "利润")
    )
    assert slots.technical == NONE


@pytest.mark.asyncio
async def test_live_extract_splits_cheap_and_downtrend():
    slots, event = await extract_slots(
        _deepseek(), "便宜而且线一直往下好吓人", MODEL
    )
    assert event == "intent_parsed"
    assert slots.fundamental != NONE
    assert slots.technical != NONE
    assert slots.fundamental != slots.technical


@pytest.mark.asyncio
async def test_live_chain_deepseek_extract_scripted_experts(tmp_path):
    raw = "别光看便宜"
    fund, tech, sent, macro = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path,
        "000858",
        fund,
        tech,
        sent,
        macro,
        user_prompt="#remember " + raw,
        extract_llm_adapter=_deepseek(),
        model=MODEL,
    )
    assert outcome.error is None, outcome.error
    assert outcome.intent is not None
    assert outcome.intent.fundamental != NONE
    assert outcome.intent.fundamental != raw
    assert raw not in format_cli_text(outcome)
    payload = _user_payload(fund)
    assert payload["constraints"]
    assert any("用户口径：" in item for item in payload["constraints"])
    assert _user_payload(tech)["constraints"] == []
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    assert raw not in dumped
    prefs = await UserStore(
        SQLiteDatabase(str(tmp_path / "insightagent.db"))
    ).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    if passes_memory_gate(outcome.intent.fundamental):
        assert len(prefs) == 1
        assert prefs[0].statement == outcome.intent.fundamental[:80]
        assert prefs[0].statement != raw
    else:
        assert prefs == []


@pytest.mark.asyncio
async def test_live_chain_garbage_prompt_does_not_copy_into_preference(
    tmp_path,
):
    raw = "写得不行"
    fund, tech, sent, macro = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path,
        "000858",
        fund,
        tech,
        sent,
        macro,
        user_prompt="#记住 " + raw,
        extract_llm_adapter=_deepseek(),
        model=MODEL,
    )
    assert outcome.error is None, outcome.error
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    assert raw not in dumped
    prefs = await UserStore(
        SQLiteDatabase(str(tmp_path / "insightagent.db"))
    ).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    if outcome.intent.fundamental == NONE or not passes_memory_gate(
        outcome.intent.fundamental
    ):
        assert prefs == []
    else:
        assert raw not in prefs[0].statement
