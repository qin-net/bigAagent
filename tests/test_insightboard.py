from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from insightboard.api import create_app
from insightboard.collector import AkshareQuoteCollector, collect_once
from insightboard.contracts import CollectionResult, DailyBar, NoticeHeadline, QuoteInput
from insightboard.store import BoardStore
from insightboard import __main__ as board_main


def quote(code: str, *, name: str = "测试公司", turnover: float = 100.0, price: float = 10.0) -> QuoteInput:
    return QuoteInput(
        stock_code=code,
        name=name,
        industry="测试行业",
        market="sz",
        price=price,
        change_pct=1.2,
        turnover=turnover,
    )


class FakeCollector:
    source = "fixture"

    def __init__(self, rows: list[QuoteInput], bars: list[DailyBar] | None = None) -> None:
        self.rows = rows
        self.bars = bars or []

    def collect(self) -> CollectionResult:
        return CollectionResult(source=self.source, quotes=self.rows)

    def collect_deep(self, stock_code: str) -> tuple[list[DailyBar], list[NoticeHeadline]]:
        items = [bar for bar in self.bars if bar.stock_code == stock_code]
        return items, []


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
    assert "采集行情" in client.get("/").text
    assert "折线" in client.get("/").text


def test_detail_workflow_follows_business_sequence(tmp_path):
    html = TestClient(create_app(str(tmp_path / "board.db"))).get("/").text
    steps = [
        html.index('id="research-start"'),
        html.index('id="paper-buy"'),
        html.index('id="track-start"'),
        html.index('id="workflow-open-profile"'),
    ]
    assert steps == sorted(steps)
    assert "建立研究基线" in html
    assert "决定是否投入" in html
    assert "手动复核策略" in html
    assert "记忆与用户画像" in html


def test_quotes_collect_button_publishes_batch(tmp_path):
    import time
    db_path = str(tmp_path / "board.db")
    BoardStore(db_path).initialize()
    client = TestClient(create_app(db_path, collector=FakeCollector([quote("000002", name="乙公司")])))
    started = client.post("/api/v1/quotes/collect")
    assert started.status_code == 200
    assert started.json()["collecting"] is True
    payload = {}
    deadline = time.time() + 3
    while time.time() < deadline:
        payload = client.get("/api/v1/meta/ingest").json()
        if not payload.get("collecting") and payload.get("stock_count"):
            break
        time.sleep(0.05)
    assert payload["stock_count"] == 1
    assert client.get("/api/v1/quotes").json()["items"][0]["stock_code"] == "000002"
    assert client.post("/api/v1/quotes/collect").status_code in {200, 409}


def test_bars_collect_button_saves_daily_line(tmp_path):
    import time
    db_path = str(tmp_path / "board.db")
    BoardStore(db_path).initialize()
    bars = [
        DailyBar("000002", "2026-08-28", 10.0, 10.5, 9.8, 10.2, 1.0, 1.0),
        DailyBar("000002", "2026-08-29", 10.2, 10.8, 10.0, 10.6, 1.0, 1.0),
        DailyBar("000002", "2026-08-30", 10.6, 11.0, 10.4, 10.9, 1.0, 1.0),
    ]
    client = TestClient(create_app(db_path, collector=FakeCollector([quote("000002")], bars=bars)))
    started = client.post("/api/v1/stocks/000002/bars/collect")
    assert started.status_code == 200
    assert started.json()["collecting"] is True
    items = []
    deadline = time.time() + 3
    while time.time() < deadline:
        items = client.get("/api/v1/bars/000002").json()["items"]
        if items:
            break
        time.sleep(0.05)
    assert [item["close"] for item in items] == [10.2, 10.6, 10.9]
    html = client.get("/").text
    assert "chart-mode-line" in html
    assert "拉取日线" in html


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


def test_deep_queue_bars_notices_and_watchlist(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))
    store.initialize()
    store.replace_quotes([quote("000001", name="平安银行")], source="fixture")

    queue = store.enqueue_deep("000001", reason="detail", priority=5)
    assert queue["status"] == "pending"
    claimed = store.claim_deep()
    assert claimed["stock_code"] == "000001"
    store.save_deep(
        [DailyBar("000001", "2026-08-24", 10, 11, 9, 10.5, 100, 1000)],
        [NoticeHeadline("000001", "年度报告", "2026-08-24", "https://example.invalid", "fixture")],
        source="fixture",
    )
    store.finish_deep("000001")
    assert store.bars("000001")[0]["close"] == 10.5
    assert store.notices("000001")[0]["title"] == "年度报告"
    store.add_watch("000001")
    assert store.watchlist()[0]["name"] == "平安银行"
    store.remove_watch("000001")
    assert store.watchlist() == []


