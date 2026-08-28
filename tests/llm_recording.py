from __future__ import annotations

import json
from typing import Any, List

from insightagent.contracts import LLMRequest, LLMResponse


class RecordingLLM:
    """Wraps an adapter and records every tool name the model asked to call."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: List[LLMRequest] = []
        self.per_request_calls: List[List[str]] = []
        self.call_args: List[dict] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        response = await self.inner.complete(request)
        names: List[str] = []
        for item in response.tool_calls or []:
            names.append(item.name)
            try:
                payload = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            self.call_args.append({"name": item.name, "arguments": payload})
        self.per_request_calls.append(names)
        return response

    @property
    def calls(self) -> List[str]:
        return [name for group in self.per_request_calls for name in group]

    def names_when(self, tool_in_registry: str) -> List[str]:
        names: List[str] = []
        for request, called in zip(self.requests, self.per_request_calls):
            registry = {
                (tool.get("function") or {}).get("name")
                for tool in request.tools or []
            }
            if tool_in_registry in registry:
                names.extend(called)
        return names
