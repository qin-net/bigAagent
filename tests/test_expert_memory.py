from __future__ import annotations

import json

import pytest

from insightagent.contracts import TaskStatus
from insightagent.persistence import SQLiteDatabase, SQLiteStateStore
from insightagent.state import (
    InMemoryStateStore,
    attach_prior_memory,
    prior_session_memory,
    snapshot_private_memory,
)
from tests.test_p0_research import ScriptedAgentLLM, _analyze


def _first_user_json(llm) -> dict:
    for message in llm.requests[0].messages:
        if message.role == "user" and message.content:
            return json.loads(message.content)
    raise AssertionError("no user message")


def test_snapshot_private_memory_keeps_whitelist():
    snapped = snapshot_private_memory(
        {
            "memory_summary": " 盯经营现金流证伪 ",
            "lessons": ["a", "b"],
            "stance": "hold",
            "open_questions": [],
        }
    )
    assert snapped["memory_summary"] == "盯经营现金流证伪"
    assert snapped["lessons"] == ["a", "b"]
    assert "stance" not in snapped
    assert "open_questions" not in snapped


def test_attach_prior_memory_skips_empty():
    raw = json.dumps({"task": "reval", "constraints": []})
    assert attach_prior_memory(raw, {}) == raw
    filled = json.loads(
        attach_prior_memory(raw, {"memory_summary": "盯现金流"})
    )
    assert filled["prior_memory"]["memory_summary"] == "盯现金流"


@pytest.mark.asyncio
async def test_inmemory_latest_and_inherit():
    store = InMemoryStateStore()
    first = await store.load_or_create(
        agent_name="fundamental",
        thesis_id="000858-initial",
        stock_code="000858",
    )
    first.private_memory = {"memory_summary": "盯经营现金流证伪"}
    first.status = TaskStatus.SUCCESS
    await store.save(first, expected_version=first.version)
    parent_id, memory = await prior_session_memory(
        store, agent_name="fundamental", thesis_id="000858-initial"
    )
    assert parent_id == first.session_id
    assert memory["memory_summary"] == "盯经营现金流证伪"
    child = await store.load_or_create(
        agent_name="fundamental",
        thesis_id="000858-initial",
        parent_session_id=parent_id,
    )
    assert child.session_id != first.session_id
    assert child.parent_session_id == first.session_id
    assert child.private_memory["memory_summary"] == "盯经营现金流证伪"


@pytest.mark.asyncio
async def test_second_analyze_injects_fundamental_memory_only(tmp_path):
    first_fund = ScriptedAgentLLM(
        "fundamental", memory_summary="盯经营现金流证伪"
    )
    await _analyze(
        tmp_path,
        "000858",
        first_fund,
        ScriptedAgentLLM("technical"),
        ScriptedAgentLLM("sentiment"),
        ScriptedAgentLLM("macro"),
    )
    store = SQLiteStateStore(SQLiteDatabase(str(tmp_path / "insightagent.db")))
    saved = await store.latest_for_agent(
        agent_name="fundamental", thesis_id="000858-initial"
    )
    assert saved is not None
    assert saved.private_memory.get("memory_summary") == "盯经营现金流证伪"

    second_fund = ScriptedAgentLLM("fundamental")
    second_tech = ScriptedAgentLLM("technical")
    await _analyze(
        tmp_path,
        "000858",
        second_fund,
        second_tech,
        ScriptedAgentLLM("sentiment"),
        ScriptedAgentLLM("macro"),
    )
    fund_task = _first_user_json(second_fund)
    assert fund_task["prior_memory"]["memory_summary"] == "盯经营现金流证伪"
    tech_task = _first_user_json(second_tech)
    assert "prior_memory" not in tech_task
