from __future__ import annotations

import json

import pytest

from insightagent.contracts import LLMResponse, LLMToolCall
from insightagent.llm import FakeLLMAdapter
from insightagent.methodology import MethodologyCatalog
from insightagent.research_store import ResearchStore
from insightagent.tracking_agent import track_thesis

from tests.llm_recording import RecordingLLM
from tests.test_tracking_agent import (
    ToolRouter,
    _final,
    _inquiry_current,
    _instance_for_schema,
    _output_schema_from_request,
    _review_output,
    _seed_baseline,
    _skill_args,
)


def _artifact_ref_from_request(request) -> str:
    for message in reversed(request.messages):
        if message.role != "tool" or not message.content:
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        data = payload.get("data") or payload
        if isinstance(data, dict):
            ref = str(data.get("artifact_ref") or "")
            if ref.startswith("artifact://"):
                return ref
    return ""


class ScriptedFullExpertLLM:
    """Hits every tool this analyst owns, then submits the tracker schema."""

    ROLE_QUEUE = {
        "fundamental": [
            ("get_fundamental_snapshot", "{}"),
            ("search_methodology", json.dumps({"query": "现金流"})),
        ],
        "technical": [
            ("get_indicator_snapshot", "{}"),
            ("get_price_snapshot", "{}"),
            ("get_kline_snapshot", "{}"),
            ("search_methodology", json.dumps({"query": "量能"})),
        ],
        "sentiment": [
            ("get_event_snapshot", "{}"),
            ("get_holder_changes", "{}"),
            ("search_methodology", json.dumps({"query": "问询"})),
        ],
        "macro": [
            ("get_macro_snapshot", "{}"),
            ("search_methodology", json.dumps({"query": "LPR"})),
        ],
    }

    def __init__(self, role: str) -> None:
        self.role = role
        self.queue = list(self.ROLE_QUEUE[role])
        self.artifact_checked = False

    async def complete(self, request):
        if self.role == "fundamental" and not self.artifact_checked:
            ref = _artifact_ref_from_request(request)
            if ref:
                self.queue.insert(
                    0, ("get_artifact", json.dumps({"ref": ref}))
                )
                self.artifact_checked = True
            elif any(message.role == "tool" for message in request.messages):
                self.artifact_checked = True
        if self.queue:
            name, arguments = self.queue.pop(0)
            return LLMResponse(
                id="tool-{}".format(name),
                model="fake",
                content=name,
                tool_calls=[
                    LLMToolCall(id=name, name=name, arguments=arguments)
                ],
                finish_reason="tool_calls",
            )
        schema = _output_schema_from_request(request)
        payload = {
            "status": "completed",
            "output": _instance_for_schema(schema),
            "reflection": {
                "what_worked": ["used every snapshot tool"],
                "what_was_missing": [],
                "process_errors": [],
            },
            "state_patch": {"set": [], "append": [], "remove": []},
        }
        return LLMResponse(
            id="final-{}".format(self.role),
            model="fake",
            content=json.dumps(payload, ensure_ascii=False),
            finish_reason="stop",
        )


def _candidate_args() -> str:
    return json.dumps(
        {
            "id": "kb_track_full_chain",
            "title": "跟踪核对现金流",
            "type": "rule",
            "trigger": "现金流",
            "action": "对照纪律",
            "evidence_required": ["cashflow_lag"],
            "exceptions": [],
            "source_refs": [],
            "text": "跟踪时若利润仍好看，要再对一下经营现金流。",
            "priority": 50,
        },
        ensure_ascii=False,
    )


def _batch(*calls: tuple[str, str]) -> LLMResponse:
    tool_calls = [
        LLMToolCall(id="b{}".format(index), name=name, arguments=arguments)
        for index, (name, arguments) in enumerate(calls, start=1)
    ]
    return LLMResponse(
        id="batch",
        model="fake",
        content="batch",
        tool_calls=tool_calls,
        finish_reason="tool_calls",
    )


