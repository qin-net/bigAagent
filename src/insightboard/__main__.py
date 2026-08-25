from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from typing import Sequence

from .api import create_app
from .collector import AkshareQuoteCollector, collect_once
from .store import BoardStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m insightboard")
    parser.add_argument("--db", default="data/board.db")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize the board database")
    commands.add_parser("status", help="Show current ingest status")
    commands.add_parser("collect-once", help="Collect one batch of delayed quotes")
    commands.add_parser("worker", help="Run 30-minute quote collection during A-share trading hours")
    serve = commands.add_parser("serve", help="Run the local API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = BoardStore(args.db)
    store.initialize()
    if args.command == "init":
        print(json.dumps({"initialized": True, "path": args.db}, ensure_ascii=False))
        return 0
    if args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "collect-once":
        try:
            batch_id = asyncio.run(collect_once(store, AkshareQuoteCollector()))
        except Exception as error:
            status = store.status()
            print(
                "行情采集失败：{}: {}".format(type(error).__name__, error),
                flush=True,
            )
            print(
                "已保留上一批成功数据。可运行 `python -m insightboard --db {} status` 查看状态。".format(args.db),
                flush=True,
            )
            if status["last_run"]:
                print("最近采集：{}".format(json.dumps(status["last_run"], ensure_ascii=False)), flush=True)
            return 1
        print(json.dumps({"batch_id": batch_id, **store.status()}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "worker":
        return _run_worker(store)
    if args.command == "serve":
        import uvicorn
        uvicorn.run(create_app(args.db), host=args.host, port=args.port)
        return 0
    return 1


def _run_worker(store: BoardStore) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from zoneinfo import ZoneInfo

    timezone = ZoneInfo("Asia/Shanghai")

    def job() -> None:
        current = datetime.now(timezone)
        in_trading_window = (current.hour == 9 and current.minute >= 15) or (
            10 <= current.hour <= 14
        ) or (current.hour == 15 and current.minute <= 15)
        if current.weekday() >= 5 or not in_trading_window:
            return
        try:
            asyncio.run(collect_once(store, AkshareQuoteCollector()))
        except Exception as error:
            print("quote collection failed: {}: {}".format(type(error).__name__, error))

    def deep_job() -> None:
        item = store.claim_deep()
        if not item:
            return
        code = item["stock_code"]
        try:
            collector = AkshareQuoteCollector()
            bars, notices = collector.collect_deep(code)
            if not bars:
                raise RuntimeError("No daily bars returned")
            store.save_deep(bars, notices, source="akshare")
            store.finish_deep(code)
        except Exception as error:
            store.finish_deep(code, error="{}: {}".format(type(error).__name__, error))

    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(job, "cron", minute="0,30", max_instances=1, coalesce=True)
    scheduler.add_job(deep_job, "interval", minutes=1, max_instances=1, coalesce=True)
    job()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
