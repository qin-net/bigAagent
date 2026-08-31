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
    FeedbackError,
    analyze_stock,
    feedback_on_run,
    format_cli_text,
    format_feedback_result,
    write_run_json,
)

DEFAULT_DB_PATH = os.environ.get(
    "INSIGHTAGENT_DB_PATH", "data/insightagent.db"
)
DEFAULT_KB_PATH = os.environ.get(
    "INSIGHTAGENT_KB_PATH", "data/kb.db"
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
    feedback = commands.add_parser(
        "feedback", help="Post-decision feedback and optional rerun"
    )
    feedback.add_argument("run_id")
    feedback.add_argument("--path", default=DEFAULT_DB_PATH)
    feedback.add_argument(
        "--artifact-root",
        default=DEFAULT_ARTIFACT_ROOT,
    )
    feedback.add_argument("--model", default="deepseek-v4-flash")
    feedback.add_argument("--json", action="store_true", dest="as_json")
    feedback.add_argument(
        "--prompt",
        default="none",
        help="Optional user intent prompt with optional #tags",
    )
    feedback.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable DeepSeek thinking mode",
    )
    feedback.add_argument(
        "--refresh-snapshot",
        action="store_true",
        help="Not implemented; exits with an error if set",
    )
    pdf2md = commands.add_parser(
        "pdf2md", help="Convert local PDF files to reviewable Markdown"
    )
    pdf2md.add_argument("input", nargs="?", default=str(DEFAULT_INCOMING_DIR))
    pdf2md.add_argument("--out", default=str(DEFAULT_MARKDOWN_DIR))
    pdf2md.add_argument("--force", action="store_true")
    pdf2md.add_argument("--json", action="store_true", dest="as_json")
    kb = commands.add_parser("kb", help="Methodology catalog")
    kb.add_argument("--path", default=DEFAULT_KB_PATH)
    kb.add_argument(
        "--research-path",
        default=DEFAULT_DB_PATH,
        help="Research log database (sessions); distill writes here, cards go to --path",
    )
    kb_commands = kb.add_subparsers(dest="kb_command", required=True)
    kb_commands.add_parser("seed", help="Insert built-in approved cards if empty")
    imported = kb_commands.add_parser("import", help="Import a candidate from markdown")
    imported.add_argument("markdown")
    approve = kb_commands.add_parser("approve")
    approve.add_argument("entry_id")
    show = kb_commands.add_parser("show")
    show.add_argument("entry_id")
    listed = kb_commands.add_parser("list")
    listed.add_argument("--status", default="")
    distill = kb_commands.add_parser("distill")
    distill.add_argument("markdown")
    distill.add_argument("--scope", required=True)
    distill.add_argument("--model", default="deepseek-v4-flash")
    distill.add_argument("--json", action="store_true", dest="as_json")
    distill.add_argument("--no-thinking", action="store_true")
    track = commands.add_parser(
        "track", help="Tracking-task: compare thesis to current snapshots"
    )
    track.add_argument("thesis_id")
    track.add_argument("--path", default=DEFAULT_DB_PATH)
    track.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    track.add_argument(
        "--fixture",
        action="store_true",
        help="Use packaged fixture snapshots instead of AKShare",
    )
    track.add_argument("--fixtures-dir", default=None)
    track.add_argument("--model", default="deepseek-v4-flash")
    track.add_argument("--json", action="store_true", dest="as_json")
    track.add_argument("--no-thinking", action="store_true")
    track.add_argument(
        "--prompt",
        default="none",
        help="Optional pre-track intent prompt with optional #tags",
    )
    track_feedback = commands.add_parser(
        "track-feedback",
        help="Post-track feedback; this_run waits for next track, #rerun re-runs",
    )
    track_feedback.add_argument("thesis_id")
    track_feedback.add_argument("--path", default=DEFAULT_DB_PATH)
    track_feedback.add_argument(
        "--artifact-root",
        default=DEFAULT_ARTIFACT_ROOT,
    )
    track_feedback.add_argument("--model", default="deepseek-v4-flash")
    track_feedback.add_argument("--json", action="store_true", dest="as_json")
    track_feedback.add_argument(
        "--prompt",
        default="none",
        help="Optional post-track intent prompt with optional #tags",
    )
    track_feedback.add_argument("--no-thinking", action="store_true")
    track_feedback.add_argument(
        "--fixture",
        action="store_true",
        help="Use packaged fixture snapshots instead of AKShare",
    )
    track_feedback.add_argument("--fixtures-dir", default=None)
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
    if args.command == "feedback":
        return await _run_feedback(args)
    if args.command == "pdf2md":
        return _run_pdf2md(args)
    if args.command == "kb":
        return await _run_kb(args)
    if args.command == "track":
        return await _run_track(args)
    if args.command == "track-feedback":
        return await _run_track_feedback(args)

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


