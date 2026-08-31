"""Live first-research for current paper picks. No tracking — leave that for tomorrow."""
from __future__ import annotations

import asyncio
import os
import sqlite3

from insightagent.env import load_dotenv, resolve_path
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.persistence import SQLiteDatabase
from insightagent.workflows.initial_research import analyze_stock
from insightboard.store import BoardStore

BOARD_DB = resolve_path("", default_relative="data/board.db")
AGENT_DB = resolve_path("", default_relative="data/insightagent.db")

PICKS = (
    ("000858", "记住：不要只看便宜，须核对经营现金流"),
    ("000333", "记住：估值须对照自由现金流，不要只看 PE"),
    ("601318", "记住：保险负债成本必须对照利率"),
    ("300308", "记住：景气股不能只看一天涨跌，须核对经营现金流"),
)


def clear_demo_tracks() -> None:
    board = sqlite3.connect(BOARD_DB)
    board.execute(
        "DELETE FROM research_job WHERE kind IN ('track', 'track_feedback')"
    )
    board.commit()
    board.close()
    agent = sqlite3.connect(AGENT_DB)
    agent.execute("DELETE FROM tracking_timeline")
    agent.execute("DELETE FROM runs WHERE mode = 'track_day' OR run_id LIKE 'demo-%-track'")
    agent.commit()
    agent.close()
    print("cleared track jobs, timelines, and track runs")


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY missing")
    clear_demo_tracks()
    board = BoardStore(BOARD_DB)
    board.initialize()
    board.initialize_research()
    database = SQLiteDatabase(AGENT_DB)
    await database.initialize()
    model = os.environ.get("INSIGHTAGENT_MODEL", "deepseek-v4-flash")
    llm = DeepSeekChatAdapter(DeepSeekConfig(api_key=api_key, default_model=model))
    artifact_root = resolve_path(
        os.environ.get("INSIGHTAGENT_ARTIFACT_ROOT", ""),
        default_relative="data/artifacts",
    )
    for code, prompt in PICKS:
        print("analyze", code, flush=True)
        try:
            outcome = await analyze_stock(
                code,
                database=database,
                llm_adapter=llm,
                artifact_root=artifact_root,
                fixture=True,
                model=model,
                thinking_enabled=True,
                unbound_policy="abstain",
                user_prompt=prompt,
            )
        except Exception as error:
            print("crash", code, type(error).__name__, error, flush=True)
            continue
        print(
            "done",
            code,
            outcome.run.status,
            outcome.run.run_id,
            outcome.error,
            flush=True,
        )
        if outcome.run.status == "failed":
            job = board.create_research_job(code, kind="analyze")
            board.finish_research(
                job["job_id"], error=outcome.error or "research failed"
            )
            continue
        job = board.create_research_job(code, kind="analyze")
        board.finish_research(job["job_id"], run_id=outcome.run.run_id)


if __name__ == "__main__":
    asyncio.run(main())
