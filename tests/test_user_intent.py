import json
import sqlite3

import pytest

from insightagent.contracts import LLMResponse
from insightagent.decision import build_multi_factor_decision
from insightagent.llm import FakeLLMAdapter
from insightagent.persistence import MIGRATION_1, SCHEMA_VERSION, SQLiteDatabase
from insightagent.user_contracts import NONE, LlmIntentSlots
from insightagent.user_intent import (
    DEFAULT_INSTRUCTION,
    EXTRACT_USER_PREFIX,
    build_expert_user_query,
    build_intent,
    empty_slots,
    extract_slots,
    fill_tagged_empty_slots,
    format_intent_echo,
    merge_parsed_with_slots,
    parse_tags,
    passes_memory_gate,
)
from insightagent.workflows.initial_research import format_cli_text
from insightagent.user_store import UserStore

from test_p0_research import _analyze, _make_four_agent_llms


def _slots_payload(**overrides) -> dict:
    payload = {
        name: NONE
        for name in LlmIntentSlots.model_fields
        if name not in {"effect", "dims"}
    }
    payload["effect"] = "this_run"
    payload["dims"] = NONE
    payload.update(overrides)
    return payload


def _extract_llm(payload: dict) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        [
            LLMResponse(
                id="extract-1",
                model="fake",
                content=json.dumps(payload, ensure_ascii=False),
                finish_reason="stop",
            )
        ]
    )


def test_parse_tags_empty_is_none():
    parsed = parse_tags("")
    assert parsed.body == NONE
    assert parsed.effect == "this_run"
    assert json.loads(parsed.tags) == [NONE]


def test_parse_tags_literal_none():
    parsed = parse_tags("none")
    assert parsed.body == NONE
    assert parsed.effect == "this_run"


def test_parse_tags_chinese_remember():
    parsed = parse_tags("#基本面 #记住 关注毛利率")
    assert parsed.body == "关注毛利率"
    assert parsed.effect == "remember"
    assert json.loads(parsed.tags) == ["fundamental", "remember"]
    assert json.loads(parsed.tagged_dims) == ["fundamental"]


def test_parse_tags_rerun_remember_combo():
    parsed = parse_tags("#rerun #remember 重看一次")
    assert parsed.effect == "remember_rerun"
    assert parsed.body == "重看一次"


def test_fill_tagged_empty_slots_copies_donor_not_body():
    parsed = parse_tags("#tracking 对照经营现金流证伪条件")
    slots = LlmIntentSlots.model_validate(
        _slots_payload(fundamental="对照经营现金流证伪利润条件")
    )
    filled = fill_tagged_empty_slots(parsed, slots)
    assert filled.tracking == "对照经营现金流证伪利润条件"
    assert filled.fundamental == "对照经营现金流证伪利润条件"
    empty = fill_tagged_empty_slots(parsed, empty_slots())
    assert empty.tracking == NONE


def test_nl_rewrite_fundamental_is_rerun():
    parsed = merge_parsed_with_slots(
        parse_tags("写得不行，重写一下基本面"),
        empty_slots(),
    )
    assert parsed.effect == "rerun"
    assert json.loads(parsed.tagged_dims) == ["fundamental"]
    assert "rerun" in json.loads(parsed.tags)


def test_chat_paraphrase_rerun_uses_llm_control():
    parsed = merge_parsed_with_slots(
        parse_tags("基本面能不能再给一版像样的"),
        LlmIntentSlots.model_validate(
            _slots_payload(effect="rerun", dims="fundamental")
        ),
    )
    assert parsed.effect == "rerun"
    assert json.loads(parsed.tagged_dims) == ["fundamental"]


def test_nl_end_does_not_rerun():
    parsed = merge_parsed_with_slots(
        parse_tags("这个任务结束吧，放弃了"),
        LlmIntentSlots.model_validate(_slots_payload(effect="end")),
    )
    assert parsed.effect == "this_run"
    assert parsed.body == NONE
    assert "end" in json.loads(parsed.tags)
    assert json.loads(parsed.tagged_dims) == [NONE]


def test_nl_dont_analyze_again_is_not_rerun():
    parsed = merge_parsed_with_slots(
        parse_tags("放弃了，别再分析了"),
        empty_slots(),
    )
    assert parsed.effect == "this_run"
    assert "rerun" not in json.loads(parsed.tags)


def test_nl_remember_without_hash():
    parsed = merge_parsed_with_slots(
        parse_tags("记住别只看便宜"),
        empty_slots(),
    )
    assert parsed.effect == "remember"
    assert "remember" in json.loads(parsed.tags)


def test_explicit_hash_tags_still_win():
    parsed = merge_parsed_with_slots(
        parse_tags("#technical #rerun 均线"),
        LlmIntentSlots.model_validate(_slots_payload(effect="end")),
    )
    assert parsed.effect == "rerun"
    assert json.loads(parsed.tagged_dims) == ["technical"]


