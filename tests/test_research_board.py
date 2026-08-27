from __future__ import annotations

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
