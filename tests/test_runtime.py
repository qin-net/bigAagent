import json

import pytest
from pydantic import BaseModel

from insightagent.contracts import (
    LLMResponse,
    LLMToolCall,
    TaskStatus,
)
from insightagent.llm import FakeLLMAdapter
from insightagent.resources import FunctionResource
from insightagent.runtime import AgentInstance, LoopTracer, RuntimeConfig


class LookupInput(BaseModel):
    stock_code: str


class LookupOutput(BaseModel):
    roe: float


@pytest.mark.asyncio
async def test_agent_loop_calls_tool_preserves_reasoning_and_updates_state():
    final_json = json.dumps(
        {
            "status": "completed",
            "output": {"summary": "ROE is healthy"},
            "reflection": {"what_worked": ["checked source data"]},
            "state_patch": {
                "set": {
                    "private_memory.memory_summary": "ROE checked"
                },
                "append": {
                    "private_memory.key_evidence_refs": ["roe-2025"]
                },
                "remove": {},
            },
        }
    )
    fake = FakeLLMAdapter(
        [
            LLMResponse(
                id="response-1",
                model="deepseek-v4-pro",
                content="I will inspect ROE.",
                reasoning_content="ROE is required for this task.",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="lookup_financial",
                        arguments='{"stock_code":"000858"}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="response-2",
                model="deepseek-v4-pro",
                content=final_json,
                reasoning_content="The evidence supports the conclusion.",
                finish_reason="stop",
            ),
        ]
    )
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=fake,
        config=RuntimeConfig(
            system_prompt="Analyze fundamentals.",
            max_loop_round=4,
        ),
    )
    agent.register_tool(
        FunctionResource(
            func=lambda stock_code: {"roe": 18.2},
            name="lookup_financial",
            description="Read financial indicators",
            input_model=LookupInput,
            output_model=LookupOutput,
        )
    )

    result = await agent.run(
        "Analyze 000858",
        session_id="session-1",
        business_context={
            "stock_code": "000858",
            "thesis_id": "thesis-1",
        },
    )

    assert result.output["summary"] == "ROE is healthy"
    state = await agent.state_store.get("session-1")
    assert state.status == TaskStatus.SUCCESS
    assert state.private_memory["memory_summary"] == "ROE checked"

    second_request = fake.requests[1]
    tool_call_message = next(
        message
        for message in second_request.messages
        if message.role == "assistant" and message.tool_calls
    )
    assert (
        tool_call_message.reasoning_content
        == "ROE is required for this task."
    )
    assert any(
        message.role == "tool"
        and message.tool_call_id == "call-1"
        for message in second_request.messages
    )


@pytest.mark.asyncio
async def test_model_schema_omits_runtime_counters():
    fake = FakeLLMAdapter(
        [
            LLMResponse(
                id="final",
                model="deepseek-v4-pro",
                content=json.dumps(
                    {
                        "status": "completed",
                        "output": {"summary": "ok"},
                        "reflection": {},
                        "state_patch": {"set": {}, "append": {}, "remove": {}},
                    }
                ),
                finish_reason="stop",
            )
        ]
    )
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=fake,
        config=RuntimeConfig(max_loop_round=2),
    )
    await agent.run("Analyze 000858", session_id="session-schema")
    prompt = fake.requests[0].messages[0].content
    assert prompt is not None
    assert "submit_final" in prompt
    assert "the scheduler increments rounds" in prompt
    submit = next(
        item
        for item in fake.requests[0].tools
        if item["function"]["name"] == "submit_final"
    )
    assert submit["function"]["strict"] is True
    parameters = json.dumps(submit["function"]["parameters"])
    assert '"base_version"' not in parameters
    assert '"loop_round"' not in parameters