def test_plain_constraint_is_not_rerun():
    parsed = merge_parsed_with_slots(
        parse_tags("利润须对照经营现金流"),
        empty_slots(),
    )
    assert parsed.effect == "this_run"
    assert json.loads(parsed.tagged_dims) == [NONE]


def test_llm_remember_rerun_and_invalid_effect():
    parsed = merge_parsed_with_slots(
        parse_tags("记住并且把基本面再出一版"),
        LlmIntentSlots.model_validate(
            _slots_payload(effect="remember_rerun", dims="fundamental")
        ),
    )
    assert parsed.effect == "remember_rerun"
    assert "fundamental" in json.loads(parsed.tagged_dims)

    parsed = merge_parsed_with_slots(
        parse_tags("随便看看"),
        LlmIntentSlots.model_validate(_slots_payload(effect="buy_now")),
    )
    assert parsed.effect == "this_run"


def test_llm_dims_union_hash_and_decision_only():
    parsed = merge_parsed_with_slots(
        parse_tags("#technical 再看一眼情绪"),
        LlmIntentSlots.model_validate(
            _slots_payload(effect="rerun", dims="sentiment")
        ),
    )
    assert parsed.effect == "rerun"
    assert json.loads(parsed.tagged_dims) == ["technical", "sentiment"]

    parsed = merge_parsed_with_slots(
        parse_tags("决策口径再套一次就行"),
        LlmIntentSlots.model_validate(
            _slots_payload(effect="rerun", dims="decision")
        ),
    )
    assert parsed.effect == "rerun"
    from insightagent.user_intent import is_decision_only_rerun, should_schedule_rerun

    intent = build_intent(utterance_id="u", parsed=parsed, slots=empty_slots())
    assert should_schedule_rerun(intent) is True
    assert is_decision_only_rerun(intent) is True


def test_hash_remember_ignores_llm_rerun():
    parsed = merge_parsed_with_slots(
        parse_tags("#记住 别只看便宜"),
        LlmIntentSlots.model_validate(
            _slots_payload(effect="rerun", dims="fundamental")
        ),
    )
    assert parsed.effect == "remember"
    assert json.loads(parsed.tagged_dims) == ["fundamental"]


@pytest.mark.asyncio
async def test_extract_accepts_dims_array_and_missing_effect():
    llm = FakeLLMAdapter(
        [
            LLMResponse(
                id="arr",
                model="fake",
                content=json.dumps(
                    {
                        "fundamental": "none",
                        "technical": "none",
                        "sentiment": "none",
                        "macro": "none",
                        "decision": "none",
                        "tracking": "none",
                        "not_evidence": "none",
                        "dims": ["fundamental", "technical"],
                    },
                    ensure_ascii=False,
                ),
                finish_reason="stop",
            )
        ]
    )
    slots, event = await extract_slots(llm, "两维都再看", "fake")
    assert event == "intent_parsed"
    assert slots.effect == "this_run"
    assert slots.dims == "fundamental,technical"
    parsed = merge_parsed_with_slots(parse_tags("两维都再看"), slots)
    assert json.loads(parsed.tagged_dims) == ["fundamental", "technical"]
    llm = FakeLLMAdapter([])
    slots, event = await extract_slots(llm, NONE, "fake")
    assert event == "intent_parsed"
    assert slots == empty_slots()
    assert llm.requests == []


@pytest.mark.asyncio
async def test_extract_bad_json_becomes_empty_slots():
    llm = FakeLLMAdapter(
        [
            LLMResponse(
                id="bad",
                model="fake",
                content="{not json",
                finish_reason="stop",
            )
        ]
    )
    slots, event = await extract_slots(llm, "看一下现金流", "fake")
    assert event == "intent_schema_invalid"
    assert slots == empty_slots()


@pytest.mark.asyncio
async def test_extract_null_field_is_schema_invalid():
    llm = _extract_llm(_slots_payload(fundamental=None))
    slots, event = await extract_slots(llm, "看一下现金流", "fake")
    assert event == "intent_schema_invalid"
    assert slots == empty_slots()


@pytest.mark.asyncio
async def test_extract_valid_slots():
    llm = _extract_llm(_slots_payload(fundamental="对比经营现金流"))
    slots, event = await extract_slots(llm, "对比经营现金流", "fake")
    assert event == "intent_parsed"
    assert slots.fundamental == "对比经营现金流"
    assert slots.technical == NONE
    assert llm.requests[0].thinking_enabled is False
    user_content = llm.requests[0].messages[1].content
    assert EXTRACT_USER_PREFIX in user_content
    assert "对比经营现金流" in user_content
    assert len(llm.requests) == 1