@pytest.mark.asyncio
async def test_projected_run_exposes_carried_memory_and_preferences(tmp_path, monkeypatch):
    from insightagent.business_contracts import RunRecord
    from insightagent.contracts import TaskStatus, utc_now
    from insightagent.persistence import SQLiteDatabase, SQLiteStateStore
    from insightagent.research_store import ResearchStore
    from insightagent.user_contracts import UserPreference
    from insightagent.user_store import UserStore
    from insightboard.research import load_projected_run

    agent_db = str(tmp_path / "insightagent.db")
    monkeypatch.setenv("INSIGHTAGENT_DB_PATH", agent_db)
    database = SQLiteDatabase(agent_db)
    await database.initialize()
    states = SQLiteStateStore(database)
    parent = await states.load_or_create(
        agent_name="fundamental",
        thesis_id="000001-initial",
        stock_code="000001",
    )
    parent.private_memory = {"memory_summary": "盯经营现金流证伪"}
    parent.status = TaskStatus.SUCCESS
    parent = await states.save(parent, expected_version=parent.version)
    child = await states.load_or_create(
        agent_name="fundamental",
        thesis_id="000001-initial",
        stock_code="000001",
        parent_session_id=parent.session_id,
    )
    child.status = TaskStatus.SUCCESS
    await states.save(child, expected_version=child.version)
    now = utc_now().isoformat()
    await UserStore(database).save_preference(
        UserPreference(
            preference_id="pref-1",
            user_id="local",
            status="active",
            current_version="1",
            kind="constraint",
            scope="fundamental",
            stock_code="000001",
            trigger="盯经营现金流",
            title="盯经营现金流",
            statement="估值必须对照经营现金流",
            source="user_feedback",
            source_utterance_id="u1",
            source_run_id="run-board-1",
            created_at=now,
            updated_at=now,
        )
    )
    run = RunRecord(
        run_id="run-board-1",
        stock_code="000001",
        thesis_id="000001-initial",
        status="success",
        session_ids={"fundamental": child.session_id},
    )
    await ResearchStore(database).save_run(run)
    projected = await load_projected_run(run.run_id, db_path=agent_db)
    assert projected["memories"]["fundamental"]["memory_summary"] == "盯经营现金流证伪"
    assert projected["memories"]["fundamental"]["carried"] is True
    assert projected["preferences"][0]["statement"] == "估值必须对照经营现金流"


@pytest.mark.asyncio
async def test_research_api_returns_memory_on_current_run(tmp_path, monkeypatch):
    from insightagent.business_contracts import RunRecord
    from insightagent.contracts import TaskStatus
    from insightagent.persistence import SQLiteDatabase, SQLiteStateStore
    from insightagent.research_store import ResearchStore

    agent_db = str(tmp_path / "insightagent.db")
    monkeypatch.setenv("INSIGHTAGENT_DB_PATH", agent_db)
    database = SQLiteDatabase(agent_db)
    await database.initialize()
    states = SQLiteStateStore(database)
    state = await states.load_or_create(
        agent_name="fundamental",
        thesis_id="000001-initial",
        stock_code="000001",
    )
    state.private_memory = {"memory_summary": "盯经营现金流证伪"}
    state.status = TaskStatus.SUCCESS
    await states.save(state, expected_version=state.version)
    run = RunRecord(
        run_id="run-api-1",
        stock_code="000001",
        thesis_id="000001-initial",
        status="success",
        session_ids={"fundamental": state.session_id},
    )
    await ResearchStore(database).save_run(run)
    board_db = str(tmp_path / "board.db")
    store = BoardStore(board_db)
    store.initialize()
    store.initialize_research()
    store.replace_quotes([quote("000001")], source="fixture")
    job = store.create_research_job("000001")
    store.finish_research(job["job_id"], run_id="run-api-1")
    client = TestClient(create_app(board_db))
    payload = client.get("/api/v1/research/stocks/000001/current").json()
    assert payload["memories"]["fundamental"]["memory_summary"] == "盯经营现金流证伪"
    html = client.get("/").text
    assert "research-start" in html
    assert "track-start" in html


