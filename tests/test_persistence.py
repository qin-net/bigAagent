import json
from pathlib import Path

import pytest

from insightagent.context import ContextBuffer
from insightagent.contracts import (
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    StatePatch,
    TaskStatus,
)
from insightagent.llm import FakeLLMAdapter
from insightagent.persistence import (
    ArtifactIntegrityError,
    FileArtifactStore,
    SCHEMA_VERSION,
    SQLiteAuditLog,
    SQLiteContextArchive,
    SQLiteDatabase,
    SQLiteStateStore,
)
from insightagent.resources import FunctionResource
from insightagent.runtime import AgentInstance, RuntimeConfig
from insightagent.state import StateConflictError
from pydantic import BaseModel


@pytest.mark.asyncio
async def test_database_init_is_idempotent_and_reports_pragmas(tmp_path):
    path = tmp_path / "insightagent.db"
    database = SQLiteDatabase(str(path))

    await database.initialize()
    await database.initialize()
    status = await SQLiteDatabase(str(path)).status()

    assert status["schema_version"] == SCHEMA_VERSION
    assert status["journal_mode"] == "wal"
    assert status["tables_ok"] is True
    assert status["missing_tables"] == []


@pytest.mark.asyncio
async def test_sqlite_state_persists_history_and_detects_conflict(tmp_path):
    path = tmp_path / "insightagent.db"
    store = SQLiteStateStore(SQLiteDatabase(str(path)))
    state = await store.load_or_create(
        agent_name="fundamental",
        session_id="persistent-session",
        stock_code="000858",
        thesis_id="thesis-1",
    )
    state.status = TaskStatus.RUNNING
    state = await store.save(state, expected_version=0)
    state = await store.apply_patch(
        state.session_id,
        StatePatch(
            base_version=1,
            set={"private_memory.memory_summary": "persisted"},
        ),
    )

    reopened = SQLiteStateStore(SQLiteDatabase(str(path)))
    loaded = await reopened.get(state.session_id)
    assert loaded.version == 2
    assert loaded.private_memory["memory_summary"] == "persisted"
    assert [item.version for item in await reopened.history(state.session_id)] == [
        0,
        1,
        2,
    ]

    with pytest.raises(StateConflictError):
        await reopened.apply_patch(
            state.session_id,
            StatePatch(
                base_version=1,
                set={"private_memory.memory_summary": "stale"},
            ),
        )


@pytest.mark.asyncio
async def test_context_strips_reasoning_and_collapses_tool_protocol(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    state_store = SQLiteStateStore(database)
    await state_store.load_or_create(
        agent_name="fundamental", session_id="context-session"
    )
    archive = SQLiteContextArchive(database)
    buffer = ContextBuffer("context-session", archive)

    await buffer.append_user("analyze")
    await buffer.append(
        LLMMessage(
            role="assistant",
            content="calling",
            reasoning_content="must not persist",
            tool_calls=[
                LLMToolCall(id="call-1", name="lookup", arguments="{}")
            ],
        )
    )
    await buffer.append_tool(tool_call_id="call-1", content='{"roe":18}')
    await buffer.append(
        LLMMessage(role="assistant", content="analysis complete")
    )

    raw = await SQLiteContextArchive(
        SQLiteDatabase(str(database.path))
    ).load_raw("context-session")
    assert all(message.reasoning_content is None for message in raw)
    assert any(message.tool_calls for message in raw)

    replay = await archive.load("context-session")
    assert not any(message.tool_calls for message in replay)
    assert any(
        message.metadata.get("replay_projection")
        == "collapsed_tool_chain"
        for message in replay
    )


@pytest.mark.asyncio
async def test_artifact_deduplicates_and_detects_tampering(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    root = tmp_path / "artifacts"
    artifacts = FileArtifactStore(database, str(root))

    first = await artifacts.put("large financial payload")
    second = await artifacts.put("large financial payload")
    assert first == second
    assert await artifacts.get(first) == "large financial payload"

    digest = first.split("artifact://", 1)[1]
    path = root / digest[:2] / "{}.txt".format(digest)
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        await artifacts.get(first)


@pytest.mark.asyncio
async def test_audit_redacts_secrets_and_reasoning(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    state_store = SQLiteStateStore(database)
    await state_store.load_or_create(
        agent_name="tracking", session_id="audit-session"
    )
    audit = SQLiteAuditLog(database)
    await audit.append(
        "agent_completed",
        {
            "api_key": "secret",
            "reasoning_content": "hidden",
            "safe": {"result": 42},
        },
        session_id="audit-session",
    )

    events = await audit.list(session_id="audit-session")
    assert events[0]["payload"]["api_key"] == "[REDACTED]"
    assert events[0]["payload"]["reasoning_content"] == "[REDACTED]"
    assert events[0]["payload"]["safe"] == {"result": 42}


class LookupInput(BaseModel):
    stock_code: str


class LookupOutput(BaseModel):
    roe: float


@pytest.mark.asyncio
async def test_runtime_recovers_from_sqlite_with_safe_context(tmp_path):
    final_first = json.dumps(
        {
            "status": "completed",
            "output": {"summary": "first"},
            "reflection": {},
            "state_patch": {
                "set": {},
                "append": {},
                "remove": {},
            },
        }
    )
    first_llm = FakeLLMAdapter(
        [
            LLMResponse(
                id="one",
                model="deepseek-v4-flash",
                content="calling",
                reasoning_content="protocol reasoning",
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="lookup",
                        arguments='{"stock_code":"000858"}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="two",
                model="deepseek-v4-flash",
                content=final_first,
                finish_reason="stop",
            ),
        ]
    )
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    archive = SQLiteContextArchive(database)
    store = SQLiteStateStore(database)
    first_agent = AgentInstance(
        name="fundamental",
        llm_adapter=first_llm,
        state_store=store,
        context_archive=archive,
        config=RuntimeConfig(max_loop_round=4),
    )
    first_agent.register_tool(
        FunctionResource(
            func=lambda stock_code: {"roe": 18.0},
            name="lookup",
            description="lookup",
            input_model=LookupInput,
            output_model=LookupOutput,
        )
    )
    await first_agent.run(
        "first run",
        session_id="runtime-session",
        business_context={"stock_code": "000858"},
    )

    final_second = json.dumps(
        {
            "status": "completed",
            "output": {"summary": "second"},
            "reflection": {},
            "state_patch": {
                "set": {},
                "append": {},
                "remove": {},
            },
        }
    )
    second_llm = FakeLLMAdapter(
        [
            LLMResponse(
                id="three",
                model="deepseek-v4-flash",
                content=final_second,
                finish_reason="stop",
            )
        ]
    )
    second_agent = AgentInstance(
        name="fundamental",
        llm_adapter=second_llm,
        state_store=SQLiteStateStore(SQLiteDatabase(str(database.path))),
        context_archive=SQLiteContextArchive(
            SQLiteDatabase(str(database.path))
        ),
        config=RuntimeConfig(max_loop_round=2),
    )
    result = await second_agent.run(
        "second run", session_id="runtime-session"
    )

    assert result.output == {"summary": "second"}
    request = second_llm.requests[0]
    assert not any(
        message.role == "assistant" and message.tool_calls
        for message in request.messages
    )
