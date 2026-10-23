from __future__ import annotations

import json
from pathlib import Path

import pytest

from insightagent.business_contracts import InitialSynthesis
from insightagent.contracts import LLMResponse, LLMToolCall
from insightagent.llm import FakeLLMAdapter
from insightagent.persistence import SQLiteAuditLog, SQLiteDatabase, SQLiteStateStore
from insightagent.research_store import ResearchStore
from insightagent.tracking_agent import finalize_initial_synthesis, track_thesis
from insightagent.fundamental_agent import default_fixtures_dir

from tests.test_p0_research import ScriptedAgentLLM, _analyze, _make_four_agent_llms

FIXTURES = default_fixtures_dir()


def _tool(name: str, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        id=call_id,
        model="fake",
        content=name,
        tool_calls=[LLMToolCall(id=call_id, name=name, arguments='{"dummy":""}')],
        finish_reason="tool_calls",
    )


def _synth_final(output: dict, memory: dict | None = None) -> LLMResponse:
    # submit_final arrives as a tool call, so state_patch must use the
    # DeepSeek-strict list form: set=[{path, value}], string values only.
    sets = [
        {"path": "private_memory.{}".format(key), "value": str(value)}
        for key, value in (memory or {}).items()
    ]
    payload = {
        "status": "completed",
        "output": output,
        "reflection": {"what_worked": ["read reports"]},
        "state_patch": {"set": sets, "append": [], "remove": []},
    }
    return LLMResponse(
        id="final",
        model="fake",
        content="done",
        tool_calls=[
            LLMToolCall(
                id="f1",
                name="submit_final",
                arguments=json.dumps(payload, ensure_ascii=False),
            )
        ],
        finish_reason="tool_calls",
    )


def _synthesis_output() -> dict:
    return {
        "thinking": "技术与基本面同向，情绪中性，无需折扣。",
        "synthesis": "基本面与技术面同向支持持有，情绪面未见异常；宏观弃权不改变方向。",
        "expert_evaluations": [
            {"agent": "fundamental", "reliability": "high", "verdict": "accept"},
            {"agent": "technical", "reliability": "medium", "verdict": "accept"},
            {"agent": "sentiment", "reliability": "medium", "verdict": "accept"},
            {"agent": "macro", "reliability": "low", "verdict": "insufficient"},
        ],
    }


def _synth_adapter(memory: dict | None = None) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        [
            _tool("get_expert_reports", "c1"),
            _tool("get_decision", "c2"),
            _synth_final(_synthesis_output(), memory),
        ]
    )


@pytest.mark.asyncio
async def test_synthesis_happy_path_persists_and_surfaces(tmp_path: Path):
    fund, tech, sent, macro = _make_four_agent_llms()
    synth = _synth_adapter()
    outcome = await _analyze(
        tmp_path, "000858", fund, tech, sent, macro,
        synthesis_llm_adapter=synth,
    )
    assert outcome.error is None
    assert outcome.run.status == "success"
    assert outcome.synthesis is not None
    assert "持有" in outcome.synthesis.synthesis
    assert len(outcome.synthesis.expert_evaluations) == 4
    assert outcome.run.session_ids.get("synthesis")
    # persisted as the first timeline row of the thesis
    store = ResearchStore(SQLiteDatabase(str(tmp_path / "insightagent.db")))
    timeline_row = await store.timeline_for_run(outcome.run.run_id)
    assert timeline_row is not None
    deliverable = timeline_row["deliverable"]
    assert deliverable["synthesis"] == outcome.synthesis.synthesis
    # serialized outcome carries it too
    assert outcome.to_dict()["synthesis"]["synthesis"]
    # CLI text shows it
    from insightagent.workflows.initial_research import format_cli_text

    text = format_cli_text(outcome)
    assert "综合分析" in text
    assert "fundamental:accept" in text


