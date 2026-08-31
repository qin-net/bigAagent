from __future__ import annotations

from pathlib import Path
import json
import threading

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .collector import AkshareQuoteCollector, collect_once
from .research import (
    agent_paths,
    generate_user_profile,
    load_projected_run,
    load_user_profile,
    retire_user_preference,
    write_prompt,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .store import BoardStore


class ResearchRequest(BaseModel):
    stock_code: str
    prompt: str = "none"
    kind: str = "analyze"


class FeedbackRequest(BaseModel):
    prompt: str


class PaperTradeRequest(BaseModel):
    stock_code: str
    side: str
    quantity: int
    reason: str = ""


class PickMemoryRequest(BaseModel):
    stock_code: str = "none"
    statement: str


def create_app(db_path: str = "data/board.db", collector=None) -> FastAPI:
    store = BoardStore(db_path)
    store.initialize()
    store.initialize_research()
    store.initialize_paper()
    quote_collector = collector or AkshareQuoteCollector()
    collect_lock = threading.Lock()
    collect_state = {"running": False}
    bars_lock = threading.Lock()
    bars_running: set[str] = set()
    app = FastAPI(title="InsightBoard", version="0.1.0")
    static_dir = Path(__file__).with_name("web")

    def _web_file(name: str, media_type: str) -> FileResponse:
        return FileResponse(static_dir / name, media_type=media_type)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/style.css")
    def style_css() -> FileResponse:
        return _web_file("style.css", "text/css")

    @app.get("/app.js")
    def app_js() -> FileResponse:
        return _web_file("app.js", "text/javascript")

    @app.get("/static/style.css")
    def static_style_css() -> FileResponse:
        return _web_file("style.css", "text/css")

    @app.get("/static/app.js")
    def static_app_js() -> FileResponse:
        return _web_file("app.js", "text/javascript")

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/api/v1/meta/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/meta/ingest")
    def ingest() -> dict:
        return {**store.status(), "collecting": collect_state["running"]}

    @app.post("/api/v1/quotes/collect")
    def collect_quotes() -> dict:
        if collect_state["running"] or not collect_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="collection already running")
        collect_state["running"] = True

        def run() -> None:
            import asyncio
            try:
                asyncio.run(collect_once(store, quote_collector))
            except Exception:
                pass
            finally:
                collect_state["running"] = False
                collect_lock.release()

        threading.Thread(target=run, daemon=True).start()
        return {**store.status(), "collecting": True}

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

    @app.post("/api/v1/stocks/{stock_code}/bars/collect")
    def collect_bars(stock_code: str) -> dict:
        code = stock_code.strip()
        if len(code) != 6 or not code.isdigit():
            raise HTTPException(status_code=400, detail="invalid stock code")
        with bars_lock:
            if code in bars_running:
                raise HTTPException(status_code=409, detail="bar collection already running")
            bars_running.add(code)

        def run() -> None:
            try:
                if not hasattr(quote_collector, "collect_deep"):
                    raise RuntimeError("collector cannot fetch daily bars")
                bars, notices = quote_collector.collect_deep(code)
                store.save_deep(bars, notices, source=getattr(quote_collector, "source", "akshare"))
                store.finish_deep(code)
            except Exception as error:
                store.enqueue_deep(code, reason="detail", priority=5)
                store.finish_deep(code, error=str(error))
            finally:
                with bars_lock:
                    bars_running.discard(code)

        threading.Thread(target=run, daemon=True).start()
        return {"stock_code": code, "collecting": True}

    @app.post("/api/v1/research/jobs")
    def create_research(request: ResearchRequest) -> dict:
        try:
            if request.kind in {"feedback", "track_feedback"}:
                raise HTTPException(status_code=400, detail="use feedback endpoint")
            if request.kind not in {"analyze", "track"}:
                raise HTTPException(status_code=400, detail="invalid research job kind")
            agent_paths()
            import os
            if not os.environ.get("DEEPSEEK_API_KEY"):
                raise HTTPException(status_code=503, detail="research is not configured")
            job = store.create_research_job(
                request.stock_code.strip(), kind=request.kind, prompt=request.prompt,
            )
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
            kind = (
                "track_feedback"
                if job["kind"] in {"track", "track_feedback"}
                else "feedback"
            )
            created = store.create_research_job(
                job["stock_code"], kind=kind, prompt=request.prompt, parent_run_id=job["run_id"],
            )
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

    @app.get("/api/v1/research/stocks/{stock_code}/track")
    async def current_track(stock_code: str) -> dict:
        job = store.current_track(stock_code)
        if not job:
            raise HTTPException(status_code=404, detail="no successful track")
        result = await load_projected_run(job["run_id"])
        if not result:
            raise HTTPException(status_code=404, detail="track run not found")
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

    @app.get("/api/v1/profile")
    async def user_profile(stock_code: str = "") -> dict:
        return await load_user_profile(stock_code=stock_code.strip() or None)

    @app.post("/api/v1/profile/generate")
    async def create_user_profile() -> dict:
        try:
            return await generate_user_profile(paper=store.paper_snapshot())
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error))

    @app.delete("/api/v1/profile/preferences/{preference_id}")
    async def retire_preference(preference_id: str) -> dict:
        retired = await retire_user_preference(preference_id)
        if not retired:
            raise HTTPException(status_code=404, detail="preference not found")
        return {"preference_id": preference_id, "status": "retired"}

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

    @app.get("/api/v1/paper")
    def paper() -> dict:
        return store.paper_snapshot()

    @app.post("/api/v1/paper/trades")
    def paper_trade(request: PaperTradeRequest) -> dict:
        try:
            return store.paper_trade(
                request.stock_code.strip(),
                side=request.side.strip(),
                quantity=request.quantity,
                reason=request.reason,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))

    @app.post("/api/v1/paper/picks")
    def save_pick(request: PickMemoryRequest) -> dict:
        try:
            return store.save_pick_memory(stock_code=request.stock_code, statement=request.statement)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))

    @app.delete("/api/v1/paper/picks/{memory_id}")
    def retire_pick(memory_id: str) -> dict:
        if not store.retire_pick_memory(memory_id):
            raise HTTPException(status_code=404, detail="pick memory not found")
        return {"memory_id": memory_id, "status": "retired"}

    return app


app = create_app()
