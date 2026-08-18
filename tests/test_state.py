import pytest

from insightagent.contracts import StatePatch, TaskStatus
from insightagent.state import InMemoryStateStore, StateConflictError


@pytest.mark.asyncio
async def test_state_is_versioned_and_patch_isolated():
    store = InMemoryStateStore()
    state = await store.load_or_create(
        agent_name="fundamental",
        session_id="session-a",
        stock_code="000858",
        thesis_id="thesis-1",
    )

    state.status = TaskStatus.RUNNING
    state = await store.save(state, expected_version=0)
    assert state.version == 1

    patched = await store.apply_patch(
        state.session_id,
        StatePatch(
            base_version=1,
            set={"private_memory.memory_summary": "cash flow is healthy"},
            append={"private_memory.open_questions": ["check margin"]},
        ),
    )

    assert patched.version == 2
    assert patched.private_memory["memory_summary"] == "cash flow is healthy"
    assert patched.private_memory["open_questions"] == ["check margin"]

    with pytest.raises(StateConflictError):
        await store.apply_patch(
            state.session_id,
            StatePatch(
                base_version=1,
                set={"private_memory.memory_summary": "stale overwrite"},
            ),
        )

    history = await store.history(state.session_id)
    assert [item.version for item in history] == [0, 1, 2]
