from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def load_dotenv() -> None:
    """Load gitignored `.env` into os.environ without overriding existing keys."""
    for path in _dotenv_candidates():
        if path.is_file():
            _apply_dotenv(path)
            return


def _dotenv_candidates() -> Iterable[Path]:
    yield Path.cwd() / ".env"
    yield Path(__file__).resolve().parents[2] / ".env"


def _apply_dotenv(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _unquote(value.strip())


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
