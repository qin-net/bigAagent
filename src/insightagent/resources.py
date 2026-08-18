from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Protocol, Type

from pydantic import BaseModel

from .contracts import (
    ResourceCall,
    ResourceResult,
    ResourceSpec,
    ResourceType,
    SideEffect,
)
from .retry import ExponentialBackoff


class DuplicateResourceError(ValueError):
    pass


class UnknownResourceError(KeyError):
    pass


class CallDependencyError(ValueError):
    pass


class AgentRunnable(Protocol):
    async def run(
        self,
        user_query: str,
        *,
        session_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        business_context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        ...


class Resource(ABC):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        resource_type: ResourceType,
        input_model: Type[BaseModel],
        output_model: Optional[Type[BaseModel]] = None,
        timeout_seconds: float = 30.0,
        retry_policy: str = "default",
        parallel_safe: bool = True,
        side_effect: SideEffect = SideEffect.NONE,
        permission_tags: Optional[Iterable[str]] = None,
        version: str = "1",
    ) -> None:
        self.input_model = input_model
        self.output_model = output_model
        self.spec = ResourceSpec(
            name=name,
            type=resource_type,
            description=description,
            input_schema=input_model.model_json_schema(),
            output_schema=(
                output_model.model_json_schema() if output_model else {}
            ),
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            parallel_safe=parallel_safe,
            side_effect=side_effect,
            permission_tags=list(permission_tags or []),
            version=version,
        )

    @abstractmethod
    async def invoke(
        self, arguments: Dict[str, Any], *, idempotency_key: str
    ) -> Any:
        raise NotImplementedError

    def validate_input(self, arguments: Dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(arguments)

    def validate_output(self, value: Any) -> Any:
        if self.output_model is None:
            return value
        if isinstance(value, self.output_model):
            return value.model_dump(mode="json")
        return self.output_model.model_validate(value).model_dump(mode="json")


class FunctionResource(Resource):
    def __init__(
        self,
        *,
        func: Callable[..., Any],
        name: str,
        description: str,
        input_model: Type[BaseModel],
        output_model: Optional[Type[BaseModel]] = None,
        resource_type: ResourceType = ResourceType.FUNCTION,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            resource_type=resource_type,
            input_model=input_model,
            output_model=output_model,
            **kwargs,
        )
        self.func = func

    async def invoke(
        self, arguments: Dict[str, Any], *, idempotency_key: str
    ) -> Any:
        validated = self.validate_input(arguments)
        value = self.func(**validated.model_dump(mode="python"))
        if inspect.isawaitable(value):
            value = await value
        return self.validate_output(value)


class AgentSkillResource(Resource):
    def __init__(
        self,
        *,
        target_agent: AgentRunnable,
        name: str,
        description: str,
        input_model: Type[BaseModel],
        output_model: Optional[Type[BaseModel]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            resource_type=ResourceType.AGENT_SKILL,
            input_model=input_model,
            output_model=output_model,
            **kwargs,
        )
        self.target_agent = target_agent

    async def invoke(
        self, arguments: Dict[str, Any], *, idempotency_key: str
    ) -> Any:
        validated = self.validate_input(arguments)
        payload = validated.model_dump(mode="python")
        result = await self.target_agent.run(
            user_query=_render_agent_task(payload),
            session_id=payload.get("target_session_id"),
            parent_session_id=payload.get("parent_session_id"),
            business_context={
                "task_prompt": payload,
                "idempotency_key": idempotency_key,
            },
        )
        return self.validate_output(result)


class ResourceRegistry:
    def __init__(self) -> None:
        self._resources: Dict[str, Resource] = {}

    def register(self, resource: Resource) -> None:
        if resource.spec.name in self._resources:
            raise DuplicateResourceError(resource.spec.name)
        self._resources[resource.spec.name] = resource

    def unregister(self, name: str) -> None:
        self._resources.pop(name, None)

    def get(self, name: str) -> Resource:
        try:
            return self._resources[name]
        except KeyError as error:
            raise UnknownResourceError(name) from error

    def list_all(self) -> list[Resource]:
        return list(self._resources.values())

    def get_all_resource_definitions(
        self, *, strict: bool = False
    ) -> list[Dict[str, Any]]:
        return [
            resource.spec.to_deepseek_tool(strict=strict)
            for resource in sorted(
                self._resources.values(), key=lambda item: item.spec.name
            )
        ]


class CallOrchestrator:
    def __init__(
        self,
        registry: ResourceRegistry,
        retry_policies: Optional[Dict[str, ExponentialBackoff]] = None,
        *,
        max_parallel_calls: int = 4,
    ) -> None:
        self.registry = registry
        self.retry_policies = {"default": ExponentialBackoff()}
        self.retry_policies.update(retry_policies or {})
        self._semaphore = asyncio.Semaphore(max_parallel_calls)

    async def dispatch_calls(
        self, calls: list[ResourceCall]
    ) -> Dict[str, ResourceResult]:
        self._validate_graph(calls)
        pending = {call.call_id: call for call in calls}
        results: Dict[str, ResourceResult] = {}

        while pending:
            ready = [
                call
                for call in pending.values()
                if all(dependency in results for dependency in call.depends_on)
            ]
            if not ready:
                raise CallDependencyError("Call graph contains a cycle")

            parallel = [
                call
                for call in ready
                if call.execution == "parallel"
                and self.registry.get(call.resource_name).spec.parallel_safe
            ]
            serial = [call for call in ready if call not in parallel]

            if parallel:
                parallel_results = await _gather_cancel_on_error(
                    [self._execute(call) for call in parallel]
                )
                for call, result in zip(parallel, parallel_results):
                    results[call.call_id] = result
                    pending.pop(call.call_id)

            for call in serial:
                results[call.call_id] = await self._execute(call)
                pending.pop(call.call_id)

        return results

    async def _execute(self, call: ResourceCall) -> ResourceResult:
        resource = self.registry.get(call.resource_name)
        resource.validate_input(call.arguments)
        retry = self.retry_policies.get(
            resource.spec.retry_policy, self.retry_policies["default"]
        )
        started_at = datetime.now(timezone.utc)
        attempts = 0

        async def operation() -> Any:
            nonlocal attempts
            attempts += 1
            async with self._semaphore:
                return await asyncio.wait_for(
                    resource.invoke(
                        call.arguments,
                        idempotency_key=_idempotency_key(call),
                    ),
                    timeout=resource.spec.timeout_seconds,
                )

        data = await retry.execute(operation)
        return ResourceResult(
            call_id=call.call_id,
            resource=call.resource_name,
            status="success",
            data=data,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            attempts=attempts,
        )

    def _validate_graph(self, calls: list[ResourceCall]) -> None:
        call_ids = {call.call_id for call in calls}
        if len(call_ids) != len(calls):
            raise CallDependencyError("Duplicate call_id")
        for call in calls:
            self.registry.get(call.resource_name)
            unknown = set(call.depends_on) - call_ids
            if unknown:
                raise CallDependencyError(
                    "Unknown dependencies for {}: {}".format(
                        call.call_id, sorted(unknown)
                    )
                )
            if call.call_id in call.depends_on:
                raise CallDependencyError("Call cannot depend on itself")


async def _gather_cancel_on_error(
    awaitables: list[Awaitable[ResourceResult]],
) -> list[ResourceResult]:
    tasks = [asyncio.create_task(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _idempotency_key(call: ResourceCall) -> str:
    return "{}:{}".format(call.resource_name, call.call_id)


def _render_agent_task(payload: Dict[str, Any]) -> str:
    questions = "\n".join(
        "- {}".format(question)
        for question in payload.get("required_questions", [])
    )
    return (
        "Task ID: {task_id}\n"
        "Objective: {objective}\n"
        "Reason: {reason}\n"
        "Required questions:\n{questions}"
    ).format(
        task_id=payload.get("task_id", ""),
        objective=payload.get("objective", ""),
        reason=payload.get("reason", ""),
        questions=questions or "- None",
    )
