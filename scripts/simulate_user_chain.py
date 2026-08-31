"""Replay a cashflow-disciplined user through analyze/track into local DBs."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from insightagent.env import repo_root
from tests.simulated_investor import run_simulated_investor


async def main() -> int:
    root = repo_root()
    summary = await run_simulated_investor(
        agent_db=str(root / "data" / "insightagent.db"),
        artifacts=str(root / "data" / "artifacts"),
        board_db=str(root / "data" / "board.db"),
        seed_quotes=False,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
