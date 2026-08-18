from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from openai import AsyncOpenAI

from .contracts import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
)
from .retry import RetryableProviderError


class LLMAdapter(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        ...


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    default_model: str = "deepseek-v4-pro"


class DeepSeekChatAdapter:
    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        client: Optional[AsyncOpenAI] = None,
    ) -> None:
        self.config = config
        self.client = client or AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": request.model or self.config.default_model,
            "messages": [
                self._serialize_message(message)
                for message in request.messages
            ],
            "max_tokens": request.max_tokens,
            "stream": False,
            "extra_body": {
                "thinking": {
                    "type": (
                        "enabled"
                        if request.thinking_enabled
                        else "disabled"
                    )
                }
            },
        }

        if request.thinking_enabled:
            payload["reasoning_effort"] = request.reasoning_effort
        if request.tools:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if request.user_id:
            payload["user_id"] = request.user_id

        raw = await self.client.chat.completions.create(**payload)
        response = self._normalize(raw)
        if response.finish_reason == "insufficient_system_resource":
            raise RetryableProviderError(
                "DeepSeek reported insufficient system resources"
            )
        return response

    @staticmethod
    def _serialize_message(message: LLMMessage) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        if message.reasoning_content is not None:
            data["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            data["tool_call_id"] = message.tool_call_id
        return data

    @staticmethod
    def _normalize(raw: Any) -> LLMResponse:
        choice = raw.choices[0]
        message = choice.message
        tool_calls = [
            LLMToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in (getattr(message, "tool_calls", None) or [])
        ]
        usage = getattr(raw, "usage", None)
        completion_details = (
            getattr(usage, "completion_tokens_details", None)
            if usage
            else None
        )

        return LLMResponse(
            id=raw.id,
            model=raw.model,
            content=getattr(message, "content", None),
            reasoning_content=getattr(
                message, "reasoning_content", None
            ),
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=LLMUsage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(
                    usage, "completion_tokens", 0
                )
                or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
                prompt_cache_hit_tokens=getattr(
                    usage, "prompt_cache_hit_tokens", 0
                )
                or 0,
                prompt_cache_miss_tokens=getattr(
                    usage, "prompt_cache_miss_tokens", 0
                )
                or 0,
                reasoning_tokens=getattr(
                    completion_details, "reasoning_tokens", 0
                )
                or 0,
            ),
            system_fingerprint=getattr(
                raw, "system_fingerprint", None
            ),
        )


class FakeLLMAdapter:
    def __init__(self, responses: List[LLMResponse]) -> None:
        self.responses = list(responses)
        self.requests: List[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request.model_copy(deep=True))
        if not self.responses:
            raise RuntimeError("FakeLLMAdapter has no queued response")
        return self.responses.pop(0)
