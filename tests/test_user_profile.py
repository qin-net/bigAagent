from __future__ import annotations

import json

import pytest

from insightagent.contracts import utc_now
from insightagent.persistence import SQLiteDatabase
from insightagent.user_contracts import UserIntent, UserPreference, UserUtterance
from insightagent.user_store import UserStore


def _utterance(**overrides) -> UserUtterance:
    now = utc_now().isoformat()
    payload = dict(
        utterance_id="u1",
        user_id="local",
        moment="pre_run",
        effect="remember",
        tags=json.dumps(["fundamental", "remember"]),
        intent_id="i1",
        stock_code="000001",
        thesis_id="000001-initial",
        run_id="r1",
        created_at=now,
    )
    payload.update(overrides)
    return UserUtterance(**payload)


def _intent(**overrides) -> UserIntent:
    now = utc_now().isoformat()
    payload = dict(
        intent_id="i1",
        utterance_id="u1",
        effect="remember",
        tags=json.dumps(["fundamental", "remember"]),
        fundamental="盯经营现金流",
        technical="none",
        sentiment="none",
        macro="none",
        decision="none",
        tracking="none",
        not_evidence="none",
        created_at=now,
    )
    payload.update(overrides)
    return UserIntent(**payload)


@pytest.mark.asyncio
async def test_profile_aggregates_without_raw_prompt(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    await database.initialize()
    store = UserStore(database)
    now = utc_now().isoformat()
    await store.save_utterance(_utterance())
    await store.save_intent(_intent())
    await store.save_preference(
        UserPreference(
            preference_id="pref-1",
            user_id="local",
            status="active",
            current_version="1",
            kind="constraint",
            scope="fundamental",
            stock_code="000001",
            trigger="盯经营现金流",
            title="盯经营现金流",
            statement="估值必须对照经营现金流",
            source="user_feedback",
            source_utterance_id="u1",
            source_run_id="r1",
            created_at=now,
            updated_at=now,
        )
    )
    profile = await store.profile(user_id="local")
    assert profile["utterance_count"] == 1
    assert profile["effects"]["remember"] == 1
    assert profile["tags"]["fundamental"] == 1
    assert profile["dims"]["fundamental"] == 1
    assert profile["dims"]["technical"] == 0
    assert profile["stocks"][0]["stock_code"] == "000001"
    assert profile["preferences"][0]["statement"] == "估值必须对照经营现金流"
    assert "常 #remember" in profile["highlights"]
    assert "常约束基本面" in profile["highlights"]
    dumped = json.dumps(profile, ensure_ascii=False)
    assert "记住别只看便宜" not in dumped

    from insightboard.research import load_user_profile

    board_profile = await load_user_profile(db_path=str(tmp_path / "insightagent.db"))
    assert board_profile["utterance_count"] == 1
    assert "expert_memories" in board_profile

    other = await store.profile(user_id="local", stock_code="000002")
    assert other["preferences"] == []

    assert await store.retire_preference(user_id="local", preference_id="pref-1") is True
    after = await store.profile(user_id="local")
    assert after["preferences"] == []
    assert await store.retire_preference(user_id="local", preference_id="pref-1") is False
