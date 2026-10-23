from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from insightagent.env import resolve_path

SPOOL_ENV = "INSIGHTBOARD_RESEARCH_SPOOL"
LOG = logging.getLogger("insightboard.research")


def spool_path(job_id: str) -> Path:
    # Anchor to the repo root like every database path does. A bare relative
    # path resolves against the *current* working directory, so an API process
    # and a worker started elsewhere would look in different directories and
    # the worker would silently read no prompt at all.
    root = Path(
        resolve_path(
            os.environ.get(SPOOL_ENV, ""),
            default_relative="data/research_spool",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / (job_id + ".txt")


def write_prompt(job_id: str, prompt: str) -> None:
    spool_path(job_id).write_text((prompt or "none").strip() or "none", encoding="utf-8")


def remove_prompt(job_id: str) -> None:
    try:
        spool_path(job_id).unlink()
    except FileNotFoundError:
        pass

from insightagent.env import load_dotenv
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
        "synthesis": None,
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


def project_synthesis(deliverable: dict[str, Any] | None) -> dict[str, Any] | None:
    if not deliverable:
        return None
    return {
        "synthesis": deliverable.get("synthesis") or "",
        "thinking": deliverable.get("thinking") or "",
        "expert_evaluations": list(deliverable.get("expert_evaluations") or []),
    }


async def _memories_for_run(database: SQLiteDatabase, session_ids: dict[str, Any]) -> dict[str, Any]:
    store = SQLiteStateStore(database)
    memories: dict[str, Any] = {}
    for role in ("fundamental", "technical", "sentiment", "macro", "tracking", "synthesis"):
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
            LIMIT 500
            """
        ).fetchall()
    except sqlite3.Error:
        # A database fault must not masquerade as "no expert has learned
        # anything yet"; log it so the empty list is explainable.
        LOG.exception("failed to load expert memories")
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
    else:
        # Research runs store the initial synthesis (narrative written by
        # the tracking agent) as a timeline row too. Keep it under its own
        # key: the frontend routes `tracking` to the track renderer.
        timeline = await store.timeline_for_run(run_id)
        projected["synthesis"] = project_synthesis((timeline or {}).get("deliverable"))
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
            synthesis_llm_adapter=llm,
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


PROGRESS_STEPS = (
    ("prepare", "准备研究"),
    ("intent", "理解口径"),
    ("fetch", "取数"),
    ("agents", "四维分析"),
    ("decision", "汇总结论"),
    ("synthesis", "综合分析"),
)

ANALYSIS_DIMENSIONS = ("fundamental", "technical", "sentiment", "macro")


def _audit_events(run_id: str, db_path: str) -> list[dict[str, Any]]:
    """Read a run's audit trail. Read-only, and missing files are not an error."""
    if not run_id or not os.path.exists(db_path):
        return []
    target = db_path.replace("\\", "/")
    connection = sqlite3.connect("file:{}?mode=ro".format(target), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select event_type, payload_json, created_at from audit_events "
            "where run_id = ? order by created_at, rowid",
            (run_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        events.append(
            {
                "type": row["event_type"],
                "payload": payload if isinstance(payload, dict) else {},
                "at": row["created_at"],
            }
        )
    return events


def _seconds_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        second = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((second - first).total_seconds(), 1)


def _guess_run_id(job: dict[str, Any], db_path: str) -> Optional[str]:
    """Find the run a job started, for jobs that died before reporting back.

    A job only learns its run_id when it finishes, so an interrupted one leaves
    run_id empty while the audit trail for its run exists anyway. Matching on
    stock + a narrow start window recovers that trail instead of showing an
    empty panel.
    """
    stock = job.get("stock_code")
    created = job.get("created_at")
    if not stock or not created or not os.path.exists(db_path):
        return None
    try:
        started = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return None
    low = (started - timedelta(seconds=5)).isoformat()
    high = (started + timedelta(minutes=30)).isoformat()
    target = db_path.replace("\\", "/")
    connection = sqlite3.connect("file:{}?mode=ro".format(target), uri=True)
    try:
        row = connection.execute(
            "select run_id from runs where stock_code = ? and created_at >= ? "
            "and created_at <= ? order by created_at asc limit 1",
            (stock, low, high),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return row[0] if row else None


def progress_payload(job: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """Fold the append-only audit trail into a stage list for the board.

    No mutable progress column is written anywhere: the trail already exists,
    so old runs get progress replay for free and there is only one source of
    truth.
    """
    now = now or datetime.now(timezone.utc)
    db_path = agent_paths()[0]
    run_id = job.get("run_id") or _guess_run_id(job, db_path)
    events = _audit_events(run_id, db_path) if run_id else []

    dimensions = {dim: "pending" for dim in ANALYSIS_DIMENSIONS}
    summaries: dict[str, Any] = {}
    fetch_started_at: dict[str, str] = {}
    finished_at: dict[str, str] = {}
    fetched: set[str] = set()
    seen: set[str] = set()
    stamps: dict[str, str] = {}
    for event in events:
        name = event["type"]
        payload = event["payload"]
        dimension = payload.get("dimension")
        seen.add(name)
        stamps.setdefault(name, event["at"])
        if name == "fetch_started" and dimension in dimensions:
            dimensions[dimension] = "fetching"
            fetch_started_at.setdefault(dimension, event["at"])
        elif name == "fetch_completed" and dimension in dimensions:
            fetched.add(dimension)
            if dimensions[dimension] == "fetching":
                dimensions[dimension] = "fetched"
        elif name == "agent_completed" and dimension in dimensions:
            dimensions[dimension] = "done"
            finished_at[dimension] = event["at"]
            summaries[dimension] = {
                "status": payload.get("status"),
                "stance": payload.get("stance"),
                "score": payload.get("score"),
                "summary": payload.get("summary"),
            }

    agents_done = sum(1 for dim in ANALYSIS_DIMENSIONS if dimensions[dim] == "done")
    fetch_done = "snapshot_ready" in seen or len(fetched) == len(ANALYSIS_DIMENSIONS)

    done_flags = {
        "prepare": "run_started" in seen,
        "intent": "intent_parsed" in seen,
        "fetch": fetch_done,
        "agents": "decision_written" in seen or agents_done == len(ANALYSIS_DIMENSIONS),
        "decision": "decision_written" in seen,
        "synthesis": "synthesis_completed" in seen or "synthesis_failed" in seen,
    }

    status = job.get("status") or "queued"
    current = None
    steps = []
    for key, label in PROGRESS_STEPS:
        if done_flags.get(key):
            state = "done"
        elif current is None:
            current = key
            state = "running"
        else:
            state = "pending"
        steps.append({"key": key, "label": label, "state": state})

    if status == "failed":
        for step in steps:
            if step["state"] == "running":
                step["state"] = "failed"
    if status in {"success", "unchanged"}:
        # A finished run must not leave a stage "pending": the ones that never
        # happened (synthesis after a crash, or every run before those events
        # existed) are skipped, not still coming.
        for step in steps:
            if not done_flags.get(step["key"]):
                step["state"] = "skipped"
        current = None

    label = dict(PROGRESS_STEPS).get(current or "", "")
    if status == "queued":
        label = "排队中"
    elif status == "failed":
        label = "失败"
    elif status in {"success", "unchanged"}:
        label = "已完成"

    elapsed = _seconds_between(job.get("created_at"), now.isoformat())
    heartbeat = _seconds_between(job.get("updated_at"), now.isoformat())

    now_iso = now.isoformat()
    # 已结束的任务（尤其失败/中断的）不能用 now 算时长，否则卡片上的秒数会一直往上涨；
    # 停在最后一个事件的时间点才是"它死在哪一秒"。
    terminal = status in {"success", "unchanged", "failed"}
    end_ref = (events[-1]["at"] if terminal and events else None) or now_iso
    agents_started = stamps.get("snapshot_ready") or stamps.get("fetch_started")
    dimension_times = {}
    for dim in ANALYSIS_DIMENSIONS:
        if dimensions[dim] == "pending":
            continue
        started = agents_started or fetch_started_at.get(dim)
        finished = finished_at.get(dim)
        dimension_times[dim] = {
            "started": bool(started),
            "finished": bool(finished),
            "elapsed_seconds": _seconds_between(started, finished or end_ref) if started else None,
        }

    phases = [
        {
            "key": "fetch",
            "label": "取数",
            "state": "done" if fetch_done else ("running" if "run_started" in seen else "pending"),
            "elapsed_seconds": _seconds_between(
                stamps.get("run_started"),
                stamps.get("snapshot_ready") or end_ref,
            ),
        },
        {
            "key": "agents",
            "label": "四维分析",
            "state": (
                "done"
                if done_flags["agents"]
                else ("running" if fetch_done else "pending")
            ),
            "elapsed_seconds": _seconds_between(
                agents_started,
                stamps.get("decision_written") or end_ref,
            ),
            "done_count": agents_done,
            "total": len(ANALYSIS_DIMENSIONS),
        },
        {
            "key": "decision",
            "label": "汇总结论",
            "state": "done" if done_flags["decision"] else ("running" if agents_done == len(ANALYSIS_DIMENSIONS) else "pending"),
            "elapsed_seconds": 0.0 if done_flags["decision"] else None,
        },
    ]

    return {
        "job_id": job.get("job_id"),
        "kind": job.get("kind"),
        "status": status,
        "run_id": run_id,
        "stage": current,
        "stage_label": label,
        "elapsed_seconds": elapsed,
        "heartbeat_seconds": heartbeat,
        "steps": steps,
        "phases": phases,
        "last_stage": current,
        "last_event_type": events[-1]["type"] if events else None,
        "last_event_at": events[-1]["at"] if events else None,
        "dimensions": dimensions,
        "dimension_summaries": summaries,
        "dimension_times": dimension_times,
        "agents_done": agents_done,
        "agents_total": len(ANALYSIS_DIMENSIONS),
    }