@pytest.mark.asyncio
async def test_track_api_projects_deliverable(tmp_path, monkeypatch):
    from insightagent.business_contracts import RunRecord
    from insightagent.persistence import SQLiteDatabase
    from insightagent.research_store import ResearchStore

    agent_db = str(tmp_path / "insightagent.db")
    monkeypatch.setenv("INSIGHTAGENT_DB_PATH", agent_db)
    database = SQLiteDatabase(agent_db)
    await database.initialize()
    store = ResearchStore(database)
    run = RunRecord(
        run_id="track-api-1",
        stock_code="000001",
        thesis_id="000001-initial",
        mode="track_day",
        status="success",
        session_ids={"tracking": "sess-track"},
    )
    await store.save_run(run)
    await store.save_timeline(
        "000001-initial",
        {
            "schema_version": "1",
            "mode": "track_day",
            "deliverable": {
                "status": "review",
                "work_summary": "估值带上移，维持观察",
                "thinking": "对比基线价格",
                "synthesis": "不改持有，但要复核技术面",
                "expert_evaluations": [
                    {
                        "agent": "technical",
                        "reliability": "medium",
                        "verdict": "discount",
                        "gaps": [],
                        "notes": "量能不足",
                    }
                ],
                "evidence_refs": [],
                "triggers_hit": ["price_move"],
                "agent_skill_calls": [],
                "decision_required": False,
                "user_output": {
                    "title": "本次跟踪更新",
                    "summary": "需要复核技术面",
                    "holding_advice": "review",
                    "key_changes": ["价格偏离基线"],
                    "next_watch_items": ["成交量"],
                },
                "next_check_suggestion": {"urgency": "medium", "reason": "等放量"},
            },
        },
        run_id="track-api-1",
    )
    board_db = str(tmp_path / "board.db")
    board = BoardStore(board_db)
    board.initialize()
    board.initialize_research()
    job = board.create_research_job("000001", kind="track")
    board.finish_research(job["job_id"], run_id="track-api-1")
    client = TestClient(create_app(board_db))
    payload = client.get("/api/v1/research/stocks/000001/track").json()
    assert payload["mode"] == "track_day"
    assert payload["tracking"]["status"] == "review"
    assert payload["tracking"]["user_output"]["summary"] == "需要复核技术面"
    assert client.get("/api/v1/research/stocks/000001/current").status_code == 404


def test_b1_api_enqueues_and_reads_local_data(tmp_path):
    db_path = str(tmp_path / "board.db")
    store = BoardStore(db_path)
    store.initialize()
    store.replace_quotes([quote("000001")], source="fixture")
    client = TestClient(create_app(db_path))

    assert client.post("/api/v1/stocks/000001/refresh-request").json()["queue"]["status"] == "pending"
    assert client.put("/api/v1/watchlist/000001").status_code == 200
    assert client.get("/api/v1/watchlist").json()["items"][0]["stock_code"] == "000001"
    assert client.get("/api/v1/bars/000001").json()["items"] == []
    assert client.get("/api/v1/notices/000001").json()["items"] == []
    assert client.get("/api/v1/paper").json()["initial_cash"] == 1000000
    assert "模拟" in client.get("/").text


