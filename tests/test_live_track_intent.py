from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from insightagent.env import load_dotenv
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig, FakeLLMAdapter
from insightagent.research_store import ResearchStore
from insightagent.tracking_agent import feedback_on_track, track_thesis
from insightagent.user_contracts import NONE
from insightagent.user_intent import extract_slots, passes_memory_gate
from insightagent.user_store import UserStore
from tests.llm_recording import RecordingLLM
from tests.test_live_track import MODEL, _adapter
from tests.test_track_intent import CaptureLLM, _unchanged_tracker
from tests.test_tracking_agent import _seed_baseline
from tests.test_user_intent import _dump_user_tables

pytestmark = pytest.mark.live

QUICK_TRACK = (
    "Call get_prescreen then submit_final with status unchanged, "
    "brief thinking and synthesis. Do not call analysts."
)


def _deepseek():
    load_dotenv()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY is not set")
    return DeepSeekChatAdapter(
        DeepSeekConfig(api_key=api_key, default_model=MODEL)
    )


def _tracker_tasks(rec: RecordingLLM) -> list:
    tasks = []
    for request in rec.requests:
        names = {
            (tool.get("function") or {}).get("name")
            for tool in request.tools or []
        }
        if "get_tracking_context" not in names:
            continue
        for message in request.messages:
            if message.role != "user" or not message.content:
                continue
            try:
                payload = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if payload.get("task") == "track":
                tasks.append(payload)
                break
    return tasks


def _joined_fields(task: dict) -> str:
    parts = list(task.get("constraints") or [])
    parts.extend(task.get("required_questions") or [])
    parts.append(str(task.get("instruction") or ""))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_live_extract_tracking_oral():
    raw = "这次追踪盯紧经营现金流证伪，别只看涨跌"
    slots, event = await extract_slots(_deepseek(), raw, MODEL)
    assert event == "intent_parsed"
    assert slots.tracking != NONE
    assert slots.tracking != raw
    assert any(
        token in slots.tracking for token in ("现金", "证伪", "追踪")
    )


@pytest.mark.asyncio
async def test_live_track_prompt_injects_and_does_not_store_raw(tmp_path: Path):
    raw = "这次对照经营现金流证伪条件，不要只看股价涨跌"
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    rec = RecordingLLM(_adapter())
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=rec,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        model=MODEL,
        thinking_enabled=False,
        user_prompt="#tracking " + raw,
        instruction=QUICK_TRACK,
    )
    assert result.intent is not None
    assert result.intent.tracking != NONE
    assert result.intent.tracking != raw
    tasks = _tracker_tasks(rec)
    assert tasks, "tracker never received a track Task JSON"
    blob = _joined_fields(tasks[0])
    assert result.intent.tracking[:12] in blob or any(
        token in blob for token in ("现金", "证伪")
    )
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    assert raw not in dumped
    rows = sqlite3.connect(str(tmp_path / "insightagent.db"))
    try:
        moment = rows.execute(
            "SELECT moment FROM user_utterances WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    finally:
        rows.close()
    assert moment == ("pre_run",)
    assert result.deliverable["status"] in {"unchanged", "review", "invalidate"}


@pytest.mark.asyncio
async def test_live_post_track_this_run_overlays_next(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    first = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=_adapter(),
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        model=MODEL,
        thinking_enabled=False,
        instruction=QUICK_TRACK,
    )
    raw = "下次对照回购是不是只当事件，别当成基本面好转"
    fb = await feedback_on_track(
        first.run_id,
        database=database,
        llm_adapter=_adapter(),
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="#tracking " + raw,
        model=MODEL,
        thinking_enabled=False,
        current=snaps,
        fixture=True,
    )
    assert fb.track_result is None
    assert fb.intent is not None
    packed = await ResearchStore(database).get_run(first.run_id)
    assert packed["run"]["status"] == "success"
    rec = RecordingLLM(_adapter())
    second = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=rec,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        model=MODEL,
        thinking_enabled=False,
        instruction=QUICK_TRACK,
    )
    assert second.run_id != first.run_id
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    tagged = "#tracking " + raw
    assert tagged not in dumped
    assert fb.intent.tracking != NONE
    tasks = _tracker_tasks(rec)
    assert tasks
    blob = _joined_fields(tasks[0])
    assert fb.intent.tracking[:8] in blob or "回购" in blob


@pytest.mark.asyncio
async def test_live_post_track_rerun_and_remember(tmp_path: Path):
    """Extract is live; tracker is scripted so this asserts the prompt pipeline."""
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    extract = _deepseek()
    first = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=_unchanged_tracker(),
        extract_llm_adapter=extract,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        model=MODEL,
        thinking_enabled=False,
        user_prompt="#tracking #remember 追踪时必须对比经营现金流",
    )
    assert first.intent is not None
    dumped = _dump_user_tables(tmp_path / "insightagent.db")
    assert "#tracking #remember" not in dumped
    prefs = await UserStore(database).active_preferences(
        user_id="local", scope="tracking", stock_code="000858"
    )
    if passes_memory_gate(first.intent.tracking):
        assert prefs
    fb = await feedback_on_track(
        thesis_id,
        database=database,
        llm_adapter=_unchanged_tracker(),
        extract_llm_adapter=extract,
        artifact_root=str(tmp_path / "artifacts"),
        user_prompt="#rerun #tracking 再对照一次现金流证伪",
        model=MODEL,
        thinking_enabled=False,
        current=snaps,
        fixture=True,
    )
    assert fb.track_result is not None
    assert fb.track_result.run_id != first.run_id
    assert fb.intent is not None
    assert fb.intent.effect in {"rerun", "remember_rerun"}
    rec = CaptureLLM(_unchanged_tracker())
    later = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=rec,
        extract_llm_adapter=FakeLLMAdapter([]),
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=snaps,
        model=MODEL,
        thinking_enabled=False,
    )
    assert later.run_id != first.run_id
    if prefs:
        blob = _joined_fields(json.loads(rec.users[0]))
        assert prefs[0].statement[:8] in blob or "现金" in blob
