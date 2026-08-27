from __future__ import annotations

import asyncio
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

from insightagent.env import load_dotenv
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.persistence import SQLiteDatabase
from insightagent.research_store import ResearchStore
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


def agent_paths() -> tuple[str, str, str]:
    load_dotenv()
    return (
        os.environ.get("INSIGHTAGENT_DB_PATH", "data/insightagent.db"),
        os.environ.get("INSIGHTAGENT_ARTIFACT_ROOT", "data/artifacts"),
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
        "status": bundle.get("status") or run.get("status"),
        "parent_run_id": parent_run_id,
        "rerun_dimensions": rerun_dimensions,
        "intent": {key: intent.get(key) for key in ("effect", "fundamental", "technical", "sentiment", "macro", "decision", "tracking") if key in intent},
        "dimensions": dimensions,
        "decision": ({key: decision.get(key) for key in ("rating", "confidence", "value_score", "timing_score", "advice_one_liner", "risks", "falsifiers", "disagreements", "dimensions_used", "dimensions_missing")} if decision else None),
        "disclaimer": "研究辅助，非投资建议",
    }


async def load_projected_run(run_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    agent_db_path, _, _ = agent_paths()
    database = SQLiteDatabase(db_path or agent_db_path)
    bundle = await ResearchStore(database).get_run(run_id)
    return project_run(bundle) if bundle else None


async def execute_job(job: dict[str, Any], board_store: Any = None) -> dict[str, Any]:
    agent_db_path, artifact_root, model = agent_paths()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    database = SQLiteDatabase(agent_db_path)
    await database.initialize()
    llm = DeepSeekChatAdapter(DeepSeekConfig(api_key=api_key, default_model=model))
    prompt = job.get("prompt") or "none"
    if job["kind"] == "analyze":
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
        return {"run_id": result.parent_run_id, "noop": True, "rerun_dimensions": []}
    if result.outcome.run.status == "failed":
        raise RuntimeError(result.outcome.error or "feedback failed")
    return {"run_id": result.outcome.run.run_id, "noop": False, "rerun_dimensions": list(result.outcome.rerun_dimensions)}


def execute_job_sync(job: dict[str, Any], board_store: Any = None) -> dict[str, Any]:
    return asyncio.run(execute_job(job, board_store))
