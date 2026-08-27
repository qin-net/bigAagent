from __future__ import annotations

import sqlite3
import uuid
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .contracts import DailyBar, NoticeHeadline, QuoteInput

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
                CREATE TABLE IF NOT EXISTS bar_daily (
                    stock_code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, turnover REAL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (stock_code, trade_date)
                );
                CREATE TABLE IF NOT EXISTS notice_headline (
                    stock_code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    published_at TEXT,
                    url TEXT,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (stock_code, title, published_at)
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    user_id TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, stock_code)
                );
                CREATE TABLE IF NOT EXISTS deep_fetch_queue (
                    stock_code TEXT PRIMARY KEY,
                    priority INTEGER NOT NULL DEFAULT 10,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('version', '1')"
            )

    def initialize_research(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_job (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    parent_run_id TEXT,
                    run_id TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    prompt TEXT,
                    noop INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_job_stock_status
                    ON research_job(stock_code, status, created_at DESC);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(research_job)").fetchall()}
            if "rerun_dimensions" not in columns:
                connection.execute("ALTER TABLE research_job ADD COLUMN rerun_dimensions TEXT NOT NULL DEFAULT ''")

    def create_research_job(
        self, stock_code: str, *, kind: str = "analyze", prompt: str = "none",
        parent_run_id: Optional[str] = None, user_id: str = "local",
    ) -> dict:
        if len(stock_code) != 6 or not stock_code.isdigit():
            raise ValueError("invalid stock code")
        if kind not in {"analyze", "feedback"}:
            raise ValueError("invalid research job kind")
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT * FROM research_job WHERE stock_code=? AND status IN ('queued','running') ORDER BY created_at LIMIT 1",
                (stock_code,),
            ).fetchone()
            if active:
                return {**dict(active), "existing": True}
            job_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO research_job(job_id,user_id,stock_code,kind,parent_run_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)",
                (job_id, user_id, stock_code, kind, parent_run_id, None, now, now),
            )
            row = connection.execute("SELECT * FROM research_job WHERE job_id=?", (job_id,)).fetchone()
        return {**dict(row), "existing": False}

    def recover_research(self, *, older_than_minutes: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
        with self._connection() as connection:
            result = connection.execute(
                "UPDATE research_job SET status='queued', updated_at=? WHERE status='running' AND updated_at<?",
                (utc_now(), cutoff),
            )
        return result.rowcount

    def claim_research(self) -> Optional[dict]:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM research_job WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE research_job SET status='running', updated_at=? WHERE job_id=? AND status='queued'",
                (now, row["job_id"]),
            )
        return dict(row)

    def finish_research(
        self, job_id: str, *, run_id: Optional[str] = None,
        error: Optional[str] = None, noop: bool = False,
        rerun_dimensions: Optional[Sequence[str]] = None,
    ) -> None:
        now = utc_now()
        status = "failed" if error else "success"
        with self._connection() as connection:
            connection.execute(
                "UPDATE research_job SET status=?,run_id=?,error=?,noop=?,rerun_dimensions=?,updated_at=? WHERE job_id=?",
                (status, run_id, (error or "")[:300] or None, int(noop), json.dumps(list(rerun_dimensions or []), ensure_ascii=False), now, job_id),
            )

    def research_job(self, job_id: str) -> Optional[dict]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT job_id,user_id,stock_code,kind,parent_run_id,run_id,status,error,noop,rerun_dimensions,created_at,updated_at FROM research_job WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def research_history(self, stock_code: str, limit: int = 20) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job_id,stock_code,kind,parent_run_id,run_id,status,error,noop,rerun_dimensions,created_at,updated_at FROM research_job WHERE stock_code=? ORDER BY created_at DESC LIMIT ?",
                (stock_code, min(max(limit, 1), 20)),
            ).fetchall()
        return [dict(row) for row in rows]

    def research_job_by_run(self, run_id: str) -> Optional[dict]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT job_id,stock_code,kind,parent_run_id,run_id,status,error,noop,rerun_dimensions,created_at,updated_at FROM research_job WHERE run_id=? ORDER BY updated_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def current_research(self, stock_code: str) -> Optional[dict]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT job_id,stock_code,kind,parent_run_id,run_id,status,error,noop,rerun_dimensions,created_at,updated_at FROM research_job WHERE stock_code=? AND status='success' AND run_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                (stock_code,),
            ).fetchone()
        return dict(row) if row else None

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

    def enqueue_deep(self, stock_code: str, *, reason: str = "detail", priority: int = 10) -> dict:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO deep_fetch_queue(stock_code, priority, reason, available_at, updated_at) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(stock_code) DO UPDATE SET priority=MIN(priority, excluded.priority), reason=excluded.reason, status=CASE WHEN status='done' THEN 'pending' ELSE status END, available_at=excluded.available_at, updated_at=excluded.updated_at",
                (stock_code, priority, reason, now, now),
            )
            row = connection.execute("SELECT stock_code, priority, reason, status, attempts, last_error FROM deep_fetch_queue WHERE stock_code=?", (stock_code,)).fetchone()
        return dict(row)

    def claim_deep(self) -> Optional[dict]:
        now = utc_now()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM deep_fetch_queue WHERE status IN ('pending','retry') AND available_at<=? ORDER BY priority ASC, updated_at ASC LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            connection.execute("UPDATE deep_fetch_queue SET status='running', attempts=attempts+1, updated_at=? WHERE stock_code=?", (now, row["stock_code"]))
            return dict(row)

    def finish_deep(self, stock_code: str, *, error: Optional[str] = None) -> None:
        now = utc_now()
        with self._connection() as connection:
            if error:
                connection.execute("UPDATE deep_fetch_queue SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'retry' END, available_at=?, last_error=?, updated_at=? WHERE stock_code=?", (now, error[:1000], now, stock_code))
            else:
                connection.execute("UPDATE deep_fetch_queue SET status='done', last_error=NULL, updated_at=? WHERE stock_code=?", (now, stock_code))

    def save_deep(self, bars: Sequence[DailyBar], notices: Sequence[NoticeHeadline], *, source: str) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.executemany("INSERT OR REPLACE INTO bar_daily(stock_code, trade_date, open, high, low, close, volume, turnover, source, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(b.stock_code, b.trade_date, b.open, b.high, b.low, b.close, b.volume, b.turnover, source, now) for b in bars[-250:]])
            connection.executemany("INSERT OR REPLACE INTO notice_headline(stock_code, title, published_at, url, source, updated_at) VALUES(?, ?, ?, ?, ?, ?)", [(n.stock_code, n.title, n.published_at, n.url, n.source, now) for n in notices[:30]])

    def bars(self, stock_code: str, limit: int = 120) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT trade_date, open, high, low, close, volume, turnover, source, updated_at FROM bar_daily WHERE stock_code=? ORDER BY trade_date DESC LIMIT ?", (stock_code, min(max(limit, 1), 250))).fetchall()
        return [dict(row) for row in reversed(rows)]

    def notices(self, stock_code: str, limit: int = 30) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT title, published_at, url, source, updated_at FROM notice_headline WHERE stock_code=? ORDER BY published_at DESC LIMIT ?", (stock_code, min(max(limit, 1), 30))).fetchall()
        return [dict(row) for row in rows]

    def watchlist(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT w.stock_code, q.name, q.price, q.change_pct FROM watchlist w LEFT JOIN quote_snapshot q ON q.batch_id=(SELECT value FROM board_meta WHERE key='current_quote_batch_id') AND q.stock_code=w.stock_code WHERE w.user_id='local' ORDER BY w.created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def add_watch(self, stock_code: str) -> None:
        with self._connection() as connection:
            connection.execute("INSERT OR IGNORE INTO watchlist(user_id, stock_code, created_at) VALUES('local', ?, ?)", (stock_code, utc_now()))

    def remove_watch(self, stock_code: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM watchlist WHERE user_id='local' AND stock_code=?", (stock_code,))
