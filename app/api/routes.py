'''

@-*- coding: utf-8 -*-

@ python：python 3.9

@ 创建人员：lg

@ 创建时间：2026/3/30

'''
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.service.crawler_service import sync_all_sources
from app.service.ofac_service import sync_ofac_demo
from app.service.canlii_service import sync_canlii_demo
from app.service.search_service import search_and_optionally_sync

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _to_bool(value) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name
        }
    )


@router.get("/results", response_class=HTMLResponse)
def results_page(
    request: Request,
    keywords: str = Query(""),
    sync_first: str | None = Query(None),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
):
    result = search_and_optionally_sync(
        keywords_input=keywords,
        sync_first=_to_bool(sync_first),
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
    )

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "app_name": settings.app_name,
            **result
        }
    )


@router.post("/search")
async def search_redirect(request: Request):
    form = await request.form()
    keywords = form.get("keywords", "")
    sync_first = form.get("sync_first")
    limit = form.get("limit", settings.default_search_limit)
    offset = form.get("offset", 0)
    source = form.get("source", "all")
    sort = form.get("sort", "relevance")

    query = urlencode({
        "keywords": keywords,
        "sync_first": "on" if sync_first else "",
        "limit": limit,
        "offset": offset,
        "source": source,
        "sort": sort,
    })
    return RedirectResponse(url=f"/results?{query}", status_code=303)


@router.get("/api/search")
def api_search(
    keywords: str = Query(...),
    sync_first: bool = Query(False),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
):
    return search_and_optionally_sync(
        keywords_input=keywords,
        sync_first=sync_first,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
    )


@router.post("/api/sync/ofac")
def api_sync_ofac():
    return sync_ofac_demo()


@router.post("/api/sync/canlii")
def api_sync_canlii():
    return sync_canlii_demo()


@router.post("/api/sync/all")
def api_sync_all():
    return sync_all_sources()