def _call(agent: str, question: str, reason: str, call_id: str) -> LLMResponse:
    return LLMResponse(
        id=call_id,
        model="fake",
        content=agent,
        tool_calls=[
            LLMToolCall(
                id=call_id,
                name="call_{}".format(agent),
                arguments=_skill_args(question, reason),
            )
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_track_full_chain_hits_every_tool(tmp_path):
    database, snaps, thesis_id = await _seed_baseline(tmp_path)
    packed = await ResearchStore(database).get_baseline_run(thesis_id)
    fund_ref = packed["run"]["snapshot_refs"]["fundamental"]
    current = _inquiry_current(snaps)
    current.fundamental.artifact_ref = fund_ref

    tracker = FakeLLMAdapter(
        [
            _batch(
                ("get_tracking_context", '{"dummy":""}'),
                ("get_prescreen", '{"dummy":""}'),
                (
                    "search_methodology",
                    json.dumps({"query": "问询", "scope": "sentiment"}),
                ),
                ("submit_candidate", _candidate_args()),
            ),
            _call("sentiment", "问询函是否改变风险感知", "新增问询", "c-s"),
            _call("fundamental", "核对盈利质量", "问询后看现金流", "c-f"),
            _call("technical", "量价有没有失控", "排除技术误报", "c-t"),
            _call("macro", "宏观是否同向恶化", "排除环境误报", "c-m"),
            _final(_review_output()),
        ]
    )
    experts = {
        "sentiment": ScriptedFullExpertLLM("sentiment"),
        "fundamental": ScriptedFullExpertLLM("fundamental"),
        "technical": ScriptedFullExpertLLM("technical"),
        "macro": ScriptedFullExpertLLM("macro"),
    }
    rec = RecordingLLM(ToolRouter(tracker, experts))
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=rec,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=True,
        current=current,
        thinking_enabled=False,
    )

    tracker_names = rec.names_when("get_tracking_context")
    for name in (
        "get_tracking_context",
        "get_prescreen",
        "search_methodology",
        "submit_candidate",
        "call_sentiment",
        "call_fundamental",
        "call_technical",
        "call_macro",
        "submit_final",
    ):
        assert name in tracker_names, name

    assert "get_event_snapshot" in rec.names_when("get_event_snapshot")
    assert "get_holder_changes" in rec.names_when("get_holder_changes")
    assert "get_fundamental_snapshot" in rec.names_when("get_fundamental_snapshot")
    assert "get_artifact" in rec.names_when("get_artifact")
    assert "get_indicator_snapshot" in rec.names_when("get_indicator_snapshot")
    assert "get_price_snapshot" in rec.names_when("get_price_snapshot")
    assert "get_kline_snapshot" in rec.names_when("get_kline_snapshot")
    assert "get_macro_snapshot" in rec.names_when("get_macro_snapshot")
    assert rec.names_when("get_fundamental_snapshot").count("search_methodology") >= 1
    assert rec.names_when("get_event_snapshot").count("search_methodology") >= 1
    assert rec.names_when("get_indicator_snapshot").count("search_methodology") >= 1
    assert rec.names_when("get_macro_snapshot").count("search_methodology") >= 1

    statuses = [(item["agent"], item["status"]) for item in result.skill_calls]
    assert statuses == [
        ("sentiment", "success"),
        ("fundamental", "success"),
        ("technical", "success"),
        ("macro", "success"),
    ]
    for item in result.skill_calls:
        output = item.get("output") or {}
        assert "thesis_impact" in output
        assert "role" not in output

    for payload in rec.call_args:
        if not str(payload["name"]).startswith("call_"):
            continue
        schema_text = payload["arguments"].get("output_schema")
        assert isinstance(schema_text, str)
        schema = json.loads(schema_text)
        assert schema["type"] == "object"

    catalog = MethodologyCatalog(database)
    stored = catalog.get("kb_track_full_chain")
    assert stored["status"] == "candidate"
    assert result.deliverable["status"] == "review"
    eval_agents = {item["agent"] for item in result.deliverable["expert_evaluations"]}
    assert eval_agents == {"sentiment", "fundamental", "technical", "macro"}
    assert result.deliverable["thinking"]
    assert result.deliverable["synthesis"]
    timeline = await ResearchStore(database).list_timeline(thesis_id)
    assert timeline