def test_build_expert_all_none_keeps_current_instruction():
    parsed = parse_tags(NONE)
    intent = build_intent(
        utterance_id="u1", parsed=parsed, slots=empty_slots()
    )
    query = json.loads(
        build_expert_user_query(
            run_id="r1",
            stock_code="000858",
            as_of="2026-01-01T00:00:00",
            dim="fundamental",
            intent=intent,
            preference_statements=[],
        )
    )
    assert query["instruction"] == DEFAULT_INSTRUCTION["fundamental"]
    assert query["required_questions"] == []
    assert query["constraints"] == []


def test_build_expert_question_goes_to_questions():
    parsed = parse_tags("毛利率高吗？")
    intent = build_intent(
        utterance_id="u1",
        parsed=parsed,
        slots=LlmIntentSlots.model_validate(
            _slots_payload(fundamental="毛利率高吗？")
        ),
    )
    query = json.loads(
        build_expert_user_query(
            run_id="r1",
            stock_code="000858",
            as_of="2026-01-01T00:00:00",
            dim="fundamental",
            intent=intent,
            preference_statements=["历史口径：看现金流"],
        )
    )
    assert query["required_questions"] == ["毛利率高吗？"]
    assert query["constraints"] == ["历史口径：看现金流"]


def test_memory_gate_matrix():
    assert passes_memory_gate(NONE) is False
    assert passes_memory_gate("   ") is False
    assert passes_memory_gate("写得不行") is False
    assert passes_memory_gate("太保守了") is False
    assert passes_memory_gate("3分改成4分") is False
    assert passes_memory_gate("对比经营现金流") is True
    assert passes_memory_gate("别光看便宜") is True
    assert passes_memory_gate("回购或增持只作事件") is True
    assert passes_memory_gate("利率相关度低时不要给方向") is True


def test_intent_echo_has_no_raw_user_text():
    parsed = parse_tags("#记住 写得不行XYZ")
    intent = build_intent(
        utterance_id="u1",
        parsed=parsed,
        slots=LlmIntentSlots.model_validate(
            _slots_payload(fundamental="对比经营现金流")
        ),
    )
    echo = format_intent_echo(intent)
    assert "意图理解（非原话）" in echo
    assert "对比经营现金流" in echo
    assert "写得不行XYZ" not in echo
    assert "效力: remember" in echo


@pytest.mark.asyncio
async def test_schema_v2_migrates_from_v1(tmp_path):
    path = tmp_path / "legacy.db"
    database = SQLiteDatabase(str(path))
    connection = database.connect()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + MIGRATION_1
            + "\nINSERT INTO schema_migrations(version, applied_at)"
            " VALUES (1, '2026-01-01T00:00:00');\nCOMMIT;"
        )
    finally:
        connection.close()

    await database.initialize()
    status = await database.status()
    assert status["schema_version"] == SCHEMA_VERSION
    assert status["tables_ok"] is True


@pytest.mark.asyncio
async def test_remember_does_not_store_raw_prompt(tmp_path):
    secret = "绝对不能写进库的那句话XYZUNIQUE"
    extract = _extract_llm(_slots_payload(fundamental="对比经营现金流"))
    fund_llm, tech_llm, sent_llm, macro_llm = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path,
        "000858",
        fund_llm,
        tech_llm,
        sent_llm,
        macro_llm,
        user_prompt="#remember " + secret,
        extract_llm_adapter=extract,
    )
    assert outcome.error is None

    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    assert secret not in dumped
    assert "对比经营现金流" in dumped

    prefs = await UserStore(
        SQLiteDatabase(str(tmp_path / "insightagent.db"))
    ).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    assert len(prefs) == 1
    assert prefs[0].statement == "对比经营现金流"


@pytest.mark.asyncio
async def test_prompt_none_matches_existing_fixture_run(tmp_path):
    fund_llm, tech_llm, sent_llm, macro_llm = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path, "000858", fund_llm, tech_llm, sent_llm, macro_llm, user_prompt=NONE
    )
    assert outcome.error is None
    payload = _user_payload(fund_llm)
    assert payload["instruction"] == DEFAULT_INSTRUCTION["fundamental"]
    assert payload["constraints"] == []
    assert payload["required_questions"] == []
    assert "意图理解" not in format_cli_text(outcome)


@pytest.mark.asyncio
async def test_extracted_slot_lands_in_expert_task(tmp_path):
    extract = _extract_llm(_slots_payload(fundamental="对比现金流质量"))
    fund_llm, tech_llm, sent_llm, macro_llm = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path,
        "000858",
        fund_llm,
        tech_llm,
        sent_llm,
        macro_llm,
        user_prompt="对比现金流质量",
        extract_llm_adapter=extract,
    )
    assert outcome.error is None
    payload = _user_payload(fund_llm)
    assert payload["constraints"] == ["用户口径：对比现金流质量"]
    tech_payload = _user_payload(tech_llm)
    assert tech_payload["constraints"] == []
    text = format_cli_text(outcome)
    assert "意图理解（非原话）" in text
    assert "对比现金流质量" in text