@pytest.mark.asyncio
async def test_profile_api_lists_and_retires_preference(tmp_path, monkeypatch):
    from insightagent.contracts import TaskStatus, utc_now
    from insightagent.persistence import SQLiteDatabase, SQLiteStateStore
    from insightagent.user_contracts import UserIntent, UserPreference, UserUtterance
    from insightagent.user_store import UserStore

    agent_db = str(tmp_path / "insightagent.db")
    monkeypatch.setenv("INSIGHTAGENT_DB_PATH", agent_db)
    database = SQLiteDatabase(agent_db)
    await database.initialize()
    states = SQLiteStateStore(database)
    state = await states.load_or_create(
        agent_name="fundamental",
        thesis_id="000001-initial",
        stock_code="000001",
    )
    state.private_memory = {"memory_summary": "盯经营现金流证伪"}
    state.status = TaskStatus.SUCCESS
    await states.save(state, expected_version=state.version)
    store = UserStore(database)
    now = utc_now().isoformat()
    await store.save_utterance(
        UserUtterance(
            utterance_id="u1",
            user_id="local",
            moment="pre_run",
            effect="remember",
            tags=json.dumps(["fundamental", "remember"]),
            intent_id="i1",
            stock_code="000001",
            thesis_id="000001-initial",
            run_id="r1",
            created_at=now,
        )
    )
    await store.save_intent(
        UserIntent(
            intent_id="i1",
            utterance_id="u1",
            effect="remember",
            tags=json.dumps(["fundamental", "remember"]),
            fundamental="盯经营现金流",
            technical="none",
            sentiment="none",
            macro="none",
            decision="none",
            tracking="none",
            not_evidence="none",
            created_at=now,
        )
    )
    await store.save_preference(
        UserPreference(
            preference_id="pref-board",
            user_id="local",
            status="active",
            current_version="1",
            kind="constraint",
            scope="fundamental",
            stock_code="000001",
            trigger="盯经营现金流",
            title="盯经营现金流",
            statement="估值必须对照经营现金流",
            source="user_feedback",
            source_utterance_id="u1",
            source_run_id="r1",
            created_at=now,
            updated_at=now,
        )
    )
    client = TestClient(create_app(str(tmp_path / "board.db")))
    payload = client.get("/api/v1/profile").json()
    assert payload["utterance_count"] == 1
    assert payload["preferences"][0]["preference_id"] == "pref-board"
    assert payload["expert_memories"][0]["memory_summary"] == "盯经营现金流证伪"
    assert "画像" in client.get("/").text
    retired = client.delete("/api/v1/profile/preferences/pref-board")
    assert retired.status_code == 200
    assert client.get("/api/v1/profile").json()["preferences"] == []
    assert client.delete("/api/v1/profile/preferences/pref-board").status_code == 404


def test_profile_generate_endpoint_returns_model_narrative(tmp_path, monkeypatch):
    async def fake_generate(**_kwargs):
        return {
            "profile_id": "profile-1",
            "persona_title": "现金流纪律型投资者",
            "overview": "偏好用现金流验证估值。",
        }

    monkeypatch.setattr(
        "insightboard.api.generate_user_profile", fake_generate
    )
    client = TestClient(create_app(str(tmp_path / "board.db")))
    response = client.post("/api/v1/profile/generate")
    assert response.status_code == 200
    assert response.json()["persona_title"] == "现金流纪律型投资者"
    assert "用户画像" in client.get("/").text


def test_paper_account_marks_to_delayed_quotes_and_stores_pick_memory(tmp_path):
    db_path = str(tmp_path / "board.db")
    store = BoardStore(db_path)
    store.initialize()
    store.initialize_paper()
    store.replace_quotes([quote("000001", price=10.0)], source="fixture")
    start = store.paper_snapshot()
    assert start["cash"] == 1_000_000
    assert start["equity"] == 1_000_000
    after_buy = store.paper_trade("000001", side="buy", quantity=100, reason="现金流质量")
    assert after_buy["cash"] == 999_000
    assert after_buy["positions"][0]["quantity"] == 100
    assert after_buy["picks"][0]["statement"] == "现金流质量"
    store.replace_quotes([quote("000001", price=12.0)], source="fixture")
    marked = store.paper_snapshot()
    assert marked["market_value"] == 1_200
    assert marked["equity"] == 1_000_200
    assert marked["pnl"] == 200
    sold = store.paper_trade("000001", side="sell", quantity=100)
    assert sold["positions"] == []
    assert sold["cash"] == 1_000_200
    assert sold["equity"] == 1_000_200


def test_paper_api_buy_and_pick(tmp_path):
    db_path = str(tmp_path / "board.db")
    store = BoardStore(db_path)
    store.initialize()
    store.replace_quotes([quote("000001")], source="fixture")
    client = TestClient(create_app(db_path))
    bought = client.post("/api/v1/paper/trades", json={"stock_code": "000001", "side": "buy", "quantity": 100, "reason": "低估值"}).json()
    assert bought["positions"][0]["quantity"] == 100
    assert bought["picks"][0]["statement"] == "低估值"
    saved = client.post("/api/v1/paper/picks", json={"stock_code": "none", "statement": "不买看不懂的生意"}).json()
    assert saved["stock_code"] == "none"
    picks = client.get("/api/v1/paper").json()["picks"]
    assert any(item["statement"] == "不买看不懂的生意" for item in picks)
    memory_id = next(item["memory_id"] for item in picks if item["statement"] == "低估值")
    assert client.delete(f"/api/v1/paper/picks/{memory_id}").status_code == 200
    assert client.post("/api/v1/paper/trades", json={"stock_code": "000001", "side": "buy", "quantity": 50}).status_code == 400

