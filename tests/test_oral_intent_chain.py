"""Full-chain: parse → extract once → two gates → experts → sqlite → next run."""

from __future__ import annotations

import json
import sqlite3

import pytest

from insightagent.contracts import LLMResponse
from insightagent.llm import FakeLLMAdapter
from insightagent.persistence import SQLiteDatabase
from insightagent.user_contracts import NONE, LlmIntentSlots
from insightagent.user_store import UserStore
from insightagent.workflows.initial_research import format_cli_text
from test_p0_research import _analyze, _make_four_agent_llms
from test_user_intent import _dump_user_tables, _extract_llm, _slots_payload, _user_payload


def _store(tmp_path):
    return UserStore(SQLiteDatabase(str(tmp_path / "insightagent.db")))


async def _chain(
    tmp_path,
    prompt,
    *,
    slots=None,
    stock="000858",
    extract=None,
):
    fund, tech, sent, macro = _make_four_agent_llms()
    extract_llm = extract
    if extract_llm is None and prompt not in ("", NONE):
        extract_llm = _extract_llm(slots or _slots_payload())
    outcome = await _analyze(
        tmp_path,
        stock,
        fund,
        tech,
        sent,
        macro,
        user_prompt=prompt,
        extract_llm_adapter=extract_llm,
    )
    return outcome, fund, tech, sent, macro, extract_llm


@pytest.mark.asyncio
async def test_empty_prompt_skips_extract_and_echo(tmp_path):
    extract = FakeLLMAdapter([])
    outcome, fund, *_rest, extract_llm = await _chain(
        tmp_path, NONE, extract=extract
    )
    assert outcome.error is None
    assert extract_llm.requests == []
    assert _user_payload(fund)["constraints"] == []
    assert "意图理解" not in format_cli_text(outcome)
    assert outcome.show_intent_echo is False


@pytest.mark.asyncio
async def test_this_run_executes_without_writing_memory(tmp_path):
    outcome, fund, tech, *_ = await _chain(
        tmp_path,
        "对比现金流质量",
        slots=_slots_payload(fundamental="对比现金流质量"),
    )
    assert outcome.error is None
    assert outcome.intent.effect == "this_run"
    assert _user_payload(fund)["constraints"] == ["用户口径：对比现金流质量"]
    assert _user_payload(tech)["constraints"] == []
    prefs = await _store(tmp_path).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    assert prefs == []
    text = format_cli_text(outcome)
    assert "意图理解（非原话）" in text
    assert "效力: this_run" in text


@pytest.mark.asyncio
async def test_remember_writes_memory_and_executes_same_run(tmp_path):
    statement = "对比经营现金流"
    outcome, fund, *_ = await _chain(
        tmp_path,
        "#remember " + statement,
        slots=_slots_payload(fundamental=statement),
    )
    assert outcome.error is None
    assert outcome.intent.effect == "remember"
    constraints = _user_payload(fund)["constraints"]
    assert "用户口径：" + statement in constraints
    assert statement in constraints
    prefs = await _store(tmp_path).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    assert [item.statement for item in prefs] == [statement]


@pytest.mark.asyncio
async def test_garbage_remember_executes_but_skips_memory(tmp_path):
    outcome, fund, *_ = await _chain(
        tmp_path,
        "#记住 写得不行",
        slots=_slots_payload(fundamental="写得不行"),
    )
    assert outcome.error is None
    assert _user_payload(fund)["constraints"] == ["用户口径：写得不行"]
    prefs = await _store(tmp_path).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    assert prefs == []


@pytest.mark.asyncio
async def test_not_evidence_is_not_memory_or_expert_constraint(tmp_path):
    secret = "同事说要涨UNIQUE"
    outcome, fund, tech, sent, macro, extract = await _chain(
        tmp_path,
        "#remember " + secret,
        slots=_slots_payload(not_evidence="他人看多"),
    )
    assert outcome.error is None
    assert outcome.intent.not_evidence == "他人看多"
    for agent in (fund, tech, sent, macro):
        assert _user_payload(agent)["constraints"] == []
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    assert secret not in dumped
    assert secret not in format_cli_text(outcome)
    for scope in (
        "fundamental",
        "technical",
        "sentiment",
        "macro",
        "decision",
        "tracking",
    ):
        prefs = await _store(tmp_path).active_preferences(
            user_id="local", scope=scope, stock_code="000858"
        )
        assert prefs == []


