from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from insightboard.api import create_app
from insightboard.collector import AkshareQuoteCollector, collect_once
from insightboard.contracts import CollectionResult, QuoteInput
from insightboard.store import BoardStore
from insightboard import __main__ as board_main


def quote(code: str, *, name: str = "测试公司", turnover: float = 100.0) -> QuoteInput:
    return QuoteInput(
        stock_code=code,
        name=name,
        industry="测试行业",
        market="sz",
        price=10.0,
        change_pct=1.2,
        turnover=turnover,
    )


class FakeCollector:
    source = "fixture"

    def __init__(self, rows: list[QuoteInput]) -> None:
        self.rows = rows

    def collect(self) -> CollectionResult:
        return CollectionResult(source=self.source, quotes=self.rows)


def test_store_publishes_complete_batch_and_pages(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))
    store.initialize()
    batch_id = store.replace_quotes(
        [quote("000002", name="乙公司", turnover=200), quote("000001", name="甲公司", turnover=100)],
        source="fixture",
    )

    result = store.quote_page(page=1, size=1, sort="turnover")
    assert result["batch_id"] == batch_id
    assert result["total"] == 2
    assert result["items"][0]["stock_code"] == "000002"
    assert store.quote_page(q="甲公司")["items"][0]["stock_code"] == "000001"


def test_failed_collection_keeps_previous_batch(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))
    store.initialize()
    old_batch = store.replace_quotes([quote("000001")], source="fixture")
    store.record_failure(source="fixture", error="network down")

    status = store.status()
    assert status["current_batch_id"] == old_batch
    assert status["last_run"]["status"] == "failed"
    assert store.quote_page()["items"][0]["stock_code"] == "000001"


def test_collect_once_records_failure(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))

    class Broken:
        source = "broken"

        def collect(self):
            raise RuntimeError("source unavailable")

    store.initialize()
    try:
        asyncio.run(collect_once(store, Broken()))
    except RuntimeError:
        pass
    else:
        raise AssertionError("collection should fail")
    assert store.status()["last_run"]["status"] == "failed"


def test_api_exposes_health_and_quotes(tmp_path):
    db_path = str(tmp_path / "board.db")
    store = BoardStore(db_path)
    store.initialize()
    store.replace_quotes([quote("000001")], source="fixture")
    client = TestClient(create_app(db_path))

    assert client.get("/api/v1/meta/health").json() == {"status": "ok"}
    response = client.get("/api/v1/quotes")
    assert response.status_code == 200
    assert response.json()["items"][0]["stock_code"] == "000001"
    assert client.get("/").status_code == 200


def test_old_batch_is_stale(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))
    store.initialize()
    batch_id = store.replace_quotes([quote("000001")], source="fixture")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with store._connection() as connection:
        connection.execute("UPDATE quote_batch SET as_of=? WHERE batch_id=?", (old, batch_id))
    assert store.status()["stale"] is True


def test_collect_once_cli_reports_source_failure_without_traceback(tmp_path, monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise ConnectionError("remote closed")

    monkeypatch.setattr(board_main, "collect_once", fail)
    exit_code = board_main.main(["--db", str(tmp_path / "board.db"), "collect-once"])
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "行情采集失败" in output
    assert "Traceback" not in output
    assert "上一批成功数据" in output


def test_tencent_mapping_normalizes_code_and_turnover_unit():
    item = AkshareQuoteCollector._map_tencent_row(
        {"code": "sz000001", "name": "平安银行", "zxj": "10.25", "zdf": "1.5", "volume": "200", "turnover": "300", "pe_ttm": "5.2"}
    )
    assert item is not None
    assert item.stock_code == "000001"
    assert item.market == "sz"
    assert item.turnover == 3_000_000


def test_sina_mapping_uses_column_order_when_labels_are_mojibake():
    item = AkshareQuoteCollector._map_sina_row(
        {"bad1": "sh600000", "bad2": "浦发银行", "bad3": 10.0, "bad4": 0.1, "bad5": 1.0, "bad6": 9.9, "bad7": 10.1, "bad8": 10.0, "bad9": 0, "bad10": 10.2, "bad11": 9.8, "bad12": 100, "bad13": 1000, "bad14": "10:00"}
    )
    assert item is not None
    assert item.stock_code == "600000"
    assert item.price == 10.0
