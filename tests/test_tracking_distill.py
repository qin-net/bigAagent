from __future__ import annotations

import json
from pathlib import Path

import pytest

from insightagent.contracts import LLMResponse, LLMToolCall
from insightagent.llm import FakeLLMAdapter
from insightagent.methodology import MethodologyCatalog
from insightagent.persistence import SQLiteDatabase
from insightagent.tracking_agent import distill_chapter

CHAPTER = (
    Path(__file__).resolve().parent / "fixtures" / "kb" / "cashflow-chapter.md"
)


def _distill_llm(path: Path) -> FakeLLMAdapter:
    submit_args = json.dumps(
        {
            "id": "kb_distill_cashflow",
            "title": "利润要看现金流",
            "type": "rule",
            "trigger": "现金流 净利润",
            "action": "核对滞后是否季节性",
            "evidence_required": ["cashflow_lag"],
            "exceptions": [],
            "source_refs": [],
            "text": "利润好看时要核对经营现金流是否同步。",
            "priority": 50,
        }
    )
    final_args = json.dumps(
        {
            "status": "completed",
            "output": {"notes": "submitted one card"},
            "reflection": {
                "what_worked": ["mapped flags"],
                "what_was_missing": [],
                "process_errors": [],
            },
            "state_patch": {"set": [], "append": [], "remove": []},
        }
    )
    return FakeLLMAdapter(
        [
            LLMResponse(
                id="t1",
                model="deepseek-v4-flash",
                content="read",
                tool_calls=[
                    LLMToolCall(
                        id="c1",
                        name="read_source_markdown",
                        arguments=json.dumps({"path": str(path)}),
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="t2",
                model="deepseek-v4-flash",
                content="flags",
                tool_calls=[
                    LLMToolCall(
                        id="c2",
                        name="list_allowed_flags",
                        arguments='{"dummy":""}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="t3",
                model="deepseek-v4-flash",
                content="submit",
                tool_calls=[
                    LLMToolCall(
                        id="c3",
                        name="submit_candidate",
                        arguments=submit_args,
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="t4",
                model="deepseek-v4-flash",
                content="done",
                tool_calls=[
                    LLMToolCall(
                        id="c4",
                        name="submit_final",
                        arguments=final_args,
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_tracking_distill_writes_candidate(tmp_path: Path):
    database = SQLiteDatabase(str(tmp_path / "kb.db"))
    result = await distill_chapter(
        str(CHAPTER),
        scope="fundamental",
        database=database,
        llm_adapter=_distill_llm(CHAPTER),
        roots=[CHAPTER.parent],
    )
    assert result.submitted_ids == ["kb_distill_cashflow"]
    catalog = MethodologyCatalog(database)
    item = catalog.get("kb_distill_cashflow")
    assert item["status"] == "candidate"
    hits = catalog.search(
        "现金流", scope="fundamental", flags=["cashflow_lag"]
    )
    assert "kb_distill_cashflow" not in [item["id"] for item in hits]


@pytest.mark.asyncio
async def test_submit_candidate_rejects_unknown_flag(tmp_path: Path):
    database = SQLiteDatabase(str(tmp_path / "kb.db"))
    await database.initialize()
    catalog = MethodologyCatalog(database)
    catalog.ensure_seeded()
    with pytest.raises(ValueError):
        catalog.submit_candidate(
            {
                "id": "kb_bad",
                "title": "bad",
                "scope": ["fundamental"],
                "trigger": "x",
                "evidence_required": ["not_a_flag"],
                "text": "short",
            }
        )
    with pytest.raises(ValueError):
        catalog.submit_candidate(
            {
                "id": "kb_cashflow_lag",
                "title": "dup",
                "scope": ["fundamental"],
                "trigger": "现金流",
                "evidence_required": ["cashflow_lag"],
                "text": "overwrite",
            }
        )
