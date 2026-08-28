from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from insightagent.business_contracts import Decision, Report, RunRecord
from insightagent.contracts import LLMResponse, LLMToolCall, utc_now
from insightagent.data_contracts import EventItem, EventSnapshot
from insightagent.fundamental_agent import default_fixtures_dir
from insightagent.llm import FakeLLMAdapter
from insightagent.persistence import FileArtifactStore, SQLiteDatabase
from insightagent.research_store import ResearchStore
from insightagent.tracking import fetch_track_snapshots, prescreen
from insightagent.tracking_agent import (
    DEFAULT_TRACK_OUTPUT_SCHEMA,
    track_thesis,
)
from insightagent.workflows.initial_research import analyze_stock

from tests.test_p0_research import ScriptedAgentLLM

FIXTURES = default_fixtures_dir()


def _schema_json() -> str:
    return json.dumps(DEFAULT_TRACK_OUTPUT_SCHEMA, ensure_ascii=False)


def _skill_args(question: str, reason: str) -> str:
    return json.dumps(
        {
            "question": question,
            "reason": reason,
            "output_schema": _schema_json(),
        },
        ensure_ascii=False,
    )


def _instance_for_schema(schema: dict) -> dict:
    properties = schema.get("properties") or {}
    filled = {}
    for key, spec in properties.items():
        if not isinstance(spec, dict):
            filled[key] = "scripted"
            continue
        if spec.get("enum"):
            filled[key] = spec["enum"][0]
        elif spec.get("type") == "boolean":
            filled[key] = False
        elif spec.get("type") == "integer":
            filled[key] = 0
        elif spec.get("type") == "number":
            filled[key] = 0.0
        elif spec.get("type") == "array":
            filled[key] = []
        elif spec.get("type") == "object" or "properties" in spec:
            filled[key] = _instance_for_schema(spec)
        else:
            filled[key] = "scripted"
    return filled


def _output_schema_from_request(request) -> dict:
    for tool in request.tools:
        function = tool.get("function") or {}
        if function.get("name") != "submit_final":
            continue
        params = function.get("parameters") or {}
        properties = params.get("properties") or {}
        output = properties.get("output") or {}
        ref = output.get("$ref")
        if isinstance(ref, str):
            name = ref.split("/")[-1]
            defs = params.get("$def") or params.get("$defs") or {}
            return defs.get(name) or {"type": "object", "properties": {}}
        return output
    raise AssertionError("submit_final schema missing")


class ScriptedRevalLLM:
    def __init__(self, role: str) -> None:
        self.role = role
        self.phase = "tool"

    async def complete(self, request):
        if self.phase == "tool":
            self.phase = "final"
            tool_name = {
                "fundamental": "get_fundamental_snapshot",
                "technical": "get_indicator_snapshot",
                "sentiment": "get_event_snapshot",
                "macro": "get_macro_snapshot",
            }[self.role]
            return LLMResponse(
                id="tool-1",
                model="fake",
                content="reading snapshot",
                tool_calls=[
                    LLMToolCall(id="call-1", name=tool_name, arguments="{}")
                ],
                finish_reason="tool_calls",
            )
        schema = _output_schema_from_request(request)
        payload = {
            "status": "completed",
            "output": _instance_for_schema(schema),
            "reflection": {
                "what_worked": ["used snapshot tool"],
                "what_was_missing": [],
                "process_errors": [],
            },
            "state_patch": {"set": [], "append": [], "remove": []},
        }
        self.phase = "done"
        return LLMResponse(
            id="final-1",
            model="fake",
            content=json.dumps(payload, ensure_ascii=False),
            finish_reason="stop",
        )


def _experts() -> dict:
    return {
        "sentiment": ScriptedRevalLLM("sentiment"),
        "fundamental": ScriptedRevalLLM("fundamental"),
        "technical": ScriptedRevalLLM("technical"),
        "macro": ScriptedRevalLLM("macro"),
    }


def _tool(name: str, arguments: str, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        id=call_id,
        model="fake",
        content=name,
        tool_calls=[
            LLMToolCall(id=call_id, name=name, arguments=arguments)
        ],
        finish_reason="tool_calls",
    )


