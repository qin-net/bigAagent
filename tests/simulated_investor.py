"""Simulate a cashflow-disciplined user through analyze → remember → re-analyze → track."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from insightagent.contracts import LLMResponse, LLMToolCall
from insightagent.fundamental_agent import default_fixtures_dir
from insightagent.llm import FakeLLMAdapter
from insightagent.persistence import SQLiteDatabase, SQLiteStateStore
from insightagent.tracking_agent import track_thesis
from insightagent.user_contracts import NONE
from insightagent.user_store import UserStore
from insightagent.workflows.initial_research import analyze_stock, feedback_on_run
from insightboard.api import create_app
from insightboard.contracts import QuoteInput
from insightboard.store import BoardStore
from tests.test_p0_research import ScriptedAgentLLM
from tests.test_user_intent import _extract_llm, _slots_payload
from tests.test_tracking_agent import _unchanged_output

FIXTURES = default_fixtures_dir()

PERSONA = {
    "title": "现金流纪律型投资者",
    "decision": "没有经营现金流对照证据不重仓",
    "stocks": (
        {
            "code": "000858",
            "name": "五粮液",
            "qty": 200,
            "fundamental": "估值必须对照经营现金流，不要只看便宜",
            "tracking": "追踪时对照基线价格，检查量能是否跟上",
        },
        {
            "code": "000333",
            "name": "美的集团",
            "qty": 300,
            "fundamental": "估值须对照自由现金流，不要只看市盈率",
            "tracking": "追踪宏观利率对出口敞口的影响",
        },
        {
            "code": "601318",
            "name": "中国平安",
            "qty": 400,
            "fundamental": "保险负债成本必须对照利率周期",
            "tracking": "检查利率是否已证伪负债成本假设",
        },
    ),
}


def _role_memory(role: str, stock: dict[str, str], *, round_no: int) -> dict[str, Any]:
    code = stock["code"]
    name = stock["name"]
    suffix = "第二轮复核" if round_no == 2 else "首轮沉淀"
    packs = {
        "fundamental": {
            "memory_summary": "{}：盯经营现金流证伪（{}）".format(name, suffix),
            "lessons": ["利润好看也要对照经营现金流"],
            "active_hypotheses": ["估值修复依赖盈利兑现"],
            "falsifiers_watched": ["经营现金流连续两季转负"],
            "open_questions": ["下一季现金流能否跟上利润"],
            "pending_tasks": ["核对{}最新季报现金流".format(code)],
        },
        "technical": {
            "memory_summary": "{}：趋势未破但量能偏弱（{}）".format(name, suffix),
            "lessons": ["不要把缩量当成趋势转强"],
            "active_hypotheses": ["价格仍贴关键均线"],
            "falsifiers_watched": ["放量跌破年线"],
            "open_questions": ["下次放量是突破还是出货"],
        },
        "sentiment": {
            "memory_summary": "{}：机构仓位平稳未见集中减持（{}）".format(name, suffix),
            "lessons": ["舆情热闹不等于基本面变化"],
            "active_hypotheses": ["持仓结构仍稳定"],
            "falsifiers_watched": ["出现集中减持或问询"],
        },
        "macro": {
            "memory_summary": "{}：宏观只作约束不对个股定价（{}）".format(name, suffix),
            "lessons": ["利率与政策变化要落到该股敞口"],
            "active_hypotheses": ["利率环境中性"],
            "falsifiers_watched": ["政策转向直接冲击该行业"],
        },
    }
    memory = dict(packs[role])
    if round_no == 2:
        memory["lessons"] = ["第二轮仍维持对照纪律，不因价格波动改口径"]
        memory["pending_tasks"] = ["把证伪条件带进下次追踪"]
    return memory


def _track_memory(stock: dict[str, str], *, round_no: int) -> dict[str, Any]:
    name = stock["name"]
    if round_no == 1:
        return {
            "memory_summary": "{}：对照基线，量价未证伪持有".format(name),
            "lessons": ["追踪不能只看一天涨跌"],
            "active_hypotheses": ["原持有理由仍成立"],
            "falsifiers_watched": ["价格跌破首次研究关键位"],
            "open_questions": ["下次检查点仍看量能与现金流"],
        }
    return {
        "memory_summary": "{}：第二轮追踪仍未见证伪".format(name),
        "lessons": ["跨轮继承后继续对照原判断"],
        "pending_tasks": ["把用户口径带进下一检查点"],
    }


def _track_final(memory: dict[str, Any]) -> LLMResponse:
    sets = [
        {"path": "private_memory.{}".format(key), "value": value}
        for key, value in memory.items()
        if isinstance(value, str) and value
    ]
    appends = [
        {"path": "private_memory.{}".format(key), "values": value}
        for key, value in memory.items()
        if isinstance(value, list) and value
    ]
    payload = {
        "status": "completed",
        "output": _unchanged_output(),
        "reflection": {
            "what_worked": ["read prescreen"],
            "what_was_missing": [],
            "process_errors": [],
        },
        "state_patch": {"set": sets, "append": appends, "remove": []},
    }
    return LLMResponse(
        id="final",
        model="fake",
        content="done",
        tool_calls=[
            LLMToolCall(
                id="f1",
                name="submit_final",
                arguments=json.dumps(payload, ensure_ascii=False),
            )
        ],
        finish_reason="tool_calls",
    )


def _track_queue(memory: dict[str, Any]) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        [
            LLMResponse(
                id="c1",
                model="fake",
                content="ctx",
                tool_calls=[
                    LLMToolCall(
                        id="c1",
                        name="get_tracking_context",
                        arguments='{"dummy":""}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                id="c2",
                model="fake",
                content="pre",
                tool_calls=[
                    LLMToolCall(
                        id="c2",
                        name="get_prescreen",
                        arguments='{"dummy":""}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            _track_final(memory),
        ]
    )


def _agents(stock: dict[str, str], *, round_no: int) -> dict[str, ScriptedAgentLLM]:
    return {
        role: ScriptedAgentLLM(role, memory=_role_memory(role, stock, round_no=round_no))
        for role in ("fundamental", "technical", "sentiment", "macro")
    }


def _first_user_payload(llm: ScriptedAgentLLM) -> dict[str, Any]:
    for message in llm.requests[0].messages:
        if message.role == "user" and message.content:
            return json.loads(message.content)
    raise AssertionError("no user message for {}".format(llm.role))


async def _analyze_round(
    *,
    code: str,
    database: SQLiteDatabase,
    artifacts: str,
    agents: dict[str, ScriptedAgentLLM],
    user_prompt: str = NONE,
    extract_slots: dict[str, str] | None = None,
) -> Any:
    outcome = await analyze_stock(
        code,
        database=database,
        llm_adapter=agents["fundamental"],
        artifact_root=artifacts,
        fixture=True,
        fixtures_dir=str(FIXTURES),
        technical_llm_adapter=agents["technical"],
        sentiment_llm_adapter=agents["sentiment"],
        macro_llm_adapter=agents["macro"],
        user_prompt=user_prompt,
        user_id="local",
        extract_llm_adapter=(
            _extract_llm(_slots_payload(**extract_slots)) if extract_slots else FakeLLMAdapter([])
        ),
    )
    if outcome.error or outcome.run.status == "failed":
        raise RuntimeError(outcome.error or "analyze failed")
    return outcome


async def _remember(
    *,
    run_id: str,
    database: SQLiteDatabase,
    artifacts: str,
    agents: dict[str, ScriptedAgentLLM],
    prompt: str,
    slots: dict[str, str],
) -> None:
    result = await feedback_on_run(
        run_id,
        database=database,
        llm_adapter=agents["fundamental"],
        artifact_root=artifacts,
        user_prompt=prompt,
        extract_llm_adapter=_extract_llm(_slots_payload(**slots)),
        technical_llm_adapter=agents["technical"],
        sentiment_llm_adapter=agents["sentiment"],
        macro_llm_adapter=agents["macro"],
        user_id="local",
    )
    if result.error:
        raise RuntimeError(result.error)


async def _track_round(
    *,
    thesis_id: str,
    database: SQLiteDatabase,
    artifacts: str,
    memory: dict[str, Any],
    user_prompt: str = NONE,
    extract_slots: dict[str, str] | None = None,
) -> Any:
    tracker = _track_queue(memory)
    result = await track_thesis(
        thesis_id,
        database=database,
        llm_adapter=tracker,
        artifact_root=artifacts,
        fixture=True,
        fixtures_dir=str(FIXTURES),
        thinking_enabled=False,
        user_prompt=user_prompt,
        user_id="local",
        extract_llm_adapter=(
            _extract_llm(_slots_payload(**extract_slots)) if extract_slots else FakeLLMAdapter([])
        ),
    )
    return result, tracker


def _quote(code: str, name: str) -> QuoteInput:
    return QuoteInput(
        stock_code=code,
        name=name,
        industry="模拟行业",
        market="sz",
        price=20.0,
        change_pct=0.8,
        turnover=1_000_000.0,
    )


def _link_job(board: BoardStore, code: str, *, kind: str, run_id: str) -> None:
    job = board.create_research_job(code, kind=kind)
    board.finish_research(job["job_id"], run_id=run_id)


async def run_simulated_investor(
    *,
    agent_db: str,
    artifacts: str,
    board_db: str,
    seed_quotes: bool = True,
) -> dict[str, Any]:
    Path(artifacts).mkdir(parents=True, exist_ok=True)
    database = SQLiteDatabase(agent_db)
    await database.initialize()
    board = BoardStore(board_db)
    board.initialize()
    board.initialize_research()
    board.initialize_paper()
    if seed_quotes:
        board.replace_quotes(
            [_quote(item["code"], item["name"]) for item in PERSONA["stocks"]],
            source="simulated-user",
        )
    board.save_pick_memory(
        stock_code="none",
        statement="选股纪律：不追一天涨停，加仓前须核对经营现金流",
    )

    summary: dict[str, Any] = {"stocks": {}, "second_analyze_prior": {}, "second_track_carried": {}}
    for stock in PERSONA["stocks"]:
        code = stock["code"]
        board.add_watch(code)
        first_agents = _agents(stock, round_no=1)
        first = await _analyze_round(
            code=code,
            database=database,
            artifacts=artifacts,
            agents=first_agents,
            user_prompt="#remember #fundamental {}".format(stock["fundamental"]),
            extract_slots={"fundamental": stock["fundamental"]},
        )
        if code == "000858":
            await _remember(
                run_id=first.run.run_id,
                database=database,
                artifacts=artifacts,
                agents=first_agents,
                prompt="#remember #decision {}".format(PERSONA["decision"]),
                slots={"decision": PERSONA["decision"]},
            )
        second_agents = _agents(stock, round_no=2)
        second = await _analyze_round(
            code=code, database=database, artifacts=artifacts, agents=second_agents
        )
        summary["second_analyze_prior"][code] = {
            role: _first_user_payload(llm).get("prior_memory") or {}
            for role, llm in second_agents.items()
        }
        _link_job(board, code, kind="analyze", run_id=second.run.run_id)

        track1, _ = await _track_round(
            thesis_id=first.run.thesis_id,
            database=database,
            artifacts=artifacts,
            memory=_track_memory(stock, round_no=1),
            user_prompt="#remember #tracking {}".format(stock["tracking"]),
            extract_slots={"tracking": stock["tracking"]},
        )
        track2, tracker = await _track_round(
            thesis_id=first.run.thesis_id,
            database=database,
            artifacts=artifacts,
            memory=_track_memory(stock, round_no=2),
        )
        track_payload = {}
        for message in tracker.requests[0].messages:
            if message.role == "user" and message.content:
                try:
                    track_payload = json.loads(message.content)
                    break
                except json.JSONDecodeError:
                    continue
        summary["second_track_carried"][code] = bool(
            (track_payload.get("prior_memory") or {}).get("memory_summary")
        )
        _link_job(board, code, kind="track", run_id=track2.run_id)
        try:
            board.paper_trade(
                code, side="buy", quantity=stock["qty"], reason=stock["fundamental"]
            )
        except ValueError:
            pass
        summary["stocks"][code] = {
            "analyze_run_id": second.run.run_id,
            "track_run_id": track2.run_id,
            "thesis_id": first.run.thesis_id,
        }

    states = SQLiteStateStore(database)
    users = UserStore(database)
    profile = await users.profile(user_id="local", stock_code=NONE)
    memories = []
    for stock in PERSONA["stocks"]:
        for role in ("fundamental", "technical", "sentiment", "macro", "tracking"):
            saved = await states.latest_for_agent(
                agent_name=role, thesis_id="{}-initial".format(stock["code"])
            )
            if saved is not None:
                memories.append(
                    {
                        "agent_name": role,
                        "stock_code": stock["code"],
                        "parent": bool(saved.parent_session_id),
                        "summary": (saved.private_memory or {}).get("memory_summary"),
                        "lessons": (saved.private_memory or {}).get("lessons") or [],
                    }
                )
    summary["preference_count"] = len(profile.get("preferences") or [])
    summary["expert_rows"] = memories
    return summary


def make_client(board_db: str, *, agent_db: str, artifacts: str, monkeypatch) -> Any:
    monkeypatch.setenv("INSIGHTAGENT_DB_PATH", agent_db)
    monkeypatch.setenv("INSIGHTAGENT_ARTIFACT_ROOT", artifacts)
    return create_app(board_db)
