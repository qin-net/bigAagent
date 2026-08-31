from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.simulated_investor import PERSONA, make_client, run_simulated_investor


@pytest.mark.asyncio
async def test_simulated_cashflow_user_full_chain_fills_expert_desk(tmp_path, monkeypatch):
    agent_db = str(tmp_path / "insightagent.db")
    artifacts = str(tmp_path / "artifacts")
    board_db = str(tmp_path / "board.db")
    summary = await run_simulated_investor(
        agent_db=agent_db,
        artifacts=artifacts,
        board_db=board_db,
    )

    assert len(summary["stocks"]) == 3
    assert all(summary["second_track_carried"].values())
    for code, prior in summary["second_analyze_prior"].items():
        assert "现金流" in prior["fundamental"]["memory_summary"] or "对照" in prior["fundamental"]["memory_summary"]
        assert prior["technical"]["memory_summary"]
        assert prior["sentiment"]["memory_summary"]
        assert prior["macro"]["memory_summary"]
        assert "prior_memory" not in str(code)
    assert summary["preference_count"] >= 4
    fund_rows = [row for row in summary["expert_rows"] if row["agent_name"] == "fundamental"]
    assert len(fund_rows) == 3
    assert all(row["parent"] for row in fund_rows)
    assert all(row["lessons"] for row in fund_rows)
    track_rows = [row for row in summary["expert_rows"] if row["agent_name"] == "tracking"]
    assert len(track_rows) == 3
    assert all(row["parent"] for row in track_rows)

    app = make_client(board_db, agent_db=agent_db, artifacts=artifacts, monkeypatch=monkeypatch)
    client = TestClient(app)
    assert client.get("/api/v1/meta/health").json()["status"] == "ok"

    experts = client.get("/api/v1/experts").json()
    by_role = {item["agent_name"]: item for item in experts["experts"]}
    assert by_role["fundamental"]["stock_count"] == 3
    assert by_role["fundamental"]["iterated"] is True
    assert by_role["fundamental"]["portrait"]
    assert by_role["tracking"]["memories"]
    assert any(item.get("lessons") for item in by_role["fundamental"]["memories"])
    statements = [item["statement"] for item in experts["preferences"]]
    assert any("现金流" in item or "对照" in item for item in statements)
    assert any("不重仓" in item for item in statements)

    profile = client.get("/api/v1/profile").json()
    assert profile["preferences"]
    assert profile["expert_memories"]

    paper = client.get("/api/v1/paper").json()
    assert len(paper["positions"]) == 3
    watch = client.get("/api/v1/watchlist").json()["items"]
    assert {item["stock_code"] for item in watch} >= {item["code"] for item in PERSONA["stocks"]}

    for code in ("000858", "000333", "601318"):
        current = client.get("/api/v1/research/stocks/{}/current".format(code))
        assert current.status_code == 200, current.text
        payload = current.json()
        assert payload["status"] in {"success", "degraded"}
        assert payload["decision"]
        assert payload["dimensions"]["fundamental"]
        memories = payload.get("memories") or {}
        assert memories["fundamental"]["memory_summary"]
        assert memories["fundamental"]["carried"] is True
        track = client.get("/api/v1/research/stocks/{}/track".format(code))
        assert track.status_code == 200, track.text
        tracked = track.json()
        assert tracked["mode"] == "track_day"
        assert tracked["tracking"]
        assert (tracked.get("memories") or {}).get("tracking", {}).get("carried") is True
        history = client.get("/api/v1/research/stocks/{}/history".format(code)).json()
        assert len(history["items"]) >= 2
