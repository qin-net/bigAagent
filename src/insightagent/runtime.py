from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .context import (
    ContextArchive,
    ContextBuffer,
    ContextCompactor,
    InMemoryContextArchive,
)
from .contracts import (
    AgentFinalResponse,
    AgentState,
    LLMMessage,
    LLMRequest,
    ResourceCall,
    TaskStatus,
)
from .llm import LLMAdapter
from .resources import (
    AgentSkillResource,
    CallOrchestrator,
    FunctionResource,
    Resource,
    ResourceRegistry,
)
from .retry import ExponentialBackoff
from .state import InMemoryStateStore, StateConflictError, StateStore


class MaxLoopRoundExceeded(RuntimeError):
    pass


class OutputTruncatedError(RuntimeError):
    pass


class ContentFilteredError(RuntimeError):
    pass


class InvalidModelOutputError(ValueError):
    pass


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class LoopTracer:
    """Records scheduler stage payloads for tests and debugging."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, stage: str, payload: Any = None) -> None:
        self.events.append({"stage": stage, "payload": _jsonable(payload)})

    def stages(self) -> List[str]:
        return [event["stage"] for event in self.events]


@dataclass(frozen=True)
class RuntimeConfig:
    model: str = "deepseek-v4-pro"
    system_prompt: str = "You are a careful agent."
    thinking_enabled: bool = True
    reasoning_effort: str = "high"
    response_format: str = "json"
    max_tokens: int = 4096
    max_loop_round: int = 15
    max_parallel_calls: int = 4
    strict_tools: bool = False
    user_id: Optional[str] = None


class AgentLocalScheduler:
    def __init__(
        self,
        *,
        agent_name: str,
        state_store: StateStore,
        resource_registry: ResourceRegistry,
        compactor: ContextCompactor,
        llm_retry: ExponentialBackoff,
        orchestrator: CallOrchestrator,
        llm_adapter: LLMAdapter,
        config: RuntimeConfig,
    ) -> None:
        self.agent_name = agent_name
        self.state_store = state_store
        self.resources = resource_registry
        self.compactor = compactor
        self.llm_retry = llm_retry
        self.orchestrator = orchestrator
        self.llm = llm_adapter
        self.config = config
        self.tracer: Optional[LoopTracer] = None

    async def run_agent_loop(
        self,
        *,
        state: AgentState,
        user_input: str,
        context_buffer: ContextBuffer,
    ) -> AgentFinalResponse:
        state.status = TaskStatus.RUNNING
        state.loop_round = 0
        state = await self.state_store.save(
            state, expected_version=state.version
        )
        await context_buffer.append_user(user_input)
        self._trace(
            "loop_start",
            {
                "agent_name": self.agent_name,
                "session_id": state.session_id,
                "version": state.version,
                "user_input": user_input,
            },
        )

        try:
            while (
                state.status == TaskStatus.RUNNING
                and state.loop_round < self.config.max_loop_round
            ):
                resource_definitions = (
                    self.resources.get_all_resource_definitions(
                        strict=self.config.strict_tools
                    )
                )
                system_prompt = self._system_prompt(state)
                compacted = await self.compactor.compact_before_llm(
                    context_buffer=context_buffer,
                    system_prompt=system_prompt,
                    resource_definitions=resource_definitions,
                )
                request = LLMRequest(
                    model=self.config.model,
                    messages=[
                        LLMMessage(
                            role="system",
                            content=system_prompt,
                            priority=100,
                        )
                    ]
                    + compacted.messages,
                    tools=resource_definitions,
                    thinking_enabled=self.config.thinking_enabled,
                    reasoning_effort=self.config.reasoning_effort,
                    response_format=self.config.response_format,
                    max_tokens=self.config.max_tokens,
                    user_id=self.config.user_id,
                )
                self._trace(
                    "llm_request",
                    {
                        "loop_round": state.loop_round,
                        "state_version": state.version,
                        "tool_names": [
                            item.get("function", {}).get("name")
                            for item in resource_definitions
                        ],
                        "messages": request.messages,
                    },
                )

                response = await self.llm_retry.execute(
                    self.llm.complete, request
                )
                self._trace(
                    "llm_response",
                    {
                        "finish_reason": response.finish_reason,
                        "content": response.content,
                        "tool_calls": response.tool_calls,
                    },
                )
                await context_buffer.append_assistant(
                    content=response.content,
                    reasoning_content=response.reasoning_content,
                    tool_calls=response.tool_calls,
                )

                if response.finish_reason == "tool_calls":
                    if not response.tool_calls:
                        raise InvalidModelOutputError(
                            "finish_reason=tool_calls without tool calls"
                        )
                    calls = self._parse_tool_calls(response.tool_calls)
                    self._trace("scheduler_dispatch", calls)
                    results = await self.orchestrator.dispatch_calls(calls)
                    self._trace(
                        "tool_results",
                        {call_id: result for call_id, result in results.items()},
                    )
                    for call in response.tool_calls:
                        result = results[call.id]
                        await context_buffer.append_tool(
                            tool_call_id=call.id,
                            content=json.dumps(
                                result.model_dump(mode="json"),
                                ensure_ascii=False,
                                default=str,
                            ),
                        )
                    state.loop_round += 1
                    state.checkpoint = self._checkpoint(
                        state, context_buffer
                    )
                    state = await self.state_store.save(
                        state, expected_version=state.version
                    )
                    self._trace(
                        "state_checkpoint",
                        {
                            "loop_round": state.loop_round,
                            "version": state.version,
                        },
                    )
                    continue

                if response.finish_reason == "stop":
                    final = self._parse_final_response(
                        response.content, state
                    )
                    self._trace("final_response", final)
                    state = await self.state_store.apply_patch(
                        state.session_id, final.state_patch
                    )
                    state.status = TaskStatus.SUCCESS
                    state.checkpoint = self._checkpoint(
                        state, context_buffer
                    )
                    await self.state_store.save(
                        state, expected_version=state.version
                    )
                    self._trace(
                        "loop_complete",
                        {
                            "status": state.status.value,
                            "version": state.version,
                        },
                    )
                    return final

                if response.finish_reason == "length":
                    raise OutputTruncatedError(
                        "DeepSeek output was truncated"
                    )
                if response.finish_reason == "content_filter":
                    raise ContentFilteredError(
                        "DeepSeek filtered the response"
                    )
                raise InvalidModelOutputError(
                    "Unhandled finish_reason: {}".format(
                        response.finish_reason
                    )
                )

            raise MaxLoopRoundExceeded(
                "Agent loop reached max_loop_round={}".format(
                    self.config.max_loop_round
                )
            )
        except Exception as error:
            await self._mark_failed(state.session_id, error)
            raise

    def _trace(self, stage: str, payload: Any = None) -> None:
        if self.tracer is not None:
            self.tracer.emit(stage, payload)

    def _system_prompt(self, state: AgentState) -> str:
        schema = AgentFinalResponse.model_json_schema()
        return (
            "{base}\n\n"
            "Return JSON for final answers. Follow this JSON schema exactly:\n"
            "{schema}\n\n"
            "Current AgentState version is {version}; state_patch.base_version "
            "must equal it. Do not expose hidden reasoning_content."
        ).format(
            base=self.config.system_prompt,
            schema=json.dumps(schema, ensure_ascii=False),
            version=state.version,
        )

    @staticmethod
    def _parse_tool_calls(tool_calls: list[Any]) -> list[ResourceCall]:
        parsed = []
        execution = "parallel" if len(tool_calls) > 1 else "serial"
        for call in tool_calls:
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError as error:
                raise InvalidModelOutputError(
                    "Invalid JSON arguments for {}".format(call.name)
                ) from error
            if not isinstance(arguments, dict):
                raise InvalidModelOutputError(
                    "Tool arguments must be a JSON object"
                )
            parsed.append(
                ResourceCall(
                    call_id=call.id,
                    resource_name=call.name,
                    arguments=arguments,
                    execution=execution,
                )
            )
        return parsed

    @staticmethod
    def _parse_final_response(
        content: Optional[str], state: AgentState
    ) -> AgentFinalResponse:
        if not content or not content.strip():
            raise InvalidModelOutputError("Model returned empty JSON content")
        try:
            final = AgentFinalResponse.model_validate_json(content)
        except ValidationError as error:
            raise InvalidModelOutputError(
                "Final response failed schema validation"
            ) from error
        if final.state_patch.base_version != state.version:
            raise StateConflictError(
                "Final response patch uses version {}, expected {}".format(
                    final.state_patch.base_version, state.version
                )
            )
        return final

    @staticmethod
    def _checkpoint(
        state: AgentState, context_buffer: ContextBuffer
    ) -> Dict[str, Any]:
        return {
            "loop_round": state.loop_round,
            "message_ids": [
                message.message_id for message in context_buffer.messages
            ],
        }

    async def _mark_failed(
        self, session_id: str, error: Exception
    ) -> None:
        try:
            current = await self.state_store.get(session_id)
            current.status = TaskStatus.FAILED
            current.business_context["failure_reason"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            await self.state_store.save(
                current, expected_version=current.version
            )
        except StateConflictError:
            return


class AgentInstance:
    def __init__(
        self,
        *,
        name: str,
        llm_adapter: LLMAdapter,
        config: Optional[RuntimeConfig] = None,
        state_store: Optional[StateStore] = None,
        context_archive: Optional[ContextArchive] = None,
        compactor: Optional[ContextCompactor] = None,
        llm_retry: Optional[ExponentialBackoff] = None,
        resource_retry_policies: Optional[
            Dict[str, ExponentialBackoff]
        ] = None,
        tracer: Optional[LoopTracer] = None,
    ) -> None:
        self.name = name
        self.config = config or RuntimeConfig()
        self.state_store = state_store or InMemoryStateStore()
        self.context_archive = (
            context_archive or InMemoryContextArchive()
        )
        self.resource_registry = ResourceRegistry()
        self.compactor = compactor or ContextCompactor()
        self.llm_retry = llm_retry or ExponentialBackoff()
        self.orchestrator = CallOrchestrator(
            self.resource_registry,
            resource_retry_policies,
            max_parallel_calls=self.config.max_parallel_calls,
        )
        self.scheduler = AgentLocalScheduler(
            agent_name=name,
            state_store=self.state_store,
            resource_registry=self.resource_registry,
            compactor=self.compactor,
            llm_retry=self.llm_retry,
            orchestrator=self.orchestrator,
            llm_adapter=llm_adapter,
            config=self.config,
        )
        self.scheduler.tracer = tracer

    def register_resource(self, resource: Resource) -> None:
        self.resource_registry.register(resource)

    def register_tool(self, tool: FunctionResource) -> None:
        self.register_resource(tool)

    def register_agent_as_skill(
        self, skill: AgentSkillResource
    ) -> None:
        self.register_resource(skill)

    async def run(
        self,
        user_query: str,
        *,
        session_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        business_context: Optional[Dict[str, Any]] = None,
    ) -> AgentFinalResponse:
        business_context = business_context or {}
        state = await self.state_store.load_or_create(
            agent_name=self.name,
            session_id=session_id,
            parent_session_id=parent_session_id,
            stock_code=business_context.get("stock_code"),
            thesis_id=business_context.get("thesis_id"),
            business_context=business_context,
        )
        if business_context and state.business_context != business_context:
            state.business_context.update(business_context)
            state = await self.state_store.save(
                state, expected_version=state.version
            )
        context = await ContextBuffer.load(
            state.session_id, self.context_archive
        )
        return await self.scheduler.run_agent_loop(
            state=state,
            user_input=user_query,
            context_buffer=context,
        )
