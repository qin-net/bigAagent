from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Optional, Sequence

from .env import load_dotenv
from .llm import DeepSeekChatAdapter, DeepSeekConfig
from .persistence import SQLiteDatabase
from .pdfmd import (
    DEFAULT_INCOMING_DIR,
    DEFAULT_MARKDOWN_DIR,
    PdfMdError,
    convert_path,
)
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
        "--prompt",
        default="none",
        help="Optional user intent prompt with optional #tags",
    )
    analyze.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable DeepSeek thinking mode",
    )
    pdf2md = commands.add_parser(
        "pdf2md", help="Convert local PDF files to reviewable Markdown"
    )
    pdf2md.add_argument("input", nargs="?", default=str(DEFAULT_INCOMING_DIR))
    pdf2md.add_argument("--out", default=str(DEFAULT_MARKDOWN_DIR))
    pdf2md.add_argument("--force", action="store_true")
    pdf2md.add_argument("--json", action="store_true", dest="as_json")
    return parser


def build_llm(model: str) -> DeepSeekChatAdapter:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit(
            "DEEPSEEK_API_KEY is required for analyze. "
            "Put it in .env or export it in the process environment."
        )
    return DeepSeekChatAdapter(
        DeepSeekConfig(api_key=api_key, default_model=model)
    )


async def _run(args: argparse.Namespace) -> int:
    if args.command == "analyze":
        return await _run_analyze(args)
    if args.command == "pdf2md":
        return _run_pdf2md(args)

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


def _run_pdf2md(args: argparse.Namespace) -> int:
    try:
        results = convert_path(
            Path(args.input), output_dir=Path(args.out), force=args.force
        )
    except PdfMdError as error:
        print("pdf2md error: {}".format(error))
        return 1
    except Exception as error:
        print("pdf2md error: {}: {}".format(type(error).__name__, error))
        return 1

    if args.as_json:
        print(
            json.dumps(
                [
                    {
                        "source_path": str(result.source_path),
                        "output_path": str(result.output_path),
                        "source_sha256": result.source_sha256,
                        "page_count": result.page_count,
                        "status": result.status,
                        "skipped": result.skipped,
                    }
                    for result in results
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if not results:
            print("pdf2md: no PDF files found in {}".format(args.input))
        for result in results:
            state = "skipped" if result.skipped else "written"
            print(
                "{}: {} -> {} ({})".format(
                    state, result.source_path, result.output_path, result.status
                )
            )
    return 1 if any(result.status in {"needs_ocr", "empty_text"} for result in results) else 0


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
        user_prompt=args.prompt,
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
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
