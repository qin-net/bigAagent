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

    return app


app = create_app()