def _tool_response(call_id: str) -> LLMResponse:
    return LLMResponse(
        id=call_id,
        model="deepseek-v4-pro",
        content="inspecting",
        tool_calls=[
            LLMToolCall(
                id=call_id,
                name="lookup_financial",
                arguments='{"stock_code":"000858"}',
            )
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_scheduler_ignores_model_runtime_counters():
    final_json = json.dumps(
        {
            "status": "completed",
            "output": {"summary": "stamped"},
            "reflection": {},
            "state_patch": {
                "base_version": 3,
                "loop_round": 99,
                "set": {
                    "private_memory.memory_summary": "used live version"
                },
                "append": {},
                "remove": {},
            },
        }
    )
    fake = FakeLLMAdapter(
        [
            _tool_response("call-1"),
            _tool_response("call-2"),
            _tool_response("call-3"),
            LLMResponse(
                id="final",
                model="deepseek-v4-pro",
                content=final_json,
                finish_reason="stop",
            ),
        ]
    )
    tracer = LoopTracer()
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=fake,
        config=RuntimeConfig(max_loop_round=6),
        tracer=tracer,
    )
    agent.register_tool(
        FunctionResource(
            func=lambda stock_code: {"roe": 18.2},
            name="lookup_financial",
            description="Read financial indicators",
            input_model=LookupInput,
            output_model=LookupOutput,
        )
    )

    result = await agent.run("Analyze 000858", session_id="session-stale")
    state = await agent.state_store.get("session-stale")

    assert result.state_patch.base_version == 4
    assert state.status == TaskStatus.SUCCESS
    assert state.private_memory["memory_summary"] == "used live version"
    assert {
        "model_base_version": 3,
        "live_version": 4,
    } in [
        event["payload"]
        for event in tracer.events
        if event["stage"] == "state_patch_version_ignored"
    ]


@pytest.mark.asyncio
async def test_submit_final_tool_stamps_version_and_completes():
    payload = {
        "status": "completed",
        "output": {
            "report": {
                "role": "fundamental",
                "score": 3,
                "stance": "hold",
                "summary": "done",
                "citations": [
                    {
                        "ref_id": "roe",
                        "kind": "field",
                        "id": "roe",
                        "source": "lookup_financial",
                    }
                ],
                "risks": ["sample"],
            }
        },
        "reflection": {
            "what_worked": ["tools"],
            "what_was_missing": [],
            "process_errors": [],
        },
        "state_patch": {
            "base_version": 1,
            "set": [
                {
                    "path": "private_memory.memory_summary",
                    "value": "from submit_final",
                }
            ],
            "append": [],
            "remove": [],
        },
    }
    fake = FakeLLMAdapter(
        [
            _tool_response("call-1"),
            LLMResponse(
                id="final",
                model="deepseek-v4-pro",
                content="",
                tool_calls=[
                    LLMToolCall(
                        id="submit-1",
                        name="submit_final",
                        arguments=json.dumps(payload),
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=fake,
        config=RuntimeConfig(max_loop_round=4),
    )
    agent.register_tool(
        FunctionResource(
            func=lambda stock_code: {"roe": 18.2},
            name="lookup_financial",
            description="Read financial indicators",
            input_model=LookupInput,
            output_model=LookupOutput,
        )
    )
    result = await agent.run("Analyze 000858", session_id="session-submit")
    state = await agent.state_store.get("session-submit")
    assert result.output["report"]["summary"] == "done"
    assert result.state_patch.base_version == 2
    assert state.status == TaskStatus.SUCCESS
    assert state.private_memory["memory_summary"] == "from submit_final"


@pytest.mark.asyncio
async def test_submit_final_drops_illegal_state_paths_and_keeps_report():
    payload = {
        "status": "abstained",
        "output": {
            "report": {
                "role": "macro",
                "score": 2,
                "stance": "abstain",
                "summary": "利率环境与白酒相关性低，本维弃权。",
                "citations": [],
                "risks": ["宏观相关性低"],
                "degraded": True,
                "abstain": True,
                "cycle_tag": "rate_data_available",
                "relevance_to_stock": "low",
            }
        },
        "reflection": {},
        "state_patch": {
            "set": [
                {"path": "macro.stance", "value": "abstain"},
                {"path": "macro.relevance_to_stock", "value": "low"},
                {"path": "private_memory.memory_summary", "value": "low relevance"},
            ],
            "append": [],
            "remove": [],
        },
    }
    fake = FakeLLMAdapter(
        [
            LLMResponse(
                id="final",
                model="deepseek-v4-flash",
                content="",
                tool_calls=[
                    LLMToolCall(
                        id="submit-1",
                        name="submit_final",
                        arguments=json.dumps(payload),
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    tracer = LoopTracer()
    agent = AgentInstance(
        name="macro",
        llm_adapter=fake,
        config=RuntimeConfig(max_loop_round=4),
        tracer=tracer,
    )
    result = await agent.run("Analyze 000858", session_id="session-macro-patch")
    state = await agent.state_store.get("session-macro-patch")
    assert result.output["report"]["stance"] == "abstain"
    assert result.output["report"]["relevance_to_stock"] == "low"
    assert state.status == TaskStatus.SUCCESS
    assert state.private_memory["memory_summary"] == "low relevance"
    assert "macro" not in state.private_memory
    dropped = [
        event["payload"]["paths"]
        for event in tracer.events
        if event["stage"] == "state_patch_paths_dropped"
    ]
    assert dropped
    assert "macro.stance" in dropped[0]
    assert "macro.relevance_to_stock" in dropped[0]


@pytest.mark.parametrize(
    ("tool_rounds", "claimed_version"),
    [
        (0, 0),
        (1, 0),
        (2, 1),
        (3, 3),
        (4, 99),
    ],
)
@pytest.mark.asyncio
async def test_scheduler_owns_loop_round_and_version(tool_rounds, claimed_version):
    responses = [_tool_response("call-{}".format(index)) for index in range(tool_rounds)]
    responses.append(
        LLMResponse(
            id="final",
            model="deepseek-v4-pro",
            content=json.dumps(
                {
                    "status": "completed",
                    "output": {"summary": "ok"},
                    "reflection": {},
                    "state_patch": {
                        "base_version": claimed_version,
                        "loop_round": 1000,
                        "set": {},
                        "append": {},
                        "remove": {},
                    },
                }
            ),
            finish_reason="stop",
        )
    )
    fake = FakeLLMAdapter(responses)
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=fake,
        config=RuntimeConfig(max_loop_round=8),
    )
    if tool_rounds:
        _register_lookup(agent)

    result = await agent.run(
        "Analyze 000858", session_id="session-owned-{}".format(tool_rounds)
    )
    state = await agent.state_store.get("session-owned-{}".format(tool_rounds))

    assert state.status == TaskStatus.SUCCESS
    assert state.loop_round == tool_rounds
    assert result.state_patch.base_version == 1 + tool_rounds


def _register_lookup(agent: AgentInstance) -> None:
    agent.register_tool(
        FunctionResource(
            func=lambda stock_code: {"roe": 18.2},
            name="lookup_financial",
            description="Read financial indicators",
            input_model=LookupInput,
            output_model=LookupOutput,
        )
    )


@pytest.mark.asyncio
async def test_invalid_final_json_retries_instead_of_failing():
    valid = {
        "status": "completed",
        "output": {"summary": "recovered"},
        "reflection": {},
        "state_patch": {"set": {}, "append": {}, "remove": {}},
    }
    fake = FakeLLMAdapter(
        [
            LLMResponse(
                id="bad",
                model="deepseek-v4-pro",
                content="not-json",
                finish_reason="stop",
            ),
            LLMResponse(
                id="good",
                model="deepseek-v4-pro",
                content=json.dumps(valid),
                finish_reason="stop",
            ),
        ]
    )
    tracer = LoopTracer()
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=fake,
        config=RuntimeConfig(max_loop_round=4),
        tracer=tracer,
    )
    result = await agent.run("Analyze 000858", session_id="session-retry")
    assert result.output["summary"] == "recovered"
    assert "output_rejected" in tracer.stages()


@pytest.mark.asyncio
async def test_unknown_tool_name_is_returned_and_loop_continues():
    valid = {
        "status": "completed",
        "output": {"summary": "recovered after unknown tool"},
        "reflection": {},
        "state_patch": {"set": {}, "append": {}, "remove": {}},
    }
    fake = FakeLLMAdapter(
        [
            LLMResponse(
                id="bad-tool",
                model="deepseek-v4-pro",
                content="",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="search_methoduality",
                        arguments='{"query":"cashflow"}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="good",
                model="deepseek-v4-pro",
                content=json.dumps(valid),
                finish_reason="stop",
            ),
        ]
    )
    tracer = LoopTracer()
    agent = AgentInstance(
        name="fundamental",
        llm_adapter=fake,
        config=RuntimeConfig(max_loop_round=4),
        tracer=tracer,
    )
    _register_lookup(agent)
    result = await agent.run("Analyze 000858", session_id="session-unknown-tool")
    assert result.output["summary"] == "recovered after unknown tool"
    tool_results = [
        event["payload"]
        for event in tracer.events
        if event["stage"] == "tool_results"
    ]
    assert tool_results
    failed = tool_results[0]["call-1"]
    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "UnknownResourceError"
