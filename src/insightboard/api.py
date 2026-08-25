from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .store import BoardStore


def create_app(db_path: str = "data/board.db") -> FastAPI:
    store = BoardStore(db_path)
    store.initialize()
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
