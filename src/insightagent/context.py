from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Set
from uuid import uuid4

from .contracts import LLMMessage


class ContextOverflowError(RuntimeError):
    pass


class TokenCounter(Protocol):
    def count_text(self, text: str) -> int:
        ...

    def count_messages(self, messages: List[LLMMessage]) -> int:
        ...


class ArtifactStore(Protocol):
    async def put(self, content: str) -> str:
        ...

    async def get(self, ref: str) -> str:
        ...


class ContextArchive(Protocol):
    async def append(
        self, session_id: str, message: LLMMessage
    ) -> None:
        ...

    async def load(self, session_id: str) -> List[LLMMessage]:
        ...


class CharacterTokenCounter:
    """Portable approximation until a model-specific tokenizer is configured."""

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    def count_messages(self, messages: List[LLMMessage]) -> int:
        total = 0
        for message in messages:
            total += 4
            total += self.count_text(message.content or "")
            total += self.count_text(message.reasoning_content or "")
            if message.tool_calls:
                total += self.count_text(
                    json.dumps(
                        [
                            call.model_dump(mode="json")
                            for call in message.tool_calls
                        ],
                        ensure_ascii=False,
                    )
                )
        return total


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._artifacts: Dict[str, str] = {}

    async def put(self, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        ref = "artifact://{}-{}".format(digest, uuid4().hex[:8])
        self._artifacts[ref] = content
        return ref

    async def get(self, ref: str) -> str:
        return self._artifacts[ref]


class InMemoryContextArchive:
    def __init__(self) -> None:
        self._messages: Dict[str, List[LLMMessage]] = {}

    async def append(self, session_id: str, message: LLMMessage) -> None:
        self._messages.setdefault(session_id, []).append(
            message.model_copy(deep=True)
        )

    async def load(self, session_id: str) -> List[LLMMessage]:
        return [
            message.model_copy(deep=True)
            for message in self._messages.get(session_id, [])
        ]


class ContextBuffer:
    def __init__(
        self,
        session_id: str,
        archive: ContextArchive,
        messages: Optional[List[LLMMessage]] = None,
    ) -> None:
        self.session_id = session_id
        self.archive = archive
        self.messages = list(messages or [])

    @classmethod
    async def load(
        cls, session_id: str, archive: ContextArchive
    ) -> "ContextBuffer":
        return cls(session_id, archive, await archive.load(session_id))

    async def append(self, message: LLMMessage) -> None:
        self.messages.append(message)
        await self.archive.append(self.session_id, message)

    async def append_user(self, content: str, *, priority: int = 100) -> None:
        await self.append(
            LLMMessage(role="user", content=content, priority=priority)
        )

    async def append_assistant(
        self,
        *,
        content: Optional[str],
        reasoning_content: Optional[str] = None,
        tool_calls: Optional[list[Any]] = None,
    ) -> None:
        await self.append(
            LLMMessage(
                role="assistant",
                content=content,
                reasoning_content=reasoning_content,
                tool_calls=tool_calls or [],
                priority=80 if tool_calls else 60,
            )
        )

    async def append_tool(
        self, *, tool_call_id: str, content: str
    ) -> None:
        await self.append(
            LLMMessage(
                role="tool",
                tool_call_id=tool_call_id,
                content=content,
                priority=80,
            )
        )

    def raw_projection(self) -> List[LLMMessage]:
        return [message.model_copy(deep=True) for message in self.messages]


@dataclass(frozen=True)
class ContextConfig:
    context_window: int = 32768
    reserved_output_tokens: int = 4096
    safety_margin_tokens: int = 512
    token_threshold: float = 0.85
    tool_result_budget: int = 2048
    snip_keep_recent: int = 12
    collapse_keep_recent: int = 8
    micro_compact_chars: int = 2000


@dataclass
class CompactionResult:
    messages: List[LLMMessage]
    layers_applied: List[str] = field(default_factory=list)
    estimated_tokens: int = 0


class AutoCompactSummarizer(Protocol):
    async def summarize(self, messages: List[LLMMessage]) -> str:
        ...


class DeterministicSummarizer:
    async def summarize(self, messages: List[LLMMessage]) -> str:
        parts = []
        for message in messages:
            text = (message.content or "").strip()
            if not text:
                continue
            parts.append(
                "{}: {}".format(message.role, _shorten(text, 300))
            )
        return "\n".join(parts)


class ContextCompactor:
    def __init__(
        self,
        *,
        token_counter: Optional[TokenCounter] = None,
        artifact_store: Optional[ArtifactStore] = None,
        summarizer: Optional[AutoCompactSummarizer] = None,
        config: Optional[ContextConfig] = None,
    ) -> None:
        self.token_counter = token_counter or CharacterTokenCounter()
        self.artifact_store = artifact_store or InMemoryArtifactStore()
        self.summarizer = summarizer or DeterministicSummarizer()
        self.config = config or ContextConfig()

    async def compact_before_llm(
        self,
        *,
        context_buffer: ContextBuffer,
        system_prompt: str,
        resource_definitions: List[Dict[str, Any]],
    ) -> CompactionResult:
        projection = context_buffer.raw_projection()
        applied: List[str] = []

        if self._fits(system_prompt, resource_definitions, projection):
            return self._result(projection, applied)

        projection = await self._l0_tool_result_budget(projection)
        applied.append("L0")
        if self._fits(system_prompt, resource_definitions, projection):
            return self._result(projection, applied)

        projection = self._l1_snip(projection)
        applied.append("L1")
        if self._fits(system_prompt, resource_definitions, projection):
            return self._result(projection, applied)

        projection = self._l2_micro_compact(projection)
        applied.append("L2")
        if self._fits(system_prompt, resource_definitions, projection):
            return self._result(projection, applied)

        projection = self._l3_context_collapse(projection)
        applied.append("L3")
        if self._fits(system_prompt, resource_definitions, projection):
            return self._result(projection, applied)

        projection = await self._l4_auto_compact(projection)
        applied.append("L4")
        if self._fits(system_prompt, resource_definitions, projection):
            return self._result(projection, applied)

        raise ContextOverflowError(
            "Context exceeds budget after L0-L4 compaction"
        )

    def _request_tokens(
        self,
        system_prompt: str,
        resource_definitions: List[Dict[str, Any]],
        messages: List[LLMMessage],
    ) -> int:
        return (
            self.token_counter.count_text(system_prompt)
            + self.token_counter.count_text(
                json.dumps(resource_definitions, ensure_ascii=False)
            )
            + self.token_counter.count_messages(messages)
        )

    def _budget(self) -> int:
        available = (
            self.config.context_window
            - self.config.reserved_output_tokens
            - self.config.safety_margin_tokens
        )
        return int(available * self.config.token_threshold)

    def _fits(
        self,
        system_prompt: str,
        resource_definitions: List[Dict[str, Any]],
        messages: List[LLMMessage],
    ) -> bool:
        return (
            self._request_tokens(
                system_prompt, resource_definitions, messages
            )
            <= self._budget()
        )

    def _result(
        self, messages: List[LLMMessage], layers: List[str]
    ) -> CompactionResult:
        return CompactionResult(
            messages=_repair_tool_protocol(messages),
            layers_applied=list(layers),
            estimated_tokens=self.token_counter.count_messages(messages),
        )

    async def _l0_tool_result_budget(
        self, messages: List[LLMMessage]
    ) -> List[LLMMessage]:
        compacted = []
        for message in messages:
            copy = message.model_copy(deep=True)
            if (
                copy.role == "tool"
                and self.token_counter.count_text(copy.content or "")
                > self.config.tool_result_budget
            ):
                original = copy.content or ""
                ref = await self.artifact_store.put(original)
                summary_limit = min(
                    600,
                    max(80, self.config.tool_result_budget * 2),
                )
                summary = copy.metadata.get("summary") or _shorten(
                    original, summary_limit
                )
                copy.content = json.dumps(
                    {
                        "data_ref": ref,
                        "summary": summary,
                        "content_sha256": hashlib.sha256(
                            original.encode("utf-8")
                        ).hexdigest(),
                    },
                    ensure_ascii=False,
                )
                copy.metadata["data_ref"] = ref
                copy.metadata["compacted_by"] = "L0"
            compacted.append(copy)
        return compacted

    def _l1_snip(
        self, messages: List[LLMMessage]
    ) -> List[LLMMessage]:
        if len(messages) <= self.config.snip_keep_recent:
            return messages

        active_from = _active_tool_chain_start(messages)
        keep_indices = set(
            range(
                max(0, len(messages) - self.config.snip_keep_recent),
                len(messages),
            )
        )
        if active_from is not None:
            keep_indices.update(range(active_from, len(messages)))
        keep_indices.update(
            index
            for index, message in enumerate(messages)
            if message.priority >= 90
            and (
                message.metadata.get("pinned", False)
                or index == len(messages) - 1
            )
        )

        return [
            message
            for index, message in enumerate(messages)
            if index in keep_indices
        ]

    def _l2_micro_compact(
        self, messages: List[LLMMessage]
    ) -> List[LLMMessage]:
        compacted = []
        active_from = _active_tool_chain_start(messages)
        for index, message in enumerate(messages):
            copy = message.model_copy(deep=True)
            if (
                copy.role == "tool"
                and (active_from is None or index < active_from)
                and len(copy.content or "") > self.config.micro_compact_chars
            ):
                copy.content = _head_and_tail(
                    copy.content or "", self.config.micro_compact_chars
                )
                copy.metadata["compacted_by"] = "L2"
            compacted.append(copy)
        return compacted

    def _l3_context_collapse(
        self, messages: List[LLMMessage]
    ) -> List[LLMMessage]:
        keep_from = max(0, len(messages) - self.config.collapse_keep_recent)
        active_from = _active_tool_chain_start(messages)
        if active_from is not None:
            keep_from = min(keep_from, active_from)
        if keep_from <= 0:
            return messages

        old = messages[:keep_from]
        collapsed = {
            "message_refs": [message.message_id for message in old],
            "key_messages": [
                {
                    "role": message.role,
                    "content": _shorten(message.content or "", 240),
                }
                for message in old
                if message.content
            ],
        }
        summary = LLMMessage(
            role="user",
            content="Context collapse projection (JSON):\n{}".format(
                json.dumps(collapsed, ensure_ascii=False)
            ),
            priority=85,
            metadata={"compacted_by": "L3"},
        )
        return [summary] + messages[keep_from:]

    async def _l4_auto_compact(
        self, messages: List[LLMMessage]
    ) -> List[LLMMessage]:
        keep_count = min(2, len(messages))
        active_from = _active_tool_chain_start(messages)
        if active_from is not None:
            keep_count = max(keep_count, len(messages) - active_from)
        old = messages[:-keep_count] if keep_count else messages
        recent = messages[-keep_count:] if keep_count else []
        if not old:
            old = recent
            recent = []

        summary = await self.summarizer.summarize(old)
        summary_message = LLMMessage(
            role="user",
            content=(
                "Auto-compact history summary (JSON-compatible text):\n"
                + summary
            ),
            priority=90,
            metadata={
                "compacted_by": "L4",
                "restore_refs": [
                    message.message_id for message in old
                ],
            },
        )
        return [summary_message] + recent


def _repair_tool_protocol(messages: List[LLMMessage]) -> List[LLMMessage]:
    """Drop tool rows whose parent assistant tool_calls were compacted away."""
    open_ids: Set[str] = set()
    repaired: List[LLMMessage] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            open_ids = {call.id for call in message.tool_calls}
            repaired.append(message)
            continue
        if message.role == "tool":
            if message.tool_call_id in open_ids:
                repaired.append(message)
            continue
        open_ids = set()
        repaired.append(message)
    return repaired


def _active_tool_chain_start(messages: List[LLMMessage]) -> Optional[int]:
    last_final_assistant = -1
    for index, message in enumerate(messages):
        if message.role == "assistant" and not message.tool_calls:
            last_final_assistant = index

    for index in range(len(messages) - 1, last_final_assistant, -1):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            return index
    return None


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _head_and_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(1, (limit - 32) // 2)
    return "{}\n...[micro-compact]...\n{}".format(
        text[:half], text[-half:]
    )
