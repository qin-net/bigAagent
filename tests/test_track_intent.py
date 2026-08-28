from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from insightagent.contracts import LLMResponse
from insightagent.llm import FakeLLMAdapter
from insightagent.research_store import ResearchStore
from insightagent.tracking_agent import feedback_on_track, track_thesis
from insightagent.user_intent import (
    build_intent,
    parse_tags,
    should_schedule_track_rerun,
)
from insightagent.user_store import UserStore
from tests.test_tracking_agent import (
    ToolRouter,
    _experts,
    _final,
    _inquiry_current,
    _review_output,
    _seed_baseline,
    _skill_args,
    _tool,
    _unchanged_output,
)
from tests.test_user_intent import _extract_llm, _slots_payload
from insightagent.user_contracts import LlmIntentSlots


class CaptureLLM:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.users: list[str] = []

    async def complete(self, request):
        for message in request.messages:
            if message.role == "user":
                self.users.append(message.content)
        return await self.inner.complete(request)


def _tracker(responses):
    return FakeLLMAdapter(list(responses))


def _unchanged_tracker():
    return _tracker(
        [
            _tool("get_tracking_context", '{"dummy":""}', "c1"),
            _tool("get_prescreen", '{"dummy":""}', "c2"),
            _final(_unchanged_output()),
        ]
    )


def test_track_rerun_from_tracking_tag():
    parsed = parse_tags("#rerun #tracking")
    slots = LlmIntentSlots.model_validate(_slots_payload())
    intent = build_intent(utterance_id="u", parsed=parsed, slots=slots)
    assert should_schedule_track_rerun(intent) is True


def test_bare_rerun_does_not_schedule_track():
    parsed = parse_tags("#rerun")
    slots = LlmIntentSlots.model_validate(_slots_payload())
    intent = build_intent(utterance_id="u", parsed=parsed, slots=slots)
    assert should_schedule_track_rerun(intent) is False


@pytest.mark.asyncio
async def test_pre_track_prompt_injects_tracking_slot(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    capture = CaptureLLM(_unchanged_tracker())
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=capture,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        thinking_enabled=False,
        user_prompt="#tracking 对照现金流证伪条件",
        extract_llm_adapter=_extract_llm(
            _slots_payload(tracking="对照现金流证伪条件")
        ),
    )
    assert result.intent is not None
    assert result.intent.tracking == "对照现金流证伪条件"
    task = json.loads(capture.users[0])
    assert any("对照现金流证伪条件" in item for item in task["constraints"])
    store = UserStore(database)
    rows = sqlite3.connect(str(tmp_path / "insightagent.db"))
    try:
        utterance = rows.execute(
            "SELECT moment, effect FROM user_utterances WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    finally:
        rows.close()
    assert utterance == ("pre_run", "this_run")
    assert await store.intent_for_run_moment(
        run_id=result.run_id, moment="pre_run", effect="this_run"
    )


@pytest.mark.asyncio
async def test_sentiment_tag_does_not_force_call(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=_unchanged_tracker(),
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        thinking_enabled=False,
        user_prompt="#sentiment 问询函怎么看",
        extract_llm_adapter=_extract_llm(
            _slots_payload(sentiment="问询函怎么看")
        ),
    )
    assert result.skill_calls == []
    assert result.intent is not None
    assert result.intent.sentiment == "问询函怎么看"


@pytest.mark.asyncio
async def test_called_expert_receives_dim_slot(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    current = _inquiry_current(snaps)
    capture = CaptureLLM(_experts()["sentiment"])
    tracker = _tracker(
        [
            _tool("get_prescreen", '{"dummy":""}', "c1"),
            _tool(
                "call_sentiment",
                _skill_args("问询函是否改变风险感知", "新增问询"),
                "c2",
            ),
            _final(_review_output()),
        ]
    )
    router = ToolRouter(tracker, {**_experts(), "sentiment": capture})
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=router,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=current,
        thinking_enabled=False,
        user_prompt="#sentiment 只评问询对风险感知的影响",
        extract_llm_adapter=_extract_llm(
            _slots_payload(sentiment="只评问询对风险感知的影响")
        ),
    )
    assert result.skill_calls[0]["status"] == "success"
    blob = "\n".join(capture.users)
    assert "只评问询对风险感知的影响" in blob


@pytest.mark.asyncio
async def test_remember_tracking_pref_on_next_track(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=_unchanged_tracker(),
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        thinking_enabled=False,
        user_prompt="#tracking #remember 追踪时必须对比经营现金流",
        extract_llm_adapter=_extract_llm(
            _slots_payload(
                tracking="追踪时必须对比经营现金流",
                effect="remember",
            )
        ),
    )
    capture = CaptureLLM(_unchanged_tracker())
    await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=capture,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        thinking_enabled=False,
        extract_llm_adapter=FakeLLMAdapter([]),
    )
    task = json.loads(capture.users[0])
    assert any("对比经营现金流" in item for item in task["constraints"])


@pytest.mark.asyncio
async def test_post_track_this_run_waits_for_next_track(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    first = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=_unchanged_tracker(),
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        thinking_enabled=False,
    )
    before = first.deliverable["status"]
    fb = await feedback_on_track(
        first.run_id,
        database=database,
        llm_adapter=_unchanged_tracker(),
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="#tracking 下次对照回购是否只作事件",
        extract_llm_adapter=_extract_llm(
            _slots_payload(tracking="下次对照回购是否只作事件")
        ),
        current=snaps,
        fixture=True,
    )
    assert fb.track_result is None
    store = ResearchStore(database)
    packed = await store.get_run(first.run_id)
    assert packed["run"]["status"] == "success"
    assert packed is not None
    capture = CaptureLLM(_unchanged_tracker())
    second = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=capture,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        thinking_enabled=False,
        extract_llm_adapter=FakeLLMAdapter([]),
    )
    assert second.run_id != first.run_id
    task = json.loads(capture.users[0])
    assert any("回购" in item for item in task["constraints"])
    assert before == first.deliverable["status"]


@pytest.mark.asyncio
async def test_post_track_rerun_starts_new_track(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    first = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=_unchanged_tracker(),
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        thinking_enabled=False,
    )
    fb = await feedback_on_track(
        thesis_id,
        database=database,
        llm_adapter=_unchanged_tracker(),
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="#rerun #tracking 再对照一次现金流",
        extract_llm_adapter=_extract_llm(
            _slots_payload(
                tracking="再对照一次现金流",
                effect="rerun",
            )
        ),
        current=snaps,
        fixture=True,
    )
    assert fb.track_result is not None
    assert fb.track_result.run_id != first.run_id
    assert fb.track_result.intent is not None
    assert "现金流" in fb.track_result.intent.tracking


@pytest.mark.asyncio
async def test_empty_prompt_skips_track_feedback(tmp_path: Path):
    result = await feedback_on_track(
        "missing",
        database=None,  # type: ignore[arg-type]
        llm_adapter=FakeLLMAdapter([]),
        artifact_root=str(tmp_path),
        user_prompt="none",
    )
    assert result.skipped is True