@pytest.mark.asyncio
async def test_synthesis_sees_no_analyst_tools(tmp_path: Path):
    fund, tech, sent, macro = _make_four_agent_llms()
    synth = _synth_adapter()
    await _analyze(
        tmp_path, "000858", fund, tech, sent, macro,
        synthesis_llm_adapter=synth,
    )
    assert synth.requests, "synthesis adapter was never called"
    for request in synth.requests:
        names = {
            tool.get("function", {}).get("name")
            for tool in request.tools
            if isinstance(tool, dict)
        }
        assert "get_expert_reports" in names
        assert "get_decision" in names
        assert not names & {
            "call_fundamental",
            "call_technical",
            "call_sentiment",
            "call_macro",
            "read_source_markdown",
        }


@pytest.mark.asyncio
async def test_synthesis_failure_degrades_quietly(tmp_path: Path):
    fund, tech, sent, macro = _make_four_agent_llms()
    # Empty queue: the first synthesis request raises inside the agent.
    broken = FakeLLMAdapter([])
    outcome = await _analyze(
        tmp_path, "000858", fund, tech, sent, macro,
        synthesis_llm_adapter=broken,
    )
    assert outcome.error is None
    assert outcome.run.status == "success"
    assert outcome.synthesis is None
    assert "synthesis" not in outcome.run.session_ids
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    store = ResearchStore(database)
    assert await store.timeline_for_run(outcome.run.run_id) is None
    events = await SQLiteAuditLog(database).list(run_id=outcome.run.run_id)
    assert any(item["event_type"] == "synthesis_failed" for item in events)


@pytest.mark.asyncio
async def test_synthesis_disabled_by_default(tmp_path: Path):
    fund, tech, sent, macro = _make_four_agent_llms()
    outcome = await _analyze(tmp_path, "000858", fund, tech, sent, macro)
    assert outcome.synthesis is None
    assert "synthesis" not in outcome.run.session_ids
    store = ResearchStore(SQLiteDatabase(str(tmp_path / "insightagent.db")))
    assert await store.timeline_for_run(outcome.run.run_id) is None


@pytest.mark.asyncio
async def test_synthesis_memory_seeds_tracking_chain(tmp_path: Path):
    fund, tech, sent, macro = _make_four_agent_llms()
    synth = _synth_adapter(memory={"memory_summary": "首评：持有，盯现金流证伪"})
    outcome = await _analyze(
        tmp_path, "000858", fund, tech, sent, macro,
        synthesis_llm_adapter=synth,
    )
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    saved = await SQLiteStateStore(database).latest_for_agent(
        agent_name="tracking",
        thesis_id="000858-initial",
        user_id="local",
    )
    assert saved is not None
    assert saved.session_id == outcome.run.session_ids["synthesis"]
    assert saved.private_memory.get("memory_summary") == "首评：持有，盯现金流证伪"

    tracker = FakeLLMAdapter(
        [
            _tool("get_tracking_context", "c1"),
            _synth_final(
                {
                    "status": "unchanged",
                    "work_summary": "无变化",
                    "user_output": {"summary": "维持", "holding_advice": "unchanged"},
                }
            ),
        ]
    )
    result = await track_thesis(
        "000858-initial",
        database=database,
        llm_adapter=tracker,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        fixtures_dir=str(FIXTURES),
        thinking_enabled=False,
    )
    assert result.run_id
    first_user = next(
        message
        for message in tracker.requests[0].messages
        if message.role == "user" and message.content
    )
    task = json.loads(first_user.content)
    assert task["prior_memory"]["memory_summary"] == "首评：持有，盯现金流证伪"


def test_finalize_fills_missing_evaluations_and_never_raises():
    result = finalize_initial_synthesis(
        {
            "thinking": "只评了基本面",
            "synthesis": "",
            "expert_evaluations": [
                {"agent": "fundamental", "verdict": "accept"},
                {"agent": "nonsense", "verdict": "accept"},  # dropped
                "not-a-dict",  # dropped
            ],
        },
        present_roles=["fundamental", "technical", "macro"],
    )
    assert isinstance(result, InitialSynthesis)
    # synthesis falls back to thinking
    assert result.synthesis == "只评了基本面"
    by_agent = {item.agent: item for item in result.expert_evaluations}
    assert set(by_agent) == {"fundamental", "technical", "macro"}
    assert by_agent["fundamental"].verdict == "accept"
    assert by_agent["technical"].verdict == "insufficient"
    assert by_agent["macro"].verdict == "insufficient"
