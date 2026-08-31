from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

SPOOL_ENV = "INSIGHTBOARD_RESEARCH_SPOOL"


def spool_path(job_id: str) -> Path:
    root = Path(os.environ.get(SPOOL_ENV, "data/research_spool"))
    root.mkdir(parents=True, exist_ok=True)
    return root / (job_id + ".txt")


def write_prompt(job_id: str, prompt: str) -> None:
    spool_path(job_id).write_text((prompt or "none").strip() or "none", encoding="utf-8")


def remove_prompt(job_id: str) -> None:
    try:
        spool_path(job_id).unlink()
    except FileNotFoundError:
        pass

from insightagent.env import load_dotenv, resolve_path
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.persistence import SQLiteDatabase, SQLiteStateStore
from insightagent.research_store import ResearchStore
from insightagent.state import snapshot_private_memory
from insightagent.user_contracts import DIMS, NONE
from insightagent.user_profile_agent import generate_investor_profile
from insightagent.user_store import UserStore
from insightagent.tracking_agent import (
    TrackFeedbackError,
    feedback_on_track,
    track_thesis,
)
from insightagent.workflows.initial_research import (
    FeedbackError,
    analyze_stock,
    feedback_on_run,
)
from insightagent.data_contracts import KlineBar, KlineSnapshot, PriceSnapshot
from insightagent.market import compute_indicators


class BoardTechnicalAdapter:
    """Read B1 daily bars first; fail loudly so workflow can fallback."""

    def __init__(self, board_store: Any) -> None:
        self.board_store = board_store

    async def fetch_technical(self, stock_code: str) -> dict[str, Any]:
        bars = self.board_store.bars(stock_code, 120)
        if len(bars) < 20:
            raise RuntimeError("board daily bars unavailable")
        indicator = compute_indicators(stock_code, bars)
        quote_page = self.board_store.quote_page(page=1, size=1, q=stock_code, sort="stock_code", order="asc")
        item = next((row for row in quote_page["items"] if row["stock_code"] == stock_code), {})
        price = PriceSnapshot(
            stock_code=stock_code,
            price=item.get("price"), open=item.get("open"), high=item.get("high"),
            low=item.get("low"), volume=item.get("volume"), turnover=item.get("turnover"),
            change_pct=item.get("change_pct"), source="insightboard",
        )
        kline = KlineSnapshot(
            stock_code=stock_code, source="insightboard", bars_used=len(bars),
            last_close=bars[-1].get("close"),
            bars=[KlineBar(date=row.get("trade_date"), open=row.get("open"), high=row.get("high"), low=row.get("low"), close=row.get("close"), volume=row.get("volume")) for row in bars[-30:]],
        )
        return {"indicator": indicator.model_dump(mode="json"), "kline": kline.model_dump(mode="json"), "price": price.model_dump(mode="json")}


def agent_paths() -> tuple[str, str, str, str]:
    load_dotenv()
    return (
        resolve_path(os.environ.get("INSIGHTAGENT_DB_PATH", ""), default_relative="data/insightagent.db"),
        resolve_path(os.environ.get("INSIGHTAGENT_KB_PATH", ""), default_relative="data/kb.db"),
        resolve_path(os.environ.get("INSIGHTAGENT_ARTIFACT_ROOT", ""), default_relative="data/artifacts"),
        os.environ.get("INSIGHTAGENT_MODEL", "deepseek-v4-flash"),
    )


def project_run(bundle: dict[str, Any], *, run_id: Optional[str] = None) -> dict[str, Any]:
    run = bundle.get("run") or {}
    reports = {item.get("role"): item for item in bundle.get("reports", [])}
    decision = (bundle.get("decisions") or [None])[0]
    intent = run.get("intent") or {}
    if not intent and bundle.get("intent"):
        intent = bundle["intent"]
    dimensions = {}
    for role in ("fundamental", "technical", "sentiment", "macro"):
        report = reports.get(role)
        dimensions[role] = (
            {key: report.get(key) for key in ("stance", "score", "summary", "abstain", "degraded", "missing_information")}
            if report else None
        )
    run_identifier = run_id or run.get("run_id")
    parent_run_id = run.get("parent_run_id")
    rerun_dimensions = run.get("rerun_dimensions", [])
    return {
        "run_id": run_identifier,
        "short_id": (run_identifier or "")[:8],
        "created_at": run.get("created_at"),
        "stock_code": run.get("stock_code"),
        "mode": run.get("mode") or "research",
        "status": bundle.get("status") or run.get("status"),
        "parent_run_id": parent_run_id,
        "rerun_dimensions": rerun_dimensions,
        "intent": {key: intent.get(key) for key in ("effect", "fundamental", "technical", "sentiment", "macro", "decision", "tracking") if key in intent},
        "dimensions": dimensions,
        "decision": ({key: decision.get(key) for key in ("rating", "confidence", "value_score", "timing_score", "advice_one_liner", "risks", "falsifiers", "disagreements", "dimensions_used", "dimensions_missing")} if decision else None),
        "tracking": None,
        "disclaimer": "研究辅助，非投资建议",
        "memories": {},
        "preferences": [],
    }


