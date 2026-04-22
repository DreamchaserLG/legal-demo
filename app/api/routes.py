import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.service.agent_service import get_dashboard_metrics, predict_legal_outcome
from app.service.analysis_service import analyze_sentence_search
from app.service.crawler_service import sync_all_sources
from app.service.ingestion_task_service import get_ingestion_task
from app.service.ofac_service import sync_ofac_demo
from app.service.canlii_service import sync_canlii_demo
from app.service.pdf_service import PDFRenderError, render_legal_memo_pdf
from app.service.search_service import search_and_optionally_sync

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _base_context(request: Request, page_id: str) -> dict:
    metrics = get_dashboard_metrics()
    return {
        "request": request,
        "app_name": settings.app_name,
        "page_id": page_id,
        "dashboard": metrics,
    }


def _to_bool(value) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _build_query_string(**kwargs) -> str:
    clean = {key: value for key, value in kwargs.items() if value not in {None, ""}}
    return urlencode(clean)


def _memo_url(text: str, limit: int, offset: int, source: str, sort: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh="1" if refresh else None,
    )
    return f"/memo?{query}"


def _analysis_url(text: str, limit: int, offset: int, source: str, sort: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh="1" if refresh else None,
    )
    return f"/analyze?{query}"


def _predict_url(text: str, limit: int, offset: int, source: str, sort: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh="1" if refresh else None,
    )
    return f"/predict?{query}"


def _memo_download_url(text: str, limit: int, offset: int, source: str, sort: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh="1" if refresh else None,
    )
    return f"/memo/download?{query}"


def _task_context(remote_fetch: dict | None, refresh_url: str) -> dict:
    task = (remote_fetch or {}).get("task") if isinstance(remote_fetch, dict) else None
    return {
        "ingestion_task": task,
        "task_refresh_url": refresh_url,
        "task_poll_ms": max(1000, int(getattr(settings, "ingestion_page_poll_seconds", 3000))),
    }


def _normalize_filename_text(value: str, max_length: int = 56) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    raw = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9\\s-]", " ", raw)
    raw = re.sub(r"\s+", "-", raw).strip(" .-_")
    if len(raw) > max_length:
        raw = raw[:max_length].rstrip(" .-_")
    return raw or "case"


def _memo_download_filenames(context: dict) -> tuple[str, str]:
    prediction_result = context.get("prediction_result") or {}
    analysis = prediction_result.get("analysis", {}) if isinstance(prediction_result, dict) else {}
    summary = (
        analysis.get("summary")
        or prediction_result.get("input_text")
        or context.get("text_input")
        or ""
    )
    summary_part = _normalize_filename_text(summary)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ascii_name = f"legal-memo-{timestamp}.pdf"
    pretty_name = f"legal-memo-{summary_part}-{timestamp}.pdf"
    return ascii_name, pretty_name


def _memo_context_payload(text: str, limit: int, offset: int, source: str, sort: str) -> dict:
    payload = {
        "text_input": text,
        "limit": limit,
        "offset": offset,
        "source": source,
        "sort": sort,
        "prediction_result": None,
        "analysis_url": _analysis_url(text, limit, offset, source, sort) if text.strip() else "/analyze",
        "predict_url": _predict_url(text, limit, offset, source, sort) if text.strip() else "/predict",
        "memo_download_url": _memo_download_url(text, limit, offset, source, sort) if text.strip() else "",
        "memo_date": datetime.now().strftime("%Y-%m-%d"),
    }
    if text.strip():
        payload["prediction_result"] = predict_legal_outcome(
            text=text,
            limit=limit,
            offset=offset,
            source=source,
            sort=sort,
        )
    return payload


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        _base_context(request, "home"),
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
    refresh: bool = Query(False),
):
    result = search_and_optionally_sync(
        keywords_input=keywords,
        sync_first=_to_bool(sync_first),
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh=refresh,
        origin_page="search",
    )

    context = _base_context(request, "search")
    context.update(result)
    context.update(
        _task_context(
            result.get("remote_fetch"),
            refresh_url=f"/results?{_build_query_string(keywords=keywords, limit=limit, offset=offset, source=source, sort=sort, refresh='1')}",
        )
    )
    return templates.TemplateResponse("results.html", context)


