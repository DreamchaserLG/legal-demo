from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import router
from app.core.config import settings
from app.service.archive_service import bootstrap_database_from_archive, ensure_archive_directories
from app.service.ingestion_task_service import ensure_ingestion_tables
from app.service.legal_data_service import ensure_legal_data_tables, schedule_canada_legal_data_sync, sync_canada_legal_data
from app.service.module_service import ensure_module_support_tables
from app.service.user_service import ensure_user_tables

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=settings.app_name)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
)

app.include_router(router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def add_no_store_headers(request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
def on_startup():
    ensure_ingestion_tables()
    ensure_user_tables()
    ensure_module_support_tables()
    ensure_legal_data_tables()
    ensure_archive_directories()
    if settings.archive_bootstrap_enabled:
        bootstrap_database_from_archive()
    startup_sync_mode = str(getattr(settings, "startup_canada_sync_mode", "background") or "background").strip().lower()
    if startup_sync_mode == "blocking":
        sync_canada_legal_data(force=False)
    elif startup_sync_mode == "background":
        schedule_canada_legal_data_sync(force=False)


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}
