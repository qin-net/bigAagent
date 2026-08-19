from types import SimpleNamespace

import pytest

from insightagent.contracts import (
    LLMMessage,
    LLMRequest,
    LLMToolCall,
)
from insightagent.llm import DEEPSEEK_BETA_URL, DeepSeekChatAdapter, DeepSeekConfig
from insightagent.retry import RetryableProviderError


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.payload = None

    async def create(self, **payload):
        self.payload = payload
        return self.response


class FakeClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(response)
        )


def raw_response(finish_reason="stop"):
    return SimpleNamespace(
        id="resp-1",
        model="deepseek-v4-pro",
        system_fingerprint="fp",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    content='{"ok":true}',
                    reasoning_content="private protocol reasoning",
                    tool_calls=None,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_cache_hit_tokens=8,
            prompt_cache_miss_tokens=2,
            completion_tokens_details=SimpleNamespace(
                reasoning_tokens=3
            ),
        ),
    )


def test_deepseek_defaults_to_beta_url_for_strict_tools():
    assert DeepSeekConfig(api_key="test").base_url == DEEPSEEK_BETA_URL


@pytest.mark.asyncio
async def test_deepseek_adapter_serializes_reasoning_and_usage():
    client = FakeClient(raw_response())
    adapter = DeepSeekChatAdapter(
        DeepSeekConfig(api_key="test"), client=client
    )
    request = LLMRequest(
        model="deepseek-v4-pro",
        messages=[
            LLMMessage(
                role="assistant",
                content="calling",
                reasoning_content="must be returned",
                tool_calls=[
                    LLMToolCall(
                        id="call-1", name="lookup", arguments="{}"
                    )
                ],
            )
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
        ],
        thinking_enabled=True,
        response_format="json",
    )

    response = await adapter.complete(request)
    payload = client.chat.completions.payload

    assert payload["extra_body"]["thinking"]["type"] == "enabled"
    assert payload["reasoning_effort"] == "high"
    assert (
        payload["messages"][0]["reasoning_content"]
        == "must be returned"
    )
    assert payload["response_format"] == {"type": "json_object"}
    assert response.usage.prompt_cache_hit_tokens == 8
    assert response.usage.reasoning_tokens == 3


@pytest.mark.asyncio
async def test_insufficient_system_resource_is_retryable():
    client = FakeClient(raw_response("insufficient_system_resource"))
    adapter = DeepSeekChatAdapter(
        DeepSeekConfig(api_key="test"), client=client
    )

    with pytest.raises(RetryableProviderError):
        await adapter.complete(
            LLMRequest(model="deepseek-v4-pro", messages=[])
        )
