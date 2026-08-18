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
from insightagent.runtime import AgentInstance, RuntimeConfig


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
                "base_version": 2,
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
