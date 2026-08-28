import pytest

from insightagent.context import (
    ContextBuffer,
    ContextCompactor,
    ContextConfig,
    InMemoryArtifactStore,
    InMemoryContextArchive,
)
from insightagent.contracts import LLMMessage, LLMToolCall


@pytest.mark.asyncio
async def test_l0_externalizes_large_tool_result():
    archive = InMemoryContextArchive()
    buffer = ContextBuffer("session", archive)
    await buffer.append_user("analyze")
    await buffer.append(
        LLMMessage(
            role="assistant",
            content="calling",
            tool_calls=[
                LLMToolCall(id="call-1", name="lookup", arguments="{}")
            ],
            priority=80,
        )
    )
    await buffer.append_tool(tool_call_id="call-1", content="x" * 4000)

    artifacts = InMemoryArtifactStore()
    compactor = ContextCompactor(
        artifact_store=artifacts,
        config=ContextConfig(
            context_window=220,
            reserved_output_tokens=20,
            safety_margin_tokens=0,
            token_threshold=1.0,
            tool_result_budget=20,
        ),
    )

    result = await compactor.compact_before_llm(
        context_buffer=buffer,
        system_prompt="system",
        resource_definitions=[],
    )

    assert result.layers_applied == ["L0"]
    tool_message = next(
        message for message in result.messages if message.role == "tool"
    )
    assert "artifact://" in tool_message.content
    ref = tool_message.metadata["data_ref"]
    assert await artifacts.get(ref) == "x" * 4000


class TinySummarizer:
    async def summarize(self, messages):
        return "compact history"


@pytest.mark.asyncio
async def test_compactor_escalates_through_l4():
    archive = InMemoryContextArchive()
    buffer = ContextBuffer("session", archive)
    for index in range(20):
        await buffer.append(
            LLMMessage(
                role="user",
                content="message-{}-{}".format(index, "x" * 30),
                priority=100,
                metadata={"pinned": True},
            )
        )

    compactor = ContextCompactor(
        summarizer=TinySummarizer(),
        config=ContextConfig(
            context_window=80,
            reserved_output_tokens=0,
            safety_margin_tokens=0,
            token_threshold=1.0,
            snip_keep_recent=4,
            collapse_keep_recent=4,
        ),
    )
    result = await compactor.compact_before_llm(
        context_buffer=buffer,
        system_prompt="system",
        resource_definitions=[],
    )

    assert result.layers_applied == ["L0", "L1", "L2", "L3", "L4"]
    assert result.messages[0].metadata["compacted_by"] == "L4"


@pytest.mark.asyncio
async def test_active_tool_reasoning_is_preserved_during_compaction():
    archive = InMemoryContextArchive()
    buffer = ContextBuffer("session", archive)
    for index in range(15):
        await buffer.append(
            LLMMessage(
                role="user",
                content="old {}".format(index) + "x" * 80,
                priority=10,
            )
        )
    await buffer.append(
        LLMMessage(
            role="assistant",
            content="calling tool",
            reasoning_content="protocol-required reasoning",
            tool_calls=[
                LLMToolCall(
                    id="call-1", name="lookup", arguments="{}"
                )
            ],
            priority=80,
        )
    )
    await buffer.append_tool(tool_call_id="call-1", content="result")

    compactor = ContextCompactor(
        config=ContextConfig(
            context_window=120,
            reserved_output_tokens=0,
            safety_margin_tokens=0,
            token_threshold=1.0,
            snip_keep_recent=3,
        )
    )
    result = await compactor.compact_before_llm(
        context_buffer=buffer,
        system_prompt="system",
        resource_definitions=[],
    )

    assistant = next(
        message
        for message in result.messages
        if message.role == "assistant" and message.tool_calls
    )
    assert assistant.reasoning_content == "protocol-required reasoning"


@pytest.mark.asyncio
async def test_l4_does_not_leave_orphan_tool_messages():
    archive = InMemoryContextArchive()
    buffer = ContextBuffer("session", archive)
    for index in range(12):
        await buffer.append(
            LLMMessage(
                role="user",
                content="old {}".format(index) + "y" * 80,
                priority=10,
            )
        )
    await buffer.append(
        LLMMessage(
            role="assistant",
            content="calling",
            tool_calls=[
                LLMToolCall(id="c1", name="lookup", arguments="{}")
            ],
            priority=80,
        )
    )
    await buffer.append_tool(tool_call_id="c1", content="z" * 200)
    await buffer.append_tool(tool_call_id="c1", content="w" * 200)

    compactor = ContextCompactor(
        summarizer=TinySummarizer(),
        config=ContextConfig(
            context_window=90,
            reserved_output_tokens=0,
            safety_margin_tokens=0,
            token_threshold=1.0,
            snip_keep_recent=2,
            collapse_keep_recent=2,
        ),
    )
    result = await compactor.compact_before_llm(
        context_buffer=buffer,
        system_prompt="system",
        resource_definitions=[],
    )
    roles = [message.role for message in result.messages]
    for index, role in enumerate(roles):
        if role != "tool":
            continue
        assert index > 0
        previous = result.messages[index - 1]
        assert previous.role in {"assistant", "tool"}
        if previous.role == "assistant":
            assert previous.tool_calls