def project_tracking(deliverable: dict[str, Any] | None) -> dict[str, Any] | None:
    if not deliverable:
        return None
    user = deliverable.get("user_output") or {}
    return {
        "status": deliverable.get("status"),
        "work_summary": deliverable.get("work_summary") or "",
        "thinking": deliverable.get("thinking") or "",
        "synthesis": deliverable.get("synthesis") or "",
        "expert_evaluations": list(deliverable.get("expert_evaluations") or []),
        "triggers_hit": list(deliverable.get("triggers_hit") or []),
        "user_output": {
            key: user.get(key)
            for key in ("title", "summary", "holding_advice", "key_changes", "next_watch_items")
        },
        "next_check_suggestion": deliverable.get("next_check_suggestion") or {},
    }


async def _memories_for_run(database: SQLiteDatabase, session_ids: dict[str, Any]) -> dict[str, Any]:
    store = SQLiteStateStore(database)
    memories: dict[str, Any] = {}
    for role in ("fundamental", "technical", "sentiment", "macro", "tracking"):
        session_id = session_ids.get(role)
        if not session_id:
            continue
        try:
            state = await store.get(session_id)
        except KeyError:
            continue
        payload = snapshot_private_memory(state.private_memory)
        if not payload:
            continue
        payload["carried"] = bool(state.parent_session_id)
        memories[role] = payload
    return memories


def _latest_expert_memories_sync(database: SQLiteDatabase) -> list[dict[str, Any]]:
    connection = database.connect()
    try:
        rows = connection.execute(
            """
            SELECT session_id, agent_name, stock_code, parent_session_id, state_json, updated_at
            FROM agent_states
            WHERE status = 'SUCCESS'
              AND agent_name IN ('fundamental', 'technical', 'sentiment', 'macro', 'tracking')
            ORDER BY updated_at DESC
            """
        ).fetchall()
    except Exception:
        return []
    finally:
        connection.close()
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for row in rows:
        key = (row["agent_name"], row["stock_code"] or "")
        if key in seen:
            continue
        try:
            payload = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            continue
        memory = snapshot_private_memory(payload.get("private_memory") or {})
        if not memory:
            continue
        seen.add(key)
        items.append(
            {
                "agent_name": row["agent_name"],
                "stock_code": row["stock_code"] or "",
                "carried": bool(row["parent_session_id"]),
                "updated_at": row["updated_at"],
                **memory,
            }
        )
    return items


async def _latest_expert_memories(database: SQLiteDatabase) -> list[dict[str, Any]]:
    await database.initialize()
    return await asyncio.to_thread(_latest_expert_memories_sync, database)


async def _preferences_for_stock(database: SQLiteDatabase, stock_code: str) -> list[dict[str, str]]:
    user_store = UserStore(database)
    items: list[dict[str, str]] = []
    for scope in DIMS:
        rows = await user_store.active_preferences(
            user_id="local", scope=scope, stock_code=stock_code
        )
        for row in rows:
            items.append({"scope": scope, "statement": row.statement})
    return items