def _final(output: dict) -> LLMResponse:
    payload = {
        "status": "completed",
        "output": output,
        "reflection": {
            "what_worked": ["read prescreen"],
            "what_was_missing": [],
            "process_errors": [],
        },
        "state_patch": {"set": [], "append": [], "remove": []},
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


def _unchanged_output() -> dict:
    return {
        "status": "unchanged",
        "work_summary": "对照基线无实质增量",
        "thinking": "预筛无新增触发，档案里的持有理由仍然成立。",
        "synthesis": "本次无需叫醒专家，维持原 thesis。",
        "expert_evaluations": [],
        "user_output": {
            "summary": "维持原判断",
            "holding_advice": "unchanged",
        },
    }


class ToolRouter:
    def __init__(self, tracking: FakeLLMAdapter, experts: dict):
        self.tracking = tracking
        self.experts = experts

    async def complete(self, request):
        names = set()
        for tool in request.tools:
            function = tool.get("function") or {}
            names.add(function.get("name"))
        if "get_tracking_context" in names:
            return await self.tracking.complete(request)
        if "get_event_snapshot" in names:
            return await self.experts["sentiment"].complete(request)
        if "get_fundamental_snapshot" in names:
            return await self.experts["fundamental"].complete(request)
        if "get_indicator_snapshot" in names:
            return await self.experts["technical"].complete(request)
        if "get_macro_snapshot" in names:
            return await self.experts["macro"].complete(request)
        raise RuntimeError("no route for tools {}".format(sorted(names)))


class TrackSnapshotsClone:
    def __init__(self, snaps):
        self.fundamental = snaps.fundamental.model_copy(deep=True)
        self.technical = deepcopy(snaps.technical)
        self.sentiment = deepcopy(snaps.sentiment)
        self.macro = deepcopy(snaps.macro)


async def _seed_baseline(tmp_path: Path, *, stock_code: str = "000858"):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    await database.initialize()
    artifacts = FileArtifactStore(database, str(tmp_path / "artifacts"))
    snaps = await fetch_track_snapshots(
        stock_code, fixture=True, fixtures_dir=str(FIXTURES)
    )
    fund_ref = await artifacts.put(
        json.dumps(snaps.fundamental.model_dump(mode="json"), ensure_ascii=False)
    )
    tech_ref = await artifacts.put(json.dumps(snaps.technical, ensure_ascii=False))
    sent_ref = await artifacts.put(json.dumps(snaps.sentiment, ensure_ascii=False))
    macro_ref = await artifacts.put(json.dumps(snaps.macro, ensure_ascii=False))
    run = RunRecord(
        run_id="run-baseline",
        stock_code=stock_code,
        thesis_id="{}-initial".format(stock_code),
        status="success",
        snapshot_refs={
            "fundamental": fund_ref,
            "technical": tech_ref,
            "sentiment": sent_ref,
            "macro": macro_ref,
        },
    )
    store = ResearchStore(database)
    await store.save_run(run)
    await store.save_report(
        run.run_id,
        "fundamental",
        Report(
            role="fundamental",
            score=3,
            stance="hold",
            summary="基线基本面。",
            citations=[
                {
                    "ref_id": "r1",
                    "kind": "field",
                    "id": "roe",
                    "source": "snapshot",
                }
            ],
            risks=["估值波动"],
            falsifiers=["经营现金流持续为负"],
        ),
    )
    await store.save_decision(
        run.run_id,
        Decision(
            rating="hold",
            confidence=0.6,
            rationale="三维持有",
            falsifiers=["经营现金流持续为负"],
            risks=["估值波动", "事件冲击"],
            advice_one_liner="维持持有，对照现金流",
        ),
    )
    return database, snaps, run.thesis_id


@pytest.mark.asyncio
async def test_prescreen_new_reduction_routes_sentiment():
    snaps = await fetch_track_snapshots(
        "000858", fixture=True, fixtures_dir=str(FIXTURES)
    )
    current = TrackSnapshotsClone(snaps)
    events = EventSnapshot(**current.sentiment["events"])
    events.events.append(
        EventItem(
            event_id="evt-inquiry",
            event_type="inquiry",
            title="问询函",
            published_at=utc_now().isoformat(),
            source="fixture",
        )
    )
    current.sentiment = dict(current.sentiment)
    current.sentiment["events"] = events.model_dump(mode="json")
    payload = prescreen(baseline=snaps, current=current)
    assert payload["suggested_agent"] == "sentiment"
    assert any("has_inquiry" in item for item in payload["triggers"])


@pytest.mark.asyncio
async def test_track_unchanged_calls_no_analyst(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    tracker = FakeLLMAdapter(
        [
            _tool("get_tracking_context", '{"dummy":""}', "c1"),
            _tool("get_prescreen", '{"dummy":""}', "c2"),
            _final(_unchanged_output()),
        ]
    )
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=tracker,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        fixtures_dir=str(FIXTURES),
        current=snaps,
        thinking_enabled=False,
    )
    assert result.deliverable["status"] == "unchanged"
    assert result.skill_calls == []
    assert result.prescreen["suggested_agent"] is None
    store = ResearchStore(database)
    packed = await store.get_run(result.run_id)
    assert packed["run"]["mode"] == "track_day"
    timeline = await store.list_timeline(thesis_id)
    assert timeline
    assert timeline[0]["advice"] == "unchanged"


def _inquiry_current(snaps):
    current = TrackSnapshotsClone(snaps)
    events = EventSnapshot(**current.sentiment["events"])
    events.events.append(
        EventItem(
            event_id="evt-inquiry",
            event_type="inquiry",
            title="问询函",
            published_at=utc_now().isoformat(),
            source="fixture",
        )
    )
    current.sentiment = dict(current.sentiment)
    current.sentiment["events"] = events.model_dump(mode="json")
    return current


def _review_output() -> dict:
    return {
        "status": "review",
        "work_summary": "问询需复核",
        "thinking": "问询是新事实。情绪专家回答了风险感知，但未覆盖现金流证伪，不足以直接证伪 thesis。",
        "synthesis": "接受情绪面「风险上升」的判断，折扣其把问询写成崩塌的部分；维持持有但标记复核。",
        "expert_evaluations": [
            {
                "agent": "sentiment",
                "reliability": "medium",
                "verdict": "accept",
                "gaps": ["未对照基线证伪条件"],
                "notes": "问询存在，风险感知变化可接受，不构成单独 invalidate。",
            }
        ],
        "user_output": {
            "summary": "建议复核",
            "holding_advice": "review",
        },
    }


@pytest.mark.asyncio
async def test_one_loop_rejects_second_analyst(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    current = _inquiry_current(snaps)
    tracker = FakeLLMAdapter(
        [
            _tool("get_prescreen", '{"dummy":""}', "c1"),
            LLMResponse(
                id="same-round",
                model="fake",
                content="two experts",
                tool_calls=[
                    LLMToolCall(
                        id="c2",
                        name="call_sentiment",
                        arguments=_skill_args(
                            "问询函是否改变风险感知", "新增问询"
                        ),
                    ),
                    LLMToolCall(
                        id="c3",
                        name="call_fundamental",
                        arguments=_skill_args(
                            "再看现金流", "同一轮想再叫一个"
                        ),
                    ),
                ],
                finish_reason="tool_calls",
            ),
            _final(_review_output()),
        ]
    )
    router = ToolRouter(tracker, _experts())
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=router,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        fixtures_dir=str(FIXTURES),
        current=current,
        thinking_enabled=False,
    )
    statuses = [item["status"] for item in result.skill_calls]
    assert statuses[0] == "success"
    assert statuses[1] == "rejected"


@pytest.mark.asyncio
async def test_later_loop_can_call_another_analyst(tmp_path: Path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    current = _inquiry_current(snaps)
    tracker = FakeLLMAdapter(
        [
            _tool("get_prescreen", '{"dummy":""}', "c1"),
            _tool(
                "call_sentiment",
                _skill_args("问询函是否改变风险感知", "新增问询"),
                "c2",
            ),
            _tool(
                "call_fundamental",
                _skill_args("问询后核对盈利质量", "上一轮情绪不足"),
                "c3",
            ),
            _final(_review_output()),
        ]
    )
    router = ToolRouter(tracker, _experts())
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=router,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        fixtures_dir=str(FIXTURES),
        current=current,
        thinking_enabled=False,
    )
    statuses = [(item["agent"], item["status"]) for item in result.skill_calls]
    assert statuses == [("sentiment", "success"), ("fundamental", "success")]
    first = result.skill_calls[0]
    assert first.get("output")
    assert "role" not in first["output"]
    assert "thesis_impact" in first["output"]
    deliverable = result.deliverable
    assert deliverable["thinking"]
    assert deliverable["synthesis"]
    agents = {item["agent"] for item in deliverable["expert_evaluations"]}
    assert "sentiment" in agents
    assert "fundamental" in agents



@pytest.mark.asyncio
async def test_analyze_does_not_start_tracking(tmp_path: Path):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    await database.initialize()
    fund, tech, sent, macro = (
        ScriptedAgentLLM("fundamental"),
        ScriptedAgentLLM("technical"),
        ScriptedAgentLLM("sentiment"),
        ScriptedAgentLLM("macro"),
    )
    outcome = await analyze_stock(
        "000858",
        database=database,
        llm_adapter=fund,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        fixtures_dir=str(FIXTURES),
        technical_llm_adapter=tech,
        sentiment_llm_adapter=sent,
        macro_llm_adapter=macro,
    )
    assert "tracking" not in outcome.run.session_ids
    assert outcome.run.mode == "research"


def test_track_cli_exists():
    from insightagent.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args(["track", "000858-initial", "--fixture", "--json"])
    assert args.command == "track"
    assert args.thesis_id == "000858-initial"
