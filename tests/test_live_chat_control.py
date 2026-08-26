import json
import os

import pytest

from insightagent.env import load_dotenv
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.user_contracts import DIMS, NONE
from insightagent.user_intent import (
    compute_rerun_dims,
    is_decision_only_rerun,
    should_schedule_rerun,
    understand_prompt,
)
from insightagent.workflows.initial_research import feedback_on_run
from test_feedback_u2 import _db, _parent
from test_user_intent import _dump_user_tables

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


async def _understand(text: str):
    return await understand_prompt(_deepseek(), text, MODEL)


@pytest.mark.asyncio
async def test_live_chat_rewrite_paraphrase_is_fundamental_rerun():
    raw = "基本面能不能再给一版像样的"
    parsed, slots, event = await _understand(raw)
    assert event == "intent_parsed"
    assert parsed.effect == "rerun"
    assert "fundamental" in json.loads(parsed.tagged_dims)
    assert slots.fundamental == NONE or slots.fundamental != raw
    assert slots.effect in {"rerun", "this_run"}
    intent_tags = json.loads(parsed.tags)
    assert "rerun" in intent_tags


@pytest.mark.asyncio
async def test_live_chat_tech_another_pass_is_technical_rerun():
    parsed, slots, event = await _understand("把技术这边的趋势再梳理清楚")
    assert event == "intent_parsed"
    assert parsed.effect == "rerun"
    assert "technical" in json.loads(parsed.tagged_dims)


@pytest.mark.asyncio
async def test_live_chat_stop_is_end_not_rerun():
    raw = "到此为止吧，结束了，我不看了"
    parsed, slots, event = await _understand(raw)
    assert event == "intent_parsed"
    assert parsed.effect == "this_run"
    assert "rerun" not in json.loads(parsed.tags)
    assert "end" in json.loads(parsed.tags) or (
        _analysis_none(slots) and slots.effect == "end"
    )
    assert parsed.body == NONE or slots.effect == "end"


@pytest.mark.asyncio
async def test_live_chat_remember_without_hash():
    raw = "以后都别只看便宜，得对照质量"
    parsed, slots, event = await _understand(raw)
    assert event == "intent_parsed"
    assert parsed.effect == "remember"
    assert slots.fundamental != NONE
    assert slots.fundamental != raw
    assert any(
        token in slots.fundamental for token in ("估值", "便宜", "质量", "PE", "pe")
    )


@pytest.mark.asyncio
async def test_live_chat_constraint_is_this_run_not_rerun():
    raw = "利润好看的话也要对照经营现金流"
    parsed, slots, event = await _understand(raw)
    assert event == "intent_parsed"
    assert parsed.effect == "this_run"
    assert "rerun" not in json.loads(parsed.tags)
    assert slots.fundamental != NONE
    assert slots.fundamental != raw


@pytest.mark.asyncio
async def test_live_chat_look_around_stays_idle():
    parsed, slots, event = await _understand("帮我看看")
    assert event == "intent_parsed"
    assert parsed.effect == "this_run"
    assert "rerun" not in json.loads(parsed.tags)
    assert _analysis_none(slots)


@pytest.mark.asyncio
async def test_live_chat_hash_rerun_beats_end_wording():
    parsed, slots, event = await _understand(
        "#rerun #fundamental 算了结束吧还是把基本面再跑一次"
    )
    assert event == "intent_parsed"
    assert parsed.effect == "rerun"
    assert json.loads(parsed.tagged_dims) == ["fundamental"]


@pytest.mark.asyncio
async def test_live_feedback_chat_rerun_fundamental_only(tmp_path):
    parent, fund, tech, sent, macro = await _parent(tmp_path)
    fund.phase = "tool"
    tech_before = len(tech.requests)
    fund_before = len(fund.requests)
    raw = "基本面写得含糊，能不能正经再出一版"
    result = await feedback_on_run(
        parent.run.run_id,
        database=_db(tmp_path),
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt=raw,
        extract_llm_adapter=_deepseek(),
        model=MODEL,
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert result.intent is not None
    assert should_schedule_rerun(result.intent) is True
    assert compute_rerun_dims(result.intent) == ["fundamental"]
    assert is_decision_only_rerun(result.intent) is False
    assert result.outcome is not None
    assert result.outcome.error is None, result.outcome.error
    assert result.outcome.rerun_dimensions == ["fundamental"]
    assert len(fund.requests) == fund_before + 2
    assert len(tech.requests) == tech_before
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    assert raw not in dumped