async def _run_kb(args: argparse.Namespace) -> int:
    from .methodology import MethodologyCatalog
    from .tracking_agent import distill_chapter

    catalog_db = SQLiteDatabase(args.path)
    catalog = MethodologyCatalog(catalog_db)
    catalog.ensure_seeded()
    command = args.kb_command
    if command == "seed":
        print(
            json.dumps(
                {"seeded": True, "count": len(catalog.list_payloads())},
                ensure_ascii=False,
            )
        )
        return 0
    if command == "import":
        stored = catalog.import_markdown(Path(args.markdown))
        print(json.dumps(stored, ensure_ascii=False, indent=2))
        return 0
    if command == "approve":
        stored = catalog.approve(args.entry_id)
        print(json.dumps(stored, ensure_ascii=False, indent=2))
        return 0
    if command == "show":
        item = catalog.get(args.entry_id)
        if item is None:
            print("unknown entry: {}".format(args.entry_id))
            return 1
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0
    if command == "list":
        statuses = None
        if args.status:
            statuses = [
                item.strip()
                for item in args.status.split(",")
                if item.strip()
            ]
        items = catalog.list_payloads(statuses=statuses)
        print(
            json.dumps(
                [
                    {
                        "id": item["id"],
                        "status": item["status"],
                        "version": item["version"],
                        "title": item["title"],
                        "scope": item["scope"],
                    }
                    for item in items
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if command == "distill":
        result = await distill_chapter(
            args.markdown,
            scope=args.scope,
            database=SQLiteDatabase(args.research_path),
            kb_database=catalog_db,
            llm_adapter=build_llm(args.model),
            model=args.model,
            thinking_enabled=not args.no_thinking,
        )
        print(
            json.dumps(
                {
                    "submitted_ids": result.submitted_ids,
                    "notes": result.notes,
                    "session_id": result.session_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return 1


async def _run_track(args: argparse.Namespace) -> int:
    from .tracking_agent import track_thesis

    database = SQLiteDatabase(args.path)
    result = await track_thesis(
        args.thesis_id,
        database=database,
        llm_adapter=build_llm(args.model),
        artifact_root=args.artifact_root,
        model=args.model,
        thinking_enabled=not args.no_thinking,
        fixture=args.fixture,
        fixtures_dir=args.fixtures_dir,
        user_prompt=(args.prompt or "").strip() or "none",
    )
    payload = {
        "thesis_id": result.thesis_id,
        "run_id": result.run_id,
        "session_id": result.session_id,
        "prescreen": result.prescreen,
        "deliverable": result.deliverable,
        "skill_calls": [
            {
                "agent": item.get("agent"),
                "status": item.get("status"),
                "question": item.get("question"),
            }
            for item in result.skill_calls
        ],
        "intent": (
            result.intent.model_dump(mode="json") if result.intent else None
        ),
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    deliverable = result.deliverable
    user = deliverable.get("user_output") or {}
    print("thesis: {}".format(result.thesis_id))
    print("status: {}".format(deliverable.get("status")))
    print("summary: {}".format(user.get("summary") or deliverable.get("work_summary")))
    synthesis = deliverable.get("synthesis") or ""
    if synthesis:
        print("synthesis: {}".format(synthesis))
    evaluations = deliverable.get("expert_evaluations") or []
    if evaluations:
        print(
            "evaluations: {}".format(
                ", ".join(
                    "{}:{}".format(item.get("agent"), item.get("verdict"))
                    for item in evaluations
                )
            )
        )
    calls = result.skill_calls
    if not calls:
        print("analysts: none")
    else:
        print(
            "analysts: {}".format(
                ", ".join(
                    "{}({})".format(item.get("agent"), item.get("status"))
                    for item in calls
                )
            )
        )
    if result.show_intent_echo and result.intent is not None:
        from .user_intent import format_intent_echo

        print(format_intent_echo(result.intent))
    return 0


async def _run_track_feedback(args: argparse.Namespace) -> int:
    from .tracking_agent import (
        TrackFeedbackError,
        feedback_on_track,
        format_track_feedback_result,
    )

    prompt = (args.prompt or "").strip() or "none"
    if prompt == "none":
        return 0
    database = SQLiteDatabase(args.path)
    await database.initialize()
    try:
        result = await feedback_on_track(
            args.thesis_id,
            database=database,
            llm_adapter=build_llm(args.model),
            artifact_root=args.artifact_root,
            user_prompt=prompt,
            model=args.model,
            thinking_enabled=not args.no_thinking,
            fixture=args.fixture,
            fixtures_dir=args.fixtures_dir,
        )
    except TrackFeedbackError as error:
        print("track-feedback error: {}".format(error))
        return 1
    payload = {
        "skipped": result.skipped,
        "noop": result.noop,
        "parent_run_id": result.parent_run_id,
        "intent": (
            result.intent.model_dump(mode="json") if result.intent else None
        ),
        "track": (
            {
                "thesis_id": result.track_result.thesis_id,
                "run_id": result.track_result.run_id,
                "deliverable": result.track_result.deliverable,
            }
            if result.track_result
            else None
        ),
        "error": result.error,
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    text = format_track_feedback_result(result)
    if text:
        print(text)
    return 0


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


async def _run_feedback(args: argparse.Namespace) -> int:
    if args.refresh_snapshot:
        print("--refresh-snapshot is not implemented in this slice")
        return 1
    prompt = (args.prompt or "").strip() or "none"
    if prompt == "none":
        return 0
    database = SQLiteDatabase(args.path)
    await database.initialize()
    try:
        result = await feedback_on_run(
            args.run_id.strip(),
            database=database,
            llm_adapter=build_llm(args.model),
            artifact_root=args.artifact_root,
            user_prompt=prompt,
            model=args.model,
            thinking_enabled=not args.no_thinking,
            unbound_policy="abstain",
        )
    except FeedbackError as error:
        print("feedback error: {}".format(error))
        return 1
    if result.outcome is not None:
        write_run_json(result.outcome, args.path)
        if args.as_json:
            print(json.dumps(result.outcome.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(format_cli_text(result.outcome))
        if result.outcome.run.status == "failed" or result.error:
            return 1
        return 0
    if args.as_json:
        print(
            json.dumps(
                {
                    "skipped": result.skipped,
                    "noop": result.noop,
                    "parent_run_id": result.parent_run_id,
                    "intent": (
                        result.intent.model_dump(mode="json")
                        if result.intent
                        else None
                    ),
                    "error": result.error,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        text = format_feedback_result(result)
        if text:
            print(text)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
