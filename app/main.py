from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import settings
from app.service.ingestion_task_service import ensure_ingestion_tables

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=settings.app_name)

app.include_router(router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup():
    ensure_ingestion_tables()


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}
