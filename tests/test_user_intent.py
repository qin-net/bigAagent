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
    build_expert_user_query,
    build_intent,
    empty_slots,
    extract_slots,
    parse_tags,
)
from insightagent.user_store import UserStore

from test_p0_research import _analyze, _make_four_agent_llms


def _slots_payload(**overrides) -> dict:
    payload = {name: NONE for name in LlmIntentSlots.model_fields}
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


@pytest.mark.asyncio
async def test_extract_skips_llm_when_body_is_none():
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
