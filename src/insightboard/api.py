from __future__ import annotations

from pathlib import Path
import json

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .research import agent_paths, load_projected_run, write_prompt
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .store import BoardStore


class ResearchRequest(BaseModel):
    stock_code: str
    prompt: str = "none"
    kind: str = "analyze"


class FeedbackRequest(BaseModel):
    prompt: str


def create_app(db_path: str = "data/board.db") -> FastAPI:
    store = BoardStore(db_path)
    store.initialize()
    store.initialize_research()
    app = FastAPI(title="InsightBoard", version="0.1.0")
    static_dir = Path(__file__).with_name("web")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/v1/meta/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/meta/ingest")
    def ingest() -> dict:
        return store.status()

    @app.get("/api/v1/quotes")
    def quotes(
        page: int = Query(1, ge=1), size: int = Query(100, ge=1, le=100),
        q: str = "", industry: str = "", sort: str = "turnover", order: str = "desc",
    ) -> dict:
        return store.quote_page(page=page, size=size, q=q.strip(), industry=industry.strip(), sort=sort, order=order)

    @app.get("/api/v1/quotes/{stock_code}")
    def quote(stock_code: str) -> dict:
        page = store.quote_page(page=1, size=1, q=stock_code, sort="stock_code", order="asc")
        for item in page["items"]:
            if item["stock_code"] == stock_code:
                return {"batch_id": page["batch_id"], "as_of": page["as_of"], "item": item}
        raise HTTPException(status_code=404, detail="Stock not found in current batch")

    @app.get("/api/v1/bars/{stock_code}")
    def bars(stock_code: str, limit: int = Query(120, ge=1, le=250)) -> dict:
        return {"stock_code": stock_code, "items": store.bars(stock_code, limit)}

    @app.get("/api/v1/notices/{stock_code}")
    def notices(stock_code: str, limit: int = Query(30, ge=1, le=30)) -> dict:
        return {"stock_code": stock_code, "items": store.notices(stock_code, limit)}

    @app.post("/api/v1/stocks/{stock_code}/refresh-request")
    def refresh_request(stock_code: str) -> dict:
        return {"stock_code": stock_code, "queue": store.enqueue_deep(stock_code, reason="detail", priority=5)}

    @app.post("/api/v1/research/jobs")
    def create_research(request: ResearchRequest) -> dict:
        try:
            if request.kind == "feedback":
                raise HTTPException(status_code=400, detail="use feedback endpoint")
            agent_paths()
            import os
            if not os.environ.get("DEEPSEEK_API_KEY"):
                raise HTTPException(status_code=503, detail="research is not configured")
            job = store.create_research_job(request.stock_code.strip(), prompt=request.prompt)
            if not job.get("existing"):
                write_prompt(job["job_id"], request.prompt)
        except HTTPException:
            raise
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        if job.get("existing"):
            raise HTTPException(status_code=409, detail={"job_id": job["job_id"]})
        return {key: job[key] for key in ("job_id", "stock_code", "kind", "status", "created_at")}

    @app.post("/api/v1/research/jobs/{job_id}/feedback")
    def create_feedback(job_id: str, request: FeedbackRequest) -> dict:
        job = store.research_job(job_id)
        if not job or job["status"] != "success" or not job.get("run_id"):
            raise HTTPException(status_code=409, detail="job has no successful run")
        try:
            created = store.create_research_job(job["stock_code"], kind="feedback", prompt=request.prompt, parent_run_id=job["run_id"])
            if not created.get("existing"):
                write_prompt(created["job_id"], request.prompt)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        if created.get("existing"):
            raise HTTPException(status_code=409, detail={"job_id": created["job_id"]})
        return {key: created[key] for key in ("job_id", "stock_code", "kind", "status", "created_at")}

    @app.get("/api/v1/research/jobs/{job_id}")
    def research_job(job_id: str) -> dict:
        job = store.research_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="research job not found")
        return job

    @app.get("/api/v1/research/stocks/{stock_code}/current")
    async def current_research(stock_code: str) -> dict:
        job = store.current_research(stock_code)
        if not job:
            raise HTTPException(status_code=404, detail="no successful research")
        result = await load_projected_run(job["run_id"])
        if not result:
            raise HTTPException(status_code=404, detail="research run not found")
        result["job_id"] = job["job_id"]
        result["parent_run_id"] = job.get("parent_run_id") or result.get("parent_run_id")
        result["rerun_dimensions"] = json.loads(job.get("rerun_dimensions") or "[]")
        return result

    @app.get("/api/v1/research/runs/{run_id}")
    async def research_run(run_id: str) -> dict:
        result = await load_projected_run(run_id)
        if not result:
            raise HTTPException(status_code=404, detail="research run not found")
        job = store.research_job_by_run(run_id)
        if job:
            result["job_id"] = job["job_id"]
            result["parent_run_id"] = job.get("parent_run_id") or result.get("parent_run_id")
            result["rerun_dimensions"] = json.loads(job.get("rerun_dimensions") or "[]")
        return result

    @app.get("/api/v1/research/stocks/{stock_code}/history")
    def research_history(stock_code: str) -> dict:
        return {"items": store.research_history(stock_code)}

    @app.get("/api/v1/watchlist")
    def watchlist() -> dict:
        return {"items": store.watchlist()}

    @app.put("/api/v1/watchlist/{stock_code}")
    def add_watch(stock_code: str) -> dict:
        store.add_watch(stock_code)
        return {"stock_code": stock_code, "watchlisted": True}

    @app.delete("/api/v1/watchlist/{stock_code}")
    def remove_watch(stock_code: str) -> dict:
        store.remove_watch(stock_code)
        return {"stock_code": stock_code, "watchlisted": False}

    return app


app = create_app()
