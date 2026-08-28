import json
import os
from pathlib import Path

import pytest

from insightagent.env import load_dotenv
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.methodology import MethodologyCatalog
from insightagent.persistence import SQLiteDatabase
from insightagent.research_store import ResearchStore
from insightagent.tracking_agent import distill_chapter, track_thesis

from tests.llm_recording import RecordingLLM
from tests.test_tracking_agent import _inquiry_current, _seed_baseline

pytestmark = pytest.mark.live

MODEL = "deepseek-v4-flash"
CHAPTER = (
    Path(__file__).resolve().parent / "fixtures" / "kb" / "cashflow-chapter.md"
)

DISTILL_WALK = (
    "This is a contract test. You MUST call tools in this order: "
    "read_source_markdown, list_allowed_flags, search_existing_entries "
    "with query 现金流, then submit_candidate with a new id starting with "
    "kb_live_distill_ and evidence_required containing only cashflow_lag, "
    "then submit_final. Do not call analysts."
)

TRACK_WALK = (
    "This is a contract test. You MUST: "
    "1) call get_tracking_context; 2) call get_prescreen; "
    "3) call search_methodology with query 问询 and scope sentiment; "
    "4) call submit_candidate with a new id starting with kb_live_track_ "
    "and evidence_required cashflow_lag; "
    "5) in a later loop call_sentiment exactly once. output_schema MUST be "
    "a JSON string of an object schema whose properties are answer (string), "
    "thesis_impact (enum none/weaken/invalidate/uncertain), evidence_refs "
    "(array of string), abstain (boolean), falsifier_hit (boolean), missing "
    "(array of string), all required, additionalProperties false; "
    "6) after that result, submit_final. Do not call two analysts in one loop. "
    "Do not skip steps. Do not invent invalidate without evidence."
)


def _adapter():
    load_dotenv()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY is not set")
    return DeepSeekChatAdapter(
        DeepSeekConfig(api_key=api_key, default_model=MODEL)
    )


def _assert_skill_schema(rec: RecordingLLM) -> None:
    calls = [
        item for item in rec.call_args if str(item["name"]).startswith("call_")
    ]
    assert calls, rec.calls
    for item in calls:
        raw = item["arguments"].get("output_schema")
        assert isinstance(raw, str) and raw.strip(), item
        schema = json.loads(raw)
        assert schema.get("type") == "object"
        assert schema.get("properties")


@pytest.mark.asyncio
async def test_live_track_fixture_baseline(tmp_path):
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
        thinking_enabled=True,
    )
    deliverable = result.deliverable
    assert result.run_id
    assert deliverable["status"] in {"unchanged", "review", "invalidate"}
    assert deliverable["user_output"]["holding_advice"] in {
        "unchanged",
        "review",
        "invalidate",
    }
    assert isinstance(deliverable["agent_skill_calls"], list)
    same_round = {}
    for item in result.skill_calls:
        if item.get("status") == "rejected":
            continue
        round_id = item.get("loop_round")
        same_round.setdefault(round_id, []).append(item["agent"])
        assert len(same_round[round_id]) <= 1
    if not result.prescreen.get("triggers"):
        assert deliverable["status"] != "invalidate"
    tracker_names = rec.names_when("get_tracking_context")
    assert "submit_final" in tracker_names
    packed = await ResearchStore(database).get_run(result.run_id)
    assert packed["run"]["mode"] == "track_day"


@pytest.mark.asyncio
async def test_live_track_inquiry_calls_sentiment(tmp_path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    current = _inquiry_current(snaps)
    rec = RecordingLLM(_adapter())
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=rec,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=current,
        model=MODEL,
        thinking_enabled=True,
    )
    assert any("has_inquiry" in item for item in result.prescreen["triggers"])
    assert result.prescreen["suggested_agent"] == "sentiment"
    tracker_names = rec.names_when("get_tracking_context")
    assert "get_prescreen" in tracker_names or "get_tracking_context" in tracker_names
    assert "call_sentiment" in tracker_names
    _assert_skill_schema(rec)
    sent = [item for item in result.skill_calls if item["agent"] == "sentiment"]
    assert sent
    assert sent[0]["status"] in {"success", "failed"}
    if sent[0]["status"] == "success":
        output = sent[0].get("output") or {}
        assert isinstance(output, dict)
        assert "role" not in output
        assert result.deliverable.get("thinking")
        assert result.deliverable.get("synthesis")
        assert any(
            item.get("agent") == "sentiment"
            for item in result.deliverable.get("expert_evaluations") or []
        )
    assert result.deliverable["status"] in {"unchanged", "review", "invalidate"}
    assert "get_event_snapshot" in rec.names_when("get_event_snapshot")
    timeline = await ResearchStore(database).list_timeline(thesis_id)
    assert timeline


@pytest.mark.asyncio
async def test_live_track_walks_scheduler_tools(tmp_path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    current = _inquiry_current(snaps)
    rec = RecordingLLM(_adapter())
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=rec,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=current,
        model=MODEL,
        thinking_enabled=True,
        instruction=TRACK_WALK,
    )
    tracker_names = rec.names_when("get_tracking_context")
    for name in (
        "get_tracking_context",
        "get_prescreen",
        "search_methodology",
        "submit_candidate",
        "call_sentiment",
        "submit_final",
    ):
        assert name in tracker_names, (name, tracker_names)
    _assert_skill_schema(rec)
    sent = [item for item in result.skill_calls if item["agent"] == "sentiment"]
    assert sent
    if sent[0]["status"] == "success":
        assert sent[0].get("output")
        assert "role" not in (sent[0].get("output") or {})
    assert result.deliverable["status"] in {"unchanged", "review", "invalidate"}
    catalog = MethodologyCatalog(database)
    candidates = catalog.list_payloads(statuses=("candidate",))
    assert any(
        str(item.get("id") or "").startswith("kb_live_track_")
        for item in candidates
    )


@pytest.mark.asyncio
async def test_live_distill_walks_every_tool(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    rec = RecordingLLM(_adapter())
    result = await distill_chapter(
        str(CHAPTER),
        scope="fundamental",
        database=database,
        llm_adapter=rec,
        model=MODEL,
        thinking_enabled=True,
        roots=[CHAPTER.parent],
        instruction=DISTILL_WALK,
    )
    assert rec.calls.count("read_source_markdown") >= 1
    assert "list_allowed_flags" in rec.calls
    assert "search_existing_entries" in rec.calls
    assert "submit_candidate" in rec.calls
    assert "submit_final" in rec.calls
    assert result.submitted_ids
    catalog = MethodologyCatalog(database)
    card = catalog.get(result.submitted_ids[0])
    assert card["status"] == "candidate"