async def load_projected_run(run_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    agent_db_path, _, _, _ = agent_paths()
    database = SQLiteDatabase(db_path or agent_db_path)
    store = ResearchStore(database)
    bundle = await store.get_run(run_id)
    if not bundle:
        return None
    projected = project_run(bundle)
    intent = await store.get_intent_for_run(run_id)
    if intent:
        projected["intent"] = {key: intent[key] for key in ("effect", "fundamental", "technical", "sentiment", "macro", "decision", "tracking")}
        projected["intent_created_at"] = intent.get("created_at")
    session_ids = (bundle.get("run") or {}).get("session_ids") or {}
    projected["memories"] = await _memories_for_run(database, session_ids)
    stock_code = projected.get("stock_code") or ""
    if stock_code:
        projected["preferences"] = await _preferences_for_stock(database, stock_code)
    if projected.get("mode") == "track_day":
        timeline = await store.timeline_for_run(run_id)
        projected["tracking"] = project_tracking((timeline or {}).get("deliverable"))
    return projected


async def load_user_profile(
    *,
    user_id: str = "local",
    stock_code: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    agent_db_path, _, _, _ = agent_paths()
    database = SQLiteDatabase(db_path or agent_db_path)
    store = UserStore(database)
    profile = await store.profile(user_id=user_id, stock_code=stock_code or NONE)
    memories = await _latest_expert_memories(database)
    if stock_code:
        memories = [item for item in memories if item["stock_code"] == stock_code]
    profile["expert_memories"] = memories
    profile["generated_profile"] = await store.latest_generated_profile(
        user_id=user_id
    )
    return profile


EXPERT_ROLES = (
    ("fundamental", "基本面专家", "看盈利质量、现金流和估值是否对得上。"),
    ("technical", "技术面专家", "看趋势、位置和交易节奏，不替代基本面。"),
    ("sentiment", "情绪面专家", "看资金与舆论温度，避免把情绪当事实。"),
    ("macro", "宏观专家", "看政策与宏观环境对行业和标的的约束。"),
    ("tracking", "追踪专家", "对照原判断看后续是否被证伪或需要复核。"),
)


async def load_experts_desk(
    *,
    user_id: str = "local",
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    agent_db_path, _, _, _ = agent_paths()
    database = SQLiteDatabase(db_path or agent_db_path)
    store = UserStore(database)
    profile = await store.profile(user_id=user_id, stock_code=NONE)
    memories = await _latest_expert_memories(database)
    grouped: dict[str, list[dict[str, Any]]] = {role: [] for role, _, _ in EXPERT_ROLES}
    for item in memories:
        grouped.setdefault(item["agent_name"], []).append(item)
    experts = []
    for role, title, duty in EXPERT_ROLES:
        rows = grouped.get(role) or []
        portrait = next(
            (item.get("memory_summary") for item in rows if item.get("memory_summary")),
            "",
        )
        experts.append(
            {
                "agent_name": role,
                "title": title,
                "duty": duty,
                "portrait": portrait,
                "stock_count": len({item["stock_code"] for item in rows if item.get("stock_code")}),
                "iterated": any(item.get("carried") for item in rows),
                "memories": rows,
            }
        )
    return {"experts": experts, "preferences": profile.get("preferences") or []}


async def generate_user_profile(
    *,
    paper: dict[str, Any],
    user_id: str = "local",
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    agent_db_path, _, _, model = agent_paths()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    database = SQLiteDatabase(db_path or agent_db_path)
    store = UserStore(database)
    aggregate = await store.profile(user_id=user_id, stock_code=NONE)
    llm = DeepSeekChatAdapter(
        DeepSeekConfig(api_key=api_key, default_model=model)
    )
    narrative = await generate_investor_profile(
        llm_adapter=llm,
        model=model,
        aggregate=aggregate,
        paper=paper,
        user_id=user_id,
    )
    return await store.save_generated_profile(
        user_id=user_id,
        model=model,
        payload=narrative.model_dump(mode="json"),
    )


async def retire_user_preference(
    preference_id: str,
    *,
    user_id: str = "local",
    db_path: Optional[str] = None,
) -> bool:
    agent_db_path, _, _, _ = agent_paths()
    database = SQLiteDatabase(db_path or agent_db_path)
    store = UserStore(database)
    return await store.retire_preference(user_id=user_id, preference_id=preference_id)


async def execute_job(job: dict[str, Any], board_store: Any = None) -> dict[str, Any]:
    agent_db_path, _kb_path, artifact_root, model = agent_paths()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    database = SQLiteDatabase(agent_db_path)
    await database.initialize()
    llm = DeepSeekChatAdapter(DeepSeekConfig(api_key=api_key, default_model=model))
    prompt = job.get("prompt") or "none"
    kind = job["kind"]
    if kind == "track":
        result = await track_thesis(
            "{}-initial".format(job["stock_code"]),
            database=database,
            llm_adapter=llm,
            artifact_root=artifact_root,
            fixture=False,
            model=model,
            thinking_enabled=True,
            user_prompt=prompt,
            user_id=job.get("user_id", "local"),
        )
        return {"run_id": result.run_id, "noop": False, "rerun_dimensions": []}
    if kind == "track_feedback":
        result = await feedback_on_track(
            job.get("parent_run_id") or "",
            database=database,
            llm_adapter=llm,
            artifact_root=artifact_root,
            user_prompt=prompt,
            model=model,
            thinking_enabled=True,
            fixture=False,
            user_id=job.get("user_id", "local"),
        )
        if result.error:
            raise TrackFeedbackError(result.error)
        if result.track_result is None:
            return {"run_id": None, "noop": True, "rerun_dimensions": [], "message": "未重跑追踪"}
        return {"run_id": result.track_result.run_id, "noop": False, "rerun_dimensions": []}
    if kind == "analyze":
        outcome = await analyze_stock(
            job["stock_code"], database=database, llm_adapter=llm,
            artifact_root=artifact_root, fixture=False, model=model,
            thinking_enabled=True, unbound_policy="abstain",
            user_prompt=prompt, user_id=job.get("user_id", "local"),
            technical_adapter=BoardTechnicalAdapter(board_store) if board_store is not None else None,
        )
        if outcome.run.status == "failed":
            raise RuntimeError(outcome.error or "research failed")
        return {"run_id": outcome.run.run_id, "noop": False, "rerun_dimensions": []}
    result = await feedback_on_run(
        job.get("parent_run_id") or "", database=database, llm_adapter=llm,
        artifact_root=artifact_root, user_prompt=prompt, model=model,
        thinking_enabled=True, unbound_policy="abstain",
        user_id=job.get("user_id", "local"),
    )
    if result.error:
        raise FeedbackError(result.error)
    if result.outcome is None:
        return {"run_id": None, "noop": True, "rerun_dimensions": [], "message": "未改变结论"}
    if result.outcome.run.status == "failed":
        raise RuntimeError(result.outcome.error or "feedback failed")
    return {"run_id": result.outcome.run.run_id, "noop": False, "rerun_dimensions": list(result.outcome.rerun_dimensions)}


def execute_job_sync(job: dict[str, Any], board_store: Any = None) -> dict[str, Any]:
    return asyncio.run(execute_job(job, board_store))
