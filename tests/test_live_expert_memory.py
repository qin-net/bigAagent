from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from insightagent.business_contracts import RunRecord
from insightagent.fundamental_agent import default_fixtures_dir
from insightagent.fundamentals import (
    FixtureFundamentalAdapter,
    apply_fundamental_rules,
)
from insightagent.persistence import FileArtifactStore, SQLiteDatabase, SQLiteStateStore
from insightagent.workflows.initial_research import _run_fundamental_agent
from tests.llm_recording import RecordingLLM
from tests.test_live_track import MODEL, _adapter

pytestmark = pytest.mark.live

MEMORY_MARK = "盯经营现金流证伪"
FIRST_INSTRUCTION = (
    "Call get_fundamental_snapshot once, then submit_final. "
    "state_patch.set must include path private_memory.memory_summary "
    "with value exactly {}. "
    "Do not skip that state_patch field."
).format(MEMORY_MARK)
SECOND_INSTRUCTION = (
    "Call get_fundamental_snapshot once, then submit_final. "
    "Use prior_memory if present. Do not invent a new memory_summary."
)
FIXTURES = default_fixtures_dir()


def _first_task(rec: RecordingLLM) -> dict:
    for request in rec.requests:
        for message in request.messages:
            if message.role != "user" or not message.content:
                continue
            try:
                payload = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    raise AssertionError("expert never received a JSON user Task")


async def _snapshot(code: str = "000858"):
    adapter = FixtureFundamentalAdapter.from_directory(FIXTURES)
    return apply_fundamental_rules(await adapter.fetch_fundamental(code))


@pytest.mark.parametrize("trial", [1, 2, 3])
@pytest.mark.asyncio
async def test_live_fundamental_memory_carries(tmp_path: Path, trial: int):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    await database.initialize()
    artifacts = FileArtifactStore(database, str(tmp_path / "artifacts"))
    snapshot = await _snapshot()
    thesis_id = "000858-mem-{}".format(trial)
    first_run = RunRecord(
        run_id=str(uuid4()),
        stock_code="000858",
        thesis_id=thesis_id,
        status="running",
    )
    first_query = json.dumps(
        {
            "run_id": first_run.run_id,
            "stock_code": "000858",
            "instruction": FIRST_INSTRUCTION,
            "objective": "完成本维分析并写下私有记忆",
            "reason": "live memory seed",
            "required_questions": [],
            "constraints": [],
        },
        ensure_ascii=False,
    )
    await _run_fundamental_agent(
        snapshot=snapshot,
        run=first_run,
        database=database,
        artifacts=artifacts,
        llm_adapter=_adapter(),
        model=MODEL,
        thinking_enabled=False,
        user_query=first_query,
    )
    saved = await SQLiteStateStore(database).latest_for_agent(
        agent_name="fundamental", thesis_id=thesis_id
    )
    assert saved is not None, "first live run left no SUCCESS state"
    summary = str((saved.private_memory or {}).get("memory_summary") or "")
    assert MEMORY_MARK in summary or "现金" in summary, saved.private_memory

    rec = RecordingLLM(_adapter())
    second_run = RunRecord(
        run_id=str(uuid4()),
        stock_code="000858",
        thesis_id=thesis_id,
        status="running",
    )
    second_query = json.dumps(
        {
            "run_id": second_run.run_id,
            "stock_code": "000858",
            "instruction": SECOND_INSTRUCTION,
            "objective": "对照上次记忆完成本维分析",
            "reason": "live memory read",
            "required_questions": [],
            "constraints": [],
        },
        ensure_ascii=False,
    )
    await _run_fundamental_agent(
        snapshot=snapshot,
        run=second_run,
        database=database,
        artifacts=artifacts,
        llm_adapter=rec,
        model=MODEL,
        thinking_enabled=False,
        user_query=second_query,
    )
    task = _first_task(rec)
    prior = task.get("prior_memory") or {}
    assert prior.get("memory_summary"), task
    assert MEMORY_MARK in str(prior.get("memory_summary")) or "现金" in str(
        prior.get("memory_summary")
    )
    assert first_run.session_ids.get("fundamental") != second_run.session_ids.get(
        "fundamental"
    )
    second_state = await SQLiteStateStore(database).get(
        second_run.session_ids["fundamental"]
    )
    assert second_state.parent_session_id == saved.session_id