@pytest.mark.asyncio
async def test_remember_garbage_slot_does_not_write_preference(tmp_path):
    extract = _extract_llm(_slots_payload(fundamental="写得不行"))
    fund_llm, tech_llm, sent_llm, macro_llm = _make_four_agent_llms()
    outcome = await _analyze(
        tmp_path,
        "000858",
        fund_llm,
        tech_llm,
        sent_llm,
        macro_llm,
        user_prompt="#remember 写得不行",
        extract_llm_adapter=extract,
    )
    assert outcome.error is None
    prefs = await UserStore(
        SQLiteDatabase(str(tmp_path / "insightagent.db"))
    ).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    assert prefs == []


@pytest.mark.asyncio
async def test_remember_keeps_two_statements_with_same_trigger_prefix(tmp_path):
    first = "对照经营现金流不要只看估值分位后续一句甲"
    second = "对照经营现金流不要只看估值分位后续一句乙"
    assert first[:16] == second[:16]
    extract_one = _extract_llm(_slots_payload(fundamental=first))
    fund_a, tech_a, sent_a, macro_a = _make_four_agent_llms()
    await _analyze(
        tmp_path,
        "000858",
        fund_a,
        tech_a,
        sent_a,
        macro_a,
        user_prompt="#remember " + first,
        extract_llm_adapter=extract_one,
    )
    extract_two = _extract_llm(_slots_payload(fundamental=second))
    fund_b, tech_b, sent_b, macro_b = _make_four_agent_llms()
    await _analyze(
        tmp_path,
        "000858",
        fund_b,
        tech_b,
        sent_b,
        macro_b,
        user_prompt="#remember " + second,
        extract_llm_adapter=extract_two,
    )
    prefs = await UserStore(
        SQLiteDatabase(str(tmp_path / "insightagent.db"))
    ).active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    statements = sorted(item.statement for item in prefs)
    assert statements == sorted([first, second])


@pytest.mark.asyncio
async def test_remember_same_statement_retires_previous(tmp_path):
    statement = "对照经营现金流不要只看利润同比"
    for _ in range(2):
        extract = _extract_llm(_slots_payload(fundamental=statement))
        fund_llm, tech_llm, sent_llm, macro_llm = _make_four_agent_llms()
        await _analyze(
            tmp_path,
            "000858",
            fund_llm,
            tech_llm,
            sent_llm,
            macro_llm,
            user_prompt="#remember " + statement,
            extract_llm_adapter=extract,
        )
    store = UserStore(SQLiteDatabase(str(tmp_path / "insightagent.db")))
    active = await store.active_preferences(
        user_id="local", scope="fundamental", stock_code="000858"
    )
    assert len(active) == 1
    assert active[0].statement == statement


def test_decision_constraint_none_keeps_rationale_prefix_free():
    from insightagent.business_contracts import FundamentalSnapshot, Report

    reports = {}
    for role in ("fundamental", "technical", "sentiment", "macro"):
        reports[role] = Report(
            role=role,
            score=3,
            stance="abstain",
            summary="ok",
            citations=[],
            risks=["a", "b"],
            degraded=True,
            abstain=True,
            missing_information=["x"],
        )
    snapshot = FundamentalSnapshot(stock_code="000858")
    baseline = build_multi_factor_decision(
        reports["fundamental"],
        reports["technical"],
        reports["sentiment"],
        reports["macro"],
        snapshot,
    )
    same = build_multi_factor_decision(
        reports["fundamental"],
        reports["technical"],
        reports["sentiment"],
        reports["macro"],
        snapshot,
        user_constraint=NONE,
    )
    changed = build_multi_factor_decision(
        reports["fundamental"],
        reports["technical"],
        reports["sentiment"],
        reports["macro"],
        snapshot,
        user_constraint="偏谨慎",
    )
    assert same.rationale == baseline.rationale
    assert same.rating == baseline.rating
    assert changed.rating == baseline.rating
    assert changed.rationale.startswith("用户决策口径：偏谨慎。")


def _user_payload(llm) -> dict:
    for message in llm.requests[0].messages:
        if message.role == "user" and message.content:
            return json.loads(message.content)
    raise AssertionError("no user message")


def _dump_user_tables(path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        chunks = []
        for table in (
            "user_utterances",
            "user_intents",
            "user_preferences",
            "user_preference_versions",
            "audit_events",
        ):
            rows = connection.execute("SELECT * FROM {}".format(table)).fetchall()
            chunks.append(str(rows))
        return "\n".join(chunks)
    finally:
        connection.close()