@router.get("/analyze", response_class=HTMLResponse)
def analyze_page(
    request: Request,
    text: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    refresh: bool = Query(False),
):
    context = _base_context(request, "analyze")
    context.update(
        {
            "text_input": text,
            "limit": limit,
            "offset": offset,
            "source": source,
            "sort": sort,
            "refresh": refresh,
            "analysis_result": None,
            "predict_url": _predict_url(text, limit, offset, source, sort) if text.strip() else "/predict",
        }
    )
    if text.strip():
        context["analysis_result"] = analyze_sentence_search(
            text=text,
            limit=limit,
            offset=offset,
            source=source,
            sort=sort,
            refresh=refresh,
            origin_page="analyze",
        )
        context.update(
            _task_context(
                context["analysis_result"].get("remote_fetch"),
                refresh_url=_analysis_url(text, limit, offset, source, sort, refresh=True),
            )
        )
    return templates.TemplateResponse("analyze.html", context)


@router.get("/predict", response_class=HTMLResponse)
def predict_page(
    request: Request,
    text: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    refresh: bool = Query(False),
):
    context = _base_context(request, "predict")
    context.update(
        {
            "text_input": text,
            "limit": limit,
            "offset": offset,
            "source": source,
            "sort": sort,
            "refresh": refresh,
            "prediction_result": None,
            "analysis_url": _analysis_url(text, limit, offset, source, sort) if text.strip() else "/analyze",
            "memo_download_url": _memo_download_url(text, limit, offset, source, sort) if text.strip() else "",
        }
    )
    if text.strip():
        context["prediction_result"] = predict_legal_outcome(
            text=text,
            limit=limit,
            offset=offset,
            source=source,
            sort=sort,
            refresh=refresh,
        )
        context.update(
            _task_context(
                context["prediction_result"].get("remote_fetch"),
                refresh_url=_predict_url(text, limit, offset, source, sort, refresh=True),
            )
        )
    return templates.TemplateResponse("predict.html", context)


@router.get("/memo", response_class=HTMLResponse)
def memo_page(
    request: Request,
    text: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
):
    return RedirectResponse(
        url=_analysis_url(text, limit, offset, source, sort),
        status_code=307,
    )


@router.get("/memo/download")
def memo_download(
    request: Request,
    text: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
):
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required for memo PDF export.")

    context = _base_context(request, "memo")
    context.update(_memo_context_payload(text, limit, offset, source, sort))
    try:
        pdf_bytes = render_legal_memo_pdf(context)
    except PDFRenderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    ascii_name, pretty_name = _memo_download_filenames(context)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(pretty_name)}"
            )
        },
    )


@router.post("/search")
async def search_redirect(request: Request):
    form = await request.form()
    query = urlencode(
        {
            "keywords": form.get("keywords", ""),
            "sync_first": "on" if form.get("sync_first") else "",
            "limit": form.get("limit", settings.default_search_limit),
            "offset": form.get("offset", 0),
            "source": form.get("source", "all"),
            "sort": form.get("sort", "relevance"),
        }
    )
    return RedirectResponse(url=f"/results?{query}", status_code=303)


@router.get("/api/search")
def api_search(
    keywords: str = Query(...),
    sync_first: bool = Query(False),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    refresh: bool = Query(False),
):
    return search_and_optionally_sync(
        keywords_input=keywords,
        sync_first=sync_first,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh=refresh,
        origin_page="search",
    )


@router.get("/api/analyze-search")
def api_analyze_search(
    text: str = Query(...),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    refresh: bool = Query(False),
):
    return analyze_sentence_search(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh=refresh,
        origin_page="analyze",
    )


@router.get("/api/predict")
def api_predict(
    text: str = Query(...),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    refresh: bool = Query(False),
):
    return predict_legal_outcome(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh=refresh,
    )


@router.get("/api/ingestion-tasks/{task_id}")
def api_ingestion_task(task_id: int):
    task = get_ingestion_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="ingestion task not found")
    return task


@router.post("/api/sync/ofac")
def api_sync_ofac():
    return sync_ofac_demo()


@router.post("/api/sync/canlii")
def api_sync_canlii():
    return sync_canlii_demo()


@router.post("/api/sync/all")
def api_sync_all():
    return sync_all_sources()
