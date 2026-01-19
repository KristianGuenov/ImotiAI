from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .auth import require_api_key
from .db import Base, engine, get_session
from .schemas import (
    DetailQueueOut,
    ExtractionBatchIn,
    ExtractionBatchOut,
    ExtractionIn,
    ExtractionListOut,
    ExtractionOut,
)
from .services import ExtractionService
from .settings import Settings, get_settings


def create_app() -> FastAPI:
    app = FastAPI(title="Legal List Extractor API", version="1.3.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup() -> None:
        Base.metadata.create_all(bind=engine)

    @app.get("/health")
    def health(settings: Settings = Depends(get_settings)) -> dict:
        return {"ok": True, "service": "legal-list-extractor", "db": settings.database_url}

    @app.post("/api/v1/extractions", response_model=ExtractionOut, dependencies=[Depends(require_api_key)])
    def create_extraction(payload: ExtractionIn, session: Session = Depends(get_session)) -> ExtractionOut:
        return ExtractionService(session).create(payload)

    # Speed path for runners: fewer HTTP calls, same server-side behavior.
    @app.post("/api/v1/extractions/batch", response_model=ExtractionBatchOut, dependencies=[Depends(require_api_key)])
    def create_extraction_batch(payload: ExtractionBatchIn, session: Session = Depends(get_session)) -> ExtractionBatchOut:
        return ExtractionService(session).create_batch(payload.items)

    @app.get("/api/v1/extractions", response_model=ExtractionListOut)
    def list_extractions(limit: int = 50, session: Session = Depends(get_session)) -> ExtractionListOut:
        limit = max(1, min(limit, 200))
        return ExtractionService(session).list(limit=limit)

    @app.get("/api/v1/extractions/{extraction_id}", response_model=ExtractionOut)
    def get_extraction(extraction_id: int, session: Session = Depends(get_session)) -> ExtractionOut:
        item = ExtractionService(session).get(extraction_id)
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        return item

    @app.get("/api/v1/extractions/{extraction_id}/items.csv")
    def download_items_csv(extraction_id: int, session: Session = Depends(get_session)) -> Response:
        csv_bytes = ExtractionService(session).export_items_csv(extraction_id)
        if csv_bytes is None:
            raise HTTPException(status_code=404, detail="Not found")
        return Response(
            content=csv_bytes,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="extraction_{extraction_id}_items.csv"'},
        )

    # Queue of listing URLs already in DB that are missing a detail run.
    @app.get("/api/v1/detail-queue", response_model=DetailQueueOut, dependencies=[Depends(require_api_key)])
    def detail_queue(
        domain: str | None = None,
        url_contains: str | None = None,
        limit: int = 200,
        session: Session = Depends(get_session),
    ) -> DetailQueueOut:
        urls = ExtractionService(session).detail_queue(domain=domain, url_contains=url_contains, limit=limit)
        return DetailQueueOut(urls=urls)

    return app


app = create_app()
