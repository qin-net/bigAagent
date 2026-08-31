from __future__ import annotations

import pytest
from insightboard.store import BoardStore
from fastapi.testclient import TestClient
from insightboard.api import create_app


def test_research_job_queue_and_history(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))
    store.initialize()
    store.initialize_research()
    job = store.create_research_job("000858", prompt="记住关注现金流")
    assert job["status"] == "queued"
    duplicate = store.create_research_job("000858", prompt="再次研究")
    assert duplicate["existing"] is True
    claimed = store.claim_research()
    assert claimed["job_id"] == job["job_id"]
    assert store.research_job(job["job_id"])["status"] == "running"
    store.finish_research(job["job_id"], run_id="run-1", rerun_dimensions=["fundamental"])
    assert store.research_job(job["job_id"])["rerun_dimensions"] == '["fundamental"]'
    store.finish_research(job["job_id"], run_id="run-1")
    assert store.current_research("000858")["run_id"] == "run-1"
    assert store.research_history("000858", 20)[0]["job_id"] == job["job_id"]


def test_api_run_lookup_includes_job_lineage(tmp_path, monkeypatch):
    store = BoardStore(str(tmp_path / "board.db")); store.initialize(); store.initialize_research()
    job = store.create_research_job("000858"); store.claim_research(); store.finish_research(job["job_id"], run_id="run-1", rerun_dimensions=["fundamental"])
    async def fake_load(_id):
        return {"run_id": _id, "parent_run_id": None, "rerun_dimensions": []}
    monkeypatch.setattr("insightboard.api.load_projected_run", fake_load)
    response = TestClient(create_app(str(tmp_path / "board.db"))).get("/api/v1/research/runs/run-1")
    assert response.status_code == 200
    assert response.json()["rerun_dimensions"] == ["fundamental"]


def test_research_job_recovers_stale_running(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))
    store.initialize(); store.initialize_research()
    job = store.create_research_job("000858")
    store.claim_research()
    with store._connection() as connection:
        connection.execute("UPDATE research_job SET updated_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["job_id"],))
    assert store.recover_research() == 1
    assert store.research_job(job["job_id"])["status"] == "queued"


def test_research_job_rejects_invalid_code(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))
    store.initialize()
    store.initialize_research()
    try:
        store.create_research_job("858")
    except ValueError as error:
        assert "invalid" in str(error)
    else:
        raise AssertionError("invalid code should fail")


def test_track_job_does_not_replace_current_research(tmp_path):
    store = BoardStore(str(tmp_path / "board.db"))
    store.initialize()
    store.initialize_research()
    research = store.create_research_job("000858")
    store.claim_research()
    store.finish_research(research["job_id"], run_id="run-research")
    track = store.create_research_job("000858", kind="track")
    store.claim_research()
    store.finish_research(track["job_id"], run_id="run-track")
    assert store.current_research("000858")["run_id"] == "run-research"
    assert store.current_track("000858")["run_id"] == "run-track"
    assert {item["kind"] for item in store.research_history("000858")} == {"analyze", "track"}


def test_api_creates_track_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    client = TestClient(create_app(str(tmp_path / "board.db")))
    payload = client.post(
        "/api/v1/research/jobs",
        json={"stock_code": "000858", "kind": "track", "prompt": "none"},
    ).json()
    assert payload["kind"] == "track"
    assert payload["status"] == "queued"
    html = client.get("/").text
    assert "track-start" in html


@pytest.mark.asyncio
async def test_execute_track_job_calls_track_thesis(tmp_path, monkeypatch):
    from insightboard.research import execute_job

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("INSIGHTAGENT_DB_PATH", str(tmp_path / "insightagent.db"))
    called = {}

    class Result:
        run_id = "track-run-1"

    async def fake_track(thesis_id, **kwargs):
        called["thesis_id"] = thesis_id
        called["fixture"] = kwargs.get("fixture")
        called["prompt"] = kwargs.get("user_prompt")
        return Result()

    monkeypatch.setattr("insightboard.research.track_thesis", fake_track)
    out = await execute_job(
        {"kind": "track", "stock_code": "000858", "prompt": "none", "user_id": "local"}
    )
    assert called["thesis_id"] == "000858-initial"
    assert called["fixture"] is False
    assert out["run_id"] == "track-run-1"
