from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from .business_contracts import Report
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
    ModelFinalResponse,
    ModelStatePatch,
    ResourceCall,
    StatePatch,
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
from .schema import to_strict_json_schema
from .state import (
    InMemoryStateStore,
    StateConflictError,
    StateStore,
    drop_immutable_patch_paths,
)


class MaxLoopRoundExceeded(RuntimeError):
    pass


class OutputTruncatedError(RuntimeError):
    pass


class ContentFilteredError(RuntimeError):
    pass


class InvalidModelOutputError(ValueError):
    pass


SUBMIT_FINAL = "submit_final"


class SubmitFinalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: Report


class ModelReflection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what_worked: List[str] = Field(default_factory=list)
    what_was_missing: List[str] = Field(default_factory=list)
    process_errors: List[str] = Field(default_factory=list)


class PathValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    value: str = ""


class PathValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    values: List[str] = Field(default_factory=list)


class SubmitStatePatch(BaseModel):
    """Model-facing patch. Arrays stay DeepSeek-strict; dicts do not."""

    model_config = ConfigDict(extra="forbid")

    set: List[PathValue] = Field(default_factory=list)
    append: List[PathValues] = Field(default_factory=list)
    remove: List[str] = Field(default_factory=list)


class SubmitFinalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "abstained", "degraded"] = "completed"
    output: SubmitFinalOutput
    reflection: ModelReflection = Field(default_factory=ModelReflection)
    state_patch: SubmitStatePatch = Field(default_factory=SubmitStatePatch)


def _submit_args_for(output_model: Type[BaseModel]) -> Type[BaseModel]:
    class Forbidden(BaseModel):
        model_config = ConfigDict(extra="forbid")

    return create_model(
        "SubmitFinalArgsCustom",
        __base__=Forbidden,
        status=(Literal["completed", "abstained", "degraded"], "completed"),
        output=(output_model, ...),
        reflection=(ModelReflection, Field(default_factory=ModelReflection)),
        state_patch=(SubmitStatePatch, Field(default_factory=SubmitStatePatch)),
    )


