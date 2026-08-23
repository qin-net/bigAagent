import asyncio

import pytest
from pydantic import BaseModel

from insightagent.contracts import ResourceCall
from insightagent.resources import (
    CallDependencyError,
    CallOrchestrator,
    FunctionResource,
    ResourceRegistry,
)


class NumberInput(BaseModel):
    value: int


class NumberOutput(BaseModel):
    doubled: int


@pytest.mark.asyncio
async def test_parallel_resource_calls_run_concurrently():
    active = 0
    max_active = 0

    async def double(value):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"doubled": value * 2}

    registry = ResourceRegistry()
    registry.register(
        FunctionResource(
            func=double,
            name="double",
            description="Double a number",
            input_model=NumberInput,
            output_model=NumberOutput,
        )
    )
    orchestrator = CallOrchestrator(registry, max_parallel_calls=2)

    results = await orchestrator.dispatch_calls(
        [
            ResourceCall(
                call_id="a",
                resource_name="double",
                arguments={"value": 2},
                execution="parallel",
            ),
            ResourceCall(
                call_id="b",
                resource_name="double",
                arguments={"value": 4},
                execution="parallel",
            ),
        ]
    )

    assert max_active == 2
    assert results["a"].data == {"doubled": 4}
    assert results["b"].data == {"doubled": 8}


@pytest.mark.asyncio
async def test_call_dependency_cycle_is_rejected():
    registry = ResourceRegistry()
    registry.register(
        FunctionResource(
            func=lambda value: {"doubled": value * 2},
            name="double",
            description="Double a number",
            input_model=NumberInput,
            output_model=NumberOutput,
        )
    )
    orchestrator = CallOrchestrator(registry)

    with pytest.raises(CallDependencyError):
        await orchestrator.dispatch_calls(
            [
                ResourceCall(
                    call_id="a",
                    resource_name="double",
                    arguments={"value": 1},
                    depends_on=["b"],
                ),
                ResourceCall(
                    call_id="b",
                    resource_name="double",
                    arguments={"value": 2},
                    depends_on=["a"],
                ),
            ]
        )


@pytest.mark.asyncio
async def test_unknown_resource_returns_failed_result():
    registry = ResourceRegistry()
    orchestrator = CallOrchestrator(registry)
    results = await orchestrator.dispatch_calls(
        [
            ResourceCall(
                call_id="a",
                resource_name="search_methoduality",
                arguments={"query": "x"},
            )
        ]
    )
    assert results["a"].status == "failed"
    assert results["a"].error["type"] == "UnknownResourceError"
    assert "search_methoduality" in results["a"].error["message"]


@pytest.mark.asyncio
async def test_invalid_artifact_ref_returns_failed_result():
    from insightagent.artifact_access import ArtifactArgs, ArtifactOutput

    async def get_artifact(ref: str):
        return {"ref": ref, "content": "secret"}

    registry = ResourceRegistry()
    registry.register(
        FunctionResource(
            func=get_artifact,
            name="get_artifact",
            description="Load artifact",
            input_model=ArtifactArgs,
            output_model=ArtifactOutput,
        )
    )
    orchestrator = CallOrchestrator(registry)
    results = await orchestrator.dispatch_calls(
        [
            ResourceCall(
                call_id="a",
                resource_name="get_artifact",
                arguments={"ref": "000858-8b2582a854b6"},
            )
        ]
    )
    assert results["a"].status == "failed"
    assert results["a"].error["type"] == "ValidationError"
    assert results["a"].data is None

