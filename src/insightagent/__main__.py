from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Optional, Sequence

from .llm import DeepSeekChatAdapter, DeepSeekConfig
from .persistence import SQLiteDatabase
from .workflows.initial_research import (
    analyze_stock,
    format_cli_text,
    write_run_json,
)

DEFAULT_DB_PATH = os.environ.get(
    "INSIGHTAGENT_DB_PATH", "data/insightagent.db"
)
DEFAULT_ARTIFACT_ROOT = os.environ.get(
    "INSIGHTAGENT_ARTIFACT_ROOT", "data/artifacts"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m insightagent")
    commands = parser.add_subparsers(dest="command", required=True)

    db = commands.add_parser("db", help="Manage the local SQLite database")
    db_commands = db.add_subparsers(dest="db_command", required=True)

    init = db_commands.add_parser("init", help="Initialize the database")
    init.add_argument("--path", default=DEFAULT_DB_PATH)

    status = db_commands.add_parser("status", help="Show database status")
    status.add_argument("--path", default=DEFAULT_DB_PATH)

    analyze = commands.add_parser("analyze", help="Run P0 fundamental research")
    analyze.add_argument("stock_code")
    analyze.add_argument("--path", default=DEFAULT_DB_PATH)
    analyze.add_argument(
        "--artifact-root",
        default=DEFAULT_ARTIFACT_ROOT,
    )
    analyze.add_argument(
        "--fixture",
        action="store_true",
        help="Use packaged fixture snapshots instead of AKShare",
    )
    analyze.add_argument("--fixtures-dir", default=None)
    analyze.add_argument("--model", default="deepseek-v4-flash")
    analyze.add_argument("--json", action="store_true", dest="as_json")
    analyze.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable DeepSeek thinking mode",
    )
    return parser


def build_llm(model: str) -> DeepSeekChatAdapter:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit(
            "DEEPSEEK_API_KEY is required for analyze. "
            "Pass it as a process environment variable."
        )
    return DeepSeekChatAdapter(
        DeepSeekConfig(api_key=api_key, default_model=model)
    )


async def _run(args: argparse.Namespace) -> int:
    if args.command == "analyze":
        return await _run_analyze(args)

    database = SQLiteDatabase(args.path)
    if args.db_command == "init":
        await database.initialize()
        status = await database.status()
        print(
            json.dumps(
                {
                    "initialized": True,
                    "path": status["path"],
                    "schema_version": status["schema_version"],
                    "journal_mode": status["journal_mode"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.db_command == "status":
        print(
            json.dumps(
                await database.status(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    raise RuntimeError("Unknown database command")


async def _run_analyze(args: argparse.Namespace) -> int:
    database = SQLiteDatabase(args.path)
    await database.initialize()
    needs_llm = True
    if args.fixture:
        from .fundamentals import (
            REQUIRED_FIELDS,
            FixtureFundamentalAdapter,
        )
        from .fundamental_agent import default_fixtures_dir

        adapter = FixtureFundamentalAdapter.from_directory(
            default_fixtures_dir(args.fixtures_dir)
        )
        snapshot = await adapter.fetch_fundamental(args.stock_code.strip())
        needs_llm = not all(
            getattr(snapshot, name) is None for name in REQUIRED_FIELDS
        )

    llm_adapter = None
    if needs_llm:
        llm_adapter = build_llm(args.model)
    else:
        from .llm import FakeLLMAdapter

        llm_adapter = FakeLLMAdapter([])

    outcome = await analyze_stock(
        args.stock_code,
        database=database,
        llm_adapter=llm_adapter,
        artifact_root=args.artifact_root,
        fixture=args.fixture,
        fixtures_dir=args.fixtures_dir,
        model=args.model,
        thinking_enabled=not args.no_thinking,
        unbound_policy="abstain",
    )
    write_run_json(outcome, args.path)
    if args.as_json:
        print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_cli_text(outcome))
    if outcome.run.status == "failed":
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