def _submit_final_tool(
    *, strict: bool, args_model: Type[BaseModel] = SubmitFinalArgs
) -> Dict[str, Any]:
    schema = args_model.model_json_schema()
    if strict:
        schema = to_strict_json_schema(schema)
    function: Dict[str, Any] = {
        "name": SUBMIT_FINAL,
        "description": (
            "Submit the finished analysis. output.report is required and "
            "must include role, score, stance, and summary. "
            "The scheduler stamps state versions and increments loop "
            "rounds; do not send those fields. state_patch paths may only "
            "start with private_memory, business_context, meta, or "
            "checkpoint. Report fields such as stance do not go there."
        ),
        "parameters": schema,
    }
    if strict:
        function["strict"] = True
    return {"type": "function", "function": function}


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
    strict_tools: bool = True
    user_id: Optional[str] = None
    final_output_model: Type[BaseModel] = SubmitFinalOutput


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
        self._submit_args = (
            SubmitFinalArgs
            if config.final_output_model is SubmitFinalOutput
            else _submit_args_for(config.final_output_model)
        )

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
                resource_definitions = self._tool_definitions()
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
                    response_format=(
                        "text"
                        if self.config.strict_tools
                        else self.config.response_format
                    ),
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
                    state, final = await self._handle_tool_calls(
                        response, state, context_buffer
                    )
                    if final is not None:
                        return final
                    continue

                if response.finish_reason == "stop":
                    try:
                        final = self._parse_final_response(
                            response.content, state
                        )
                    except InvalidModelOutputError as error:
                        state = await self._retry_invalid_output(
                            state, context_buffer, error
                        )
                        continue
                    return await self._complete_success(
                        state, context_buffer, final
                    )

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

    def _tool_definitions(self) -> list[Dict[str, Any]]:
        definitions = self.resources.get_all_resource_definitions(
            strict=self.config.strict_tools
        )
        if self.config.strict_tools:
            definitions.append(
                _submit_final_tool(
                    strict=True, args_model=self._submit_args
                )
            )
            definitions.sort(
                key=lambda item: item.get("function", {}).get("name", "")
            )
        return definitions

    async def _handle_tool_calls(
        self,
        response: Any,
        state: AgentState,
        context_buffer: ContextBuffer,
    ) -> tuple:
        if not response.tool_calls:
            state = await self._retry_invalid_output(
                state,
                context_buffer,
                InvalidModelOutputError(
                    "finish_reason=tool_calls without tool calls"
                ),
            )
            return state, None
        try:
            calls = self._parse_tool_calls(response.tool_calls)
        except InvalidModelOutputError as error:
            for call in response.tool_calls:
                await context_buffer.append_tool(
                    tool_call_id=call.id,
                    content=json.dumps(
                        {"status": "failed", "error": str(error)}
                    ),
                )
            state = await self._retry_invalid_output(
                state, context_buffer, error
            )
            return state, None

        final_calls = [
            call for call in calls if call.resource_name == SUBMIT_FINAL
        ]
        work_calls = [
            call for call in calls if call.resource_name != SUBMIT_FINAL
        ]
        if work_calls:
            self._trace("scheduler_dispatch", work_calls)
            results = await self.orchestrator.dispatch_calls(work_calls)
            self._trace(
                "tool_results",
                {call_id: result for call_id, result in results.items()},
            )
            for call in response.tool_calls:
                if call.name == SUBMIT_FINAL:
                    await context_buffer.append_tool(
                        tool_call_id=call.id,
                        content=json.dumps(
                            {
                                "status": "ignored",
                                "error": (
                                    "submit_final cannot mix with other tools"
                                ),
                            }
                        ),
                    )
                    continue
                result = results[call.id]
                await context_buffer.append_tool(
                    tool_call_id=call.id,
                    content=json.dumps(
                        result.model_dump(mode="json"),
                        ensure_ascii=False,
                        default=str,
                    ),
                )
            state = await self._checkpoint_round(state, context_buffer)
            return state, None

        if not final_calls:
            state = await self._retry_invalid_output(
                state,
                context_buffer,
                InvalidModelOutputError("No dispatchable tool calls"),
            )
            return state, None

        try:
            final = self._parse_final_payload(
                final_calls[0].arguments, state, require_report=True
            )
        except InvalidModelOutputError as error:
            for call in response.tool_calls:
                await context_buffer.append_tool(
                    tool_call_id=call.id,
                    content=json.dumps(
                        {"status": "failed", "error": str(error)}
                    ),
                )
            state = await self._retry_invalid_output(
                state, context_buffer, error
            )
            return state, None

        for call in response.tool_calls:
            await context_buffer.append_tool(
                tool_call_id=call.id,
                content=json.dumps({"status": "accepted"}),
            )
        final = await self._complete_success(state, context_buffer, final)
        return state, final

    async def _complete_success(
        self,
        state: AgentState,
        context_buffer: ContextBuffer,
        final: AgentFinalResponse,
    ) -> AgentFinalResponse:
        self._trace("final_response", final)
        state = await self.state_store.apply_patch(
            state.session_id, final.state_patch
        )
        state.status = TaskStatus.SUCCESS
        state.checkpoint = self._checkpoint(state, context_buffer)
        await self.state_store.save(state, expected_version=state.version)
        self._trace(
            "loop_complete",
            {
                "status": state.status.value,
                "version": state.version,
            },
        )
        return final

    async def _checkpoint_round(
        self, state: AgentState, context_buffer: ContextBuffer
    ) -> AgentState:
        state.loop_round += 1
        state.checkpoint = self._checkpoint(state, context_buffer)
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
        return state

    async def _retry_invalid_output(
        self,
        state: AgentState,
        context_buffer: ContextBuffer,
        error: Exception,
    ) -> AgentState:
        self._trace(
            "output_rejected",
            {"type": type(error).__name__, "message": str(error)},
        )
        await context_buffer.append_user(
            "Your previous final output was rejected: {}. "
            "Call submit_final with a valid payload. "
            "Do not include base_version, loop_round, or version; "
            "the scheduler owns those counters.".format(error)
        )
        return await self._checkpoint_round(state, context_buffer)

    def _trace(self, stage: str, payload: Any = None) -> None:
        if self.tracer is not None:
            self.tracer.emit(stage, payload)

    def _system_prompt(self, _state: AgentState) -> str:
        if self.config.strict_tools:
            return (
                "{base}\n\n"
                "Call tools as needed. Finish by calling submit_final. "
                "That tool's JSON Schema is the only allowed final payload. "
                "Do not put the final answer in message content. "
                "Do not include base_version, loop_round, or version; "
                "the scheduler increments rounds and stamps state versions. "
                "Do not expose hidden reasoning_content."
            ).format(base=self.config.system_prompt)
        schema = ModelFinalResponse.model_json_schema()
        return (
            "{base}\n\n"
            "Return JSON for final answers. Follow this JSON schema exactly:\n"
            "{schema}\n\n"
            "Do not include state versions, loop rounds, or other runtime "
            "counters. The scheduler owns those. Do not expose hidden "
            "reasoning_content."
        ).format(
            base=self.config.system_prompt,
            schema=json.dumps(schema, ensure_ascii=False),
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
                    "Invalid JSON arguments for {}. "
                    "submit_final needs one JSON object, no duplicate keys, "
                    "and output.report must include role, score, and stance.".format(
                        call.name
                    )
                    if call.name == "submit_final"
                    else "Invalid JSON arguments for {}".format(call.name)
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

    _MODEL_COUNTER_KEYS = ("base_version", "loop_round", "version")

    def _parse_final_response(
        self, content: Optional[str], state: AgentState
    ) -> AgentFinalResponse:
        if not content or not content.strip():
            raise InvalidModelOutputError("Model returned empty JSON content")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise InvalidModelOutputError(
                "Final response is not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise InvalidModelOutputError(
                "Final response must be a JSON object"
            )
        return self._parse_final_payload(payload, state)

    def _parse_final_payload(
        self,
        payload: Dict[str, Any],
        state: AgentState,
        *,
        require_report: bool = False,
    ) -> AgentFinalResponse:
        claimed = None
        patch = payload.get("state_patch")
        if isinstance(patch, dict):
            claimed = patch.get("base_version")
            for key in self._MODEL_COUNTER_KEYS:
                patch.pop(key, None)
        payload.pop("loop_round", None)
        payload.pop("base_version", None)

        output: Dict[str, Any]
        try:
            submitted = self._submit_args.model_validate(payload)
            output = submitted.output.model_dump(
                mode="json", exclude_none=True
            )
            reflection = submitted.reflection.model_dump(mode="json")
            status = submitted.status
            patch_body = ModelStatePatch(
                set={item.path: item.value for item in submitted.state_patch.set},
                append={
                    item.path: item.values
                    for item in submitted.state_patch.append
                },
                remove={name: [] for name in submitted.state_patch.remove},
            )
        except ValidationError as error:
            if require_report:
                raise InvalidModelOutputError(
                    "submit_final.output.report is required and must "
                    "include role, score, and stance"
                ) from error
            try:
                visible = ModelFinalResponse.model_validate(payload)
            except ValidationError as fallback_error:
                raise InvalidModelOutputError(
                    "Final response failed schema validation"
                ) from fallback_error
            output = visible.output
            reflection = visible.reflection
            status = visible.status
            patch_body = visible.state_patch

        if claimed is not None and claimed != state.version:
            self._trace(
                "state_patch_version_ignored",
                {
                    "model_base_version": claimed,
                    "live_version": state.version,
                },
            )

        built = StatePatch(
            base_version=state.version,
            set=patch_body.set,
            append=patch_body.append,
            remove=patch_body.remove,
        )
        cleaned, dropped = drop_immutable_patch_paths(built)
        if dropped:
            self._trace(
                "state_patch_paths_dropped",
                {"paths": dropped},
            )

        return AgentFinalResponse(
            status=status,
            output=output,
            reflection=reflection,
            state_patch=cleaned,
        )

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
