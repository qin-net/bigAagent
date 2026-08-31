"""Run tracking for current paper picks and attach board track jobs."""
from __future__ import annotations

import asyncio
import os

from insightagent.env import load_dotenv, resolve_path
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.persistence import SQLiteDatabase
from insightagent.tracking_agent import track_thesis
from insightboard.store import BoardStore

BOARD_DB = resolve_path("", default_relative="data/board.db")
AGENT_DB = resolve_path("", default_relative="data/insightagent.db")

PICKS = (
    ("000858", "对照基线检查量能，不要只看一天涨跌"),
    ("000333", "对照自由现金流和出口敞口做一次追踪"),
    ("601318", "检查利率是否证伪负债成本假设"),
    ("300308", "对照订单与现金流，不要被日内波动带跑"),
)


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY missing")
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
        thesis = "{}-initial".format(code)
        print("track", thesis, flush=True)
        try:
            result = await track_thesis(
                thesis,
                database=database,
                llm_adapter=llm,
                artifact_root=artifact_root,
                fixture=True,
                model=model,
                thinking_enabled=True,
                user_prompt=prompt,
            )
        except Exception as error:
            print("crash", code, type(error).__name__, error, flush=True)
            job = board.create_research_job(code, kind="track")
            board.finish_research(job["job_id"], error="{}: {}".format(type(error).__name__, error))
            continue
        status = (result.deliverable or {}).get("status")
        print("done", code, result.run_id, status, flush=True)
        job = board.create_research_job(code, kind="track")
        board.finish_research(job["job_id"], run_id=result.run_id)


if __name__ == "__main__":
    asyncio.run(main())
