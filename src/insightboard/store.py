from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .contracts import QuoteInput

STALE_AFTER = timedelta(minutes=45)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class BoardStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quote_batch (
                    batch_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quote_snapshot (
                    batch_id TEXT NOT NULL REFERENCES quote_batch(batch_id),
                    stock_code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    industry TEXT,
                    market TEXT,
                    price REAL,
                    change_pct REAL,
                    open REAL,
                    high REAL,
                    low REAL,
                    volume REAL,
                    turnover REAL,
                    pe REAL,
                    pb REAL,
                    PRIMARY KEY (batch_id, stock_code)
                );
                CREATE INDEX IF NOT EXISTS quote_snapshot_batch_sort
                    ON quote_snapshot(batch_id, turnover DESC, stock_code);
                CREATE INDEX IF NOT EXISTS quote_snapshot_batch_name
                    ON quote_snapshot(batch_id, name);
                CREATE TABLE IF NOT EXISTS board_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_run (
                    run_id TEXT PRIMARY KEY,
                    layer TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    source TEXT
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('version', '1')"
            )

    def replace_quotes(self, quotes: Sequence[QuoteInput], *, source: str) -> str:
        if not quotes:
            raise ValueError("Cannot publish an empty quote batch")
        codes = [quote.stock_code for quote in quotes]
        if len(codes) != len(set(codes)):
            raise ValueError("Quote batch contains duplicate stock codes")
        if any(len(code) != 6 or not code.isdigit() for code in codes):
            raise ValueError("Quote batch contains an invalid stock code")
        if sum(quote.price is not None for quote in quotes) / len(quotes) < 0.8:
            raise ValueError("Quote batch has fewer than 80% non-null prices")

        batch_id = uuid.uuid4().hex
        as_of = utc_now()
        started_at = utc_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO ingest_run(run_id, layer, started_at, status, source) VALUES(?, 'quotes', ?, 'running', ?)",
                (batch_id, started_at, source),
            )
            connection.execute(
                "INSERT INTO quote_batch(batch_id, source, as_of, status, row_count, created_at) VALUES(?, ?, ?, 'staging', ?, ?)",
                (batch_id, source, as_of, len(quotes), as_of),
            )
            connection.executemany(
                """
                INSERT INTO quote_snapshot(
                    batch_id, stock_code, name, industry, market, price, change_pct,
                    open, high, low, volume, turnover, pe, pb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id, quote.stock_code, quote.name, quote.industry,
                        quote.market, quote.price, quote.change_pct, quote.open,
                        quote.high, quote.low, quote.volume, quote.turnover,
                        quote.pe, quote.pb,
                    )
                    for quote in quotes
                ],
            )
            # Publishing the pointer within this transaction prevents readers seeing a partial batch.
            connection.execute("UPDATE quote_batch SET status='succeeded' WHERE batch_id=?", (batch_id,))
            connection.execute(
                "INSERT INTO board_meta(key, value) VALUES('current_quote_batch_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (batch_id,),
            )
            connection.execute(
                "UPDATE ingest_run SET finished_at=?, status='succeeded', row_count=? WHERE run_id=?",
                (utc_now(), len(quotes), batch_id),
            )
        return batch_id

    def record_failure(self, *, source: str, error: str) -> str:
        run_id = uuid.uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO ingest_run(run_id, layer, started_at, finished_at, status, error, source) VALUES(?, 'quotes', ?, ?, 'failed', ?, ?)",
                (run_id, now, now, error[:1000], source),
            )
        return run_id

    def quote_page(
        self, *, page: int = 1, size: int = 100, q: str = "", industry: str = "", sort: str = "turnover", order: str = "desc"
    ) -> dict:
        page = max(page, 1)
        size = min(max(size, 1), 100)
        allowed_sorts = {"stock_code", "name", "price", "change_pct", "turnover"}
        sort = sort if sort in allowed_sorts else "turnover"
        direction = "ASC" if order.lower() == "asc" else "DESC"
        batch = self._current_batch()
        if batch is None:
            return {"batch_id": None, "as_of": None, "total": 0, "items": []}
        filters = ["batch_id=?"]
        values: list[object] = [batch["batch_id"]]
        if q:
            filters.append("(stock_code LIKE ? OR name LIKE ?)")
            values.extend(["%{}%".format(q), "%{}%".format(q)])
        if industry:
            filters.append("industry=?")
            values.append(industry)
        where = " AND ".join(filters)
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(*) FROM quote_snapshot WHERE " + where, values).fetchone()[0]
            rows = connection.execute(
                "SELECT stock_code, name, industry, market, price, change_pct, open, high, low, volume, turnover, pe, pb "
                "FROM quote_snapshot WHERE {} ORDER BY {} {} NULLS LAST, stock_code ASC LIMIT ? OFFSET ?".format(where, sort, direction),
                values + [size, (page - 1) * size],
            ).fetchall()
        return {"batch_id": batch["batch_id"], "as_of": batch["as_of"], "total": total, "items": [dict(row) for row in rows]}

    def status(self) -> dict:
        batch = self._current_batch()
        with self._connection() as connection:
            latest = connection.execute(
                "SELECT status, finished_at, row_count, error, source FROM ingest_run WHERE layer='quotes' ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        as_of = batch["as_of"] if batch else None
        stale = True
        if as_of:
            stale = datetime.now(timezone.utc) - datetime.fromisoformat(as_of) > STALE_AFTER
        return {
            "quotes_as_of": as_of,
            "stock_count": batch["row_count"] if batch else 0,
            "stale": stale,
            "current_batch_id": batch["batch_id"] if batch else None,
            "last_run": dict(latest) if latest else None,
        }

    def _current_batch(self) -> Optional[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                "SELECT batch_id, as_of, row_count, source FROM quote_batch WHERE batch_id=(SELECT value FROM board_meta WHERE key='current_quote_batch_id') AND status='succeeded'"
            ).fetchone()