@pytest.mark.asyncio
async def test_split_slots_hit_only_named_experts(tmp_path):
    outcome, fund, tech, sent, macro, extract = await _chain(
        tmp_path,
        "便宜而且均线难看",
        slots=_slots_payload(
            fundamental="估值不要只看价格低",
            technical="以下行趋势为主不要把超卖当反转",
        ),
    )
    assert outcome.error is None
    assert len(extract.requests) == 1
    assert _user_payload(fund)["constraints"] == ["用户口径：估值不要只看价格低"]
    assert _user_payload(tech)["constraints"] == [
        "用户口径：以下行趋势为主不要把超卖当反转"
    ]
    assert _user_payload(sent)["constraints"] == []
    assert _user_payload(macro)["constraints"] == []


@pytest.mark.asyncio
async def test_second_run_without_prompt_injects_remembered_rule(tmp_path):
    statement = "利润须对照经营现金流"
    await _chain(
        tmp_path,
        "#remember " + statement,
        slots=_slots_payload(fundamental=statement),
    )
    fund, tech, sent, macro = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path,
        "000858",
        fund,
        tech,
        sent,
        macro,
        user_prompt=NONE,
        extract_llm_adapter=FakeLLMAdapter([]),
    )
    assert outcome.error is None
    assert _user_payload(fund)["constraints"] == [statement]
    assert _user_payload(tech)["constraints"] == []
    assert "意图理解" not in format_cli_text(outcome)


@pytest.mark.asyncio
async def test_decision_slot_is_execution_only_for_rationale(tmp_path):
    outcome, fund, *_ = await _chain(
        tmp_path,
        "决策偏谨慎",
        slots=_slots_payload(decision="偏谨慎"),
    )
    assert outcome.error is None
    assert _user_payload(fund)["constraints"] == []
    assert outcome.decision.rationale.startswith("用户决策口径：偏谨慎。")
    prefs = await _store(tmp_path).active_preferences(
        user_id="local", scope="decision", stock_code="000858"
    )
    assert prefs == []


@pytest.mark.asyncio
async def test_remember_decision_is_memory_not_expert_task(tmp_path):
    await _chain(
        tmp_path,
        "#remember 决策不要追高",
        slots=_slots_payload(decision="不要追高须对照估值"),
    )
    prefs = await _store(tmp_path).active_preferences(
        user_id="local", scope="decision", stock_code="000858"
    )
    assert [item.statement for item in prefs] == ["不要追高须对照估值"]
    fund, tech, sent, macro = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path,
        "000858",
        fund,
        tech,
        sent,
        macro,
        user_prompt=NONE,
        extract_llm_adapter=FakeLLMAdapter([]),
    )
    assert outcome.decision.rationale.startswith(
        "用户决策口径：不要追高须对照估值。"
    )
    assert _user_payload(fund)["constraints"] == []


@pytest.mark.asyncio
async def test_tracking_slot_can_be_remembered_but_is_not_an_agent(tmp_path):
    outcome, fund, *_ = await _chain(
        tmp_path,
        "#remember #tracking 跟踪时对照证伪条件",
        slots=_slots_payload(tracking="跟踪时对照已写证伪条件"),
    )
    assert outcome.error is None
    assert outcome.run.session_ids.get("tracking") is None
    assert _user_payload(fund)["constraints"] == []
    prefs = await _store(tmp_path).active_preferences(
        user_id="local", scope="tracking", stock_code="000858"
    )
    assert [item.statement for item in prefs] == ["跟踪时对照已写证伪条件"]


@pytest.mark.asyncio
async def test_macro_slot_only_lands_in_macro_task(tmp_path):
    outcome, fund, tech, sent, macro, _ = await _chain(
        tmp_path,
        "白酒别用利率做买卖",
        slots=_slots_payload(macro="利率对该股相关度低时不要给出方向"),
    )
    assert outcome.error is None
    assert _user_payload(fund)["constraints"] == []
    assert _user_payload(macro)["constraints"] == [
        "用户口径：利率对该股相关度低时不要给出方向"
    ]


@pytest.mark.asyncio
async def test_extract_failure_still_runs_experts_without_user_constraint(
    tmp_path,
):
    extract = FakeLLMAdapter(
        [
            LLMResponse(
                id="bad",
                model="fake",
                content="{not json",
                finish_reason="stop",
            )
        ]
    )
    outcome, fund, *_rest, extract_llm = await _chain(
        tmp_path, "看一下现金流", extract=extract
    )
    assert outcome.error is None
    assert extract_llm.requests and len(extract_llm.requests) == 1
    assert _user_payload(fund)["constraints"] == []
    assert outcome.intent.fundamental == NONE


@pytest.mark.asyncio
async def test_remember_rerun_tag_remembers_but_does_not_second_expert_loop(
    tmp_path,
):
    outcome, fund, tech, sent, macro, extract = await _chain(
        tmp_path,
        "#rerun #remember 对比经营现金流",
        slots=_slots_payload(fundamental="对比经营现金流"),
    )
    assert outcome.error is None
    assert outcome.intent.effect == "remember_rerun"
    assert len(extract.requests) == 1
    assert len(fund.requests) == 2
    assert len(tech.requests) == 2
    prefs = await _store(tmp_path).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    assert len(prefs) == 1


@pytest.mark.asyncio
async def test_chinese_tags_and_raw_prompt_never_land_in_json_or_db(tmp_path):
    secret = "绝对不能出现的口语XYZ999"
    outcome, *_ = await _chain(
        tmp_path,
        "#基本面 #记住 " + secret,
        slots=_slots_payload(fundamental="估值不要只看价格低"),
    )
    assert outcome.error is None
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    blob = json.dumps(outcome.to_dict(), ensure_ascii=False)
    assert secret not in dumped
    assert secret not in blob
    assert secret not in format_cli_text(outcome)
    assert "估值不要只看价格低" in format_cli_text(outcome)


@pytest.mark.asyncio
async def test_question_slot_uses_required_questions(tmp_path):
    outcome, fund, *_ = await _chain(
        tmp_path,
        "毛利率高吗？",
        slots=_slots_payload(fundamental="毛利率高吗？"),
    )
    payload = _user_payload(fund)
    assert payload["required_questions"] == ["毛利率高吗？"]
    assert payload["constraints"] == []


@pytest.mark.asyncio
async def test_memory_does_not_leak_across_stocks(tmp_path):
    await _chain(
        tmp_path,
        "#remember 对比经营现金流",
        slots=_slots_payload(fundamental="对比经营现金流"),
        stock="000858",
    )
    fund, tech, sent, macro = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path,
        "000001",
        fund,
        tech,
        sent,
        macro,
        user_prompt=NONE,
        extract_llm_adapter=FakeLLMAdapter([]),
    )
    assert outcome.error is None
    assert _user_payload(fund)["constraints"] == []


@pytest.mark.asyncio
async def test_all_none_slots_with_body_still_echo(tmp_path):
    outcome, fund, *_ = await _chain(
        tmp_path,
        "帮我看看",
        slots=_slots_payload(),
    )
    assert outcome.show_intent_echo is True
    assert "效力: this_run" in format_cli_text(outcome)
    assert _user_payload(fund)["constraints"] == []


def test_extract_user_payload_contains_body_not_only_examples():
    fields = LlmIntentSlots.model_fields
    assert "not_evidence" in fields
    assert set(LlmIntentSlots.model_fields) >= {
        "fundamental",
        "technical",
        "sentiment",
        "macro",
        "decision",
        "tracking",
        "not_evidence",
    }
