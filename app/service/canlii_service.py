import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin

import certifi
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings
from app.service.common_service import log_sync, parse_dt, replace_item_keywords, sha256_text, upsert_source_item

CANLII_DATABASES_INDEX_URL = "https://www.canlii.org/en/databases.html"
_DATABASE_PAGE_CACHE: dict[str, object] = {"expires_at": 0.0, "pages": []}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Connection": "close",
    "Cache-Control": "no-cache",
}


class TLSHttpAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(*args, **kwargs)


def _build_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )

    adapter = TLSHttpAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(HTTP_HEADERS)
    session.verify = certifi.where()
    session.trust_env = False
    return session


_HTTP_SESSION = _build_session()


def _http_get(url: str) -> requests.Response:
    try:
        resp = _HTTP_SESSION.get(url, timeout=settings.request_timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(f"SSL/TLS handshake failed for {url}: {exc}") from exc


def _normalize_canlii_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url

    prefix = "https://www.canlii.org/"
    if url.startswith(prefix):
        path = url[len(prefix):].lstrip("/")
        if not path.startswith(("en/", "fr/")):
            url = prefix + "en/" + path

    return url.rstrip("/") + "/"


def _get_soup(url: str) -> BeautifulSoup:
    return BeautifulSoup(_http_get(url).text, "html.parser")


def _is_case_database_url(url: str) -> bool:
    normalized = _normalize_canlii_url(url)
    prefix = "https://www.canlii.org/"
    if not normalized.startswith(prefix):
        return False
    path = normalized[len(prefix):].strip("/")
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) == 3 and segments[0] == "en"


def _discover_case_database_pages() -> List[str]:
    ttl_seconds = max(300, int(getattr(settings, "canlii_database_discovery_ttl_seconds", 21600)))
    now = time.time()
    cached_pages = _DATABASE_PAGE_CACHE.get("pages") or []
    if cached_pages and float(_DATABASE_PAGE_CACHE.get("expires_at") or 0.0) > now:
        return list(cached_pages)

    pages = []
    seen = set()
    try:
        soup = _get_soup(CANLII_DATABASES_INDEX_URL)
        for anchor in soup.find_all("a", href=True):
            full_url = _normalize_canlii_url(urljoin(CANLII_DATABASES_INDEX_URL, anchor["href"]))
            if not _is_case_database_url(full_url):
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            pages.append(full_url)
    except Exception:
        pages = []

    _DATABASE_PAGE_CACHE["pages"] = pages
    _DATABASE_PAGE_CACHE["expires_at"] = now + ttl_seconds
    return list(pages)


def _keyword_search_database_pages() -> List[str]:
    configured_pages = [_normalize_canlii_url(page) for page in settings.canlii_database_pages if str(page).strip()]
    discovered_pages = _discover_case_database_pages()

    combined = []
    seen = set()
    for page in configured_pages + discovered_pages:
        if page in seen:
            continue
        seen.add(page)
        combined.append(page)

    page_limit = max(1, int(getattr(settings, "canlii_remote_database_page_limit", 80)))
    return combined[:page_limit]


def _is_rss_url_working(url: str) -> bool:
    try:
        resp = _http_get(url)
        content_type = resp.headers.get("Content-Type", "").lower()
        if "xml" not in content_type and not resp.text.lstrip().startswith("<?xml"):
            return False

        ET.fromstring(resp.content)
        return True
    except Exception:
        return False


def _discover_rss_url(database_page_url: str) -> Optional[str]:
    database_page_url = _normalize_canlii_url(database_page_url)
    if database_page_url.endswith(".xml"):
        return database_page_url

    for candidate in [
        urljoin(database_page_url, "rss_new.xml"),
        urljoin(database_page_url, "rss_modified.xml"),
    ]:
        if _is_rss_url_working(candidate):
            return candidate

    try:
        soup = _get_soup(database_page_url)

        link_tag = soup.find("link", attrs={"type": "application/rss+xml"})
        if link_tag and link_tag.get("href"):
            return urljoin(database_page_url, link_tag["href"])

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "rss" in href.lower():
                return urljoin(database_page_url, href)
    except Exception:
        return None

    return None


def _parse_rss_items(rss_url: str) -> List[Dict]:
    root = ET.fromstring(_http_get(rss_url).content)

    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title", default="").strip()
        link = item.findtext("link", default="").strip()
        pub_date = item.findtext("pubDate", default="").strip()
        description = item.findtext("description", default="").strip()

        if not title and not link:
            continue

        items.append(
            {
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "description": description,
            }
        )

    return items


def _keywords_from_texts(*values) -> List[str]:
    words = []
    for val in values:
        if not val:
            continue
        text = str(val).replace("/", " ").replace("-", " ")
        for piece in text.split():
            piece = piece.strip(" ,.;:()[]{}\"'")
            if len(piece) >= 4:
                words.append(piece)

    result = []
    seen = set()
    for word in words:
        low = word.lower()
        if low in seen:
            continue
        seen.add(low)
        result.append(word)
        if len(result) >= 20:
            break
    return result


def _keyword_match(values: List[str], keywords: List[str]) -> bool:
    haystack = " ".join([str(value or "") for value in values]).lower()
    return any(str(keyword or "").strip().lower() in haystack for keyword in keywords if str(keyword or "").strip())


def _extract_case_text(url: str) -> str:
    if not url:
        return ""
    try:
        soup = _get_soup(url)
    except Exception:
        return ""

    container = soup.find("main") or soup.find("article") or soup.body
    if not container:
        return ""

    for tag in container.find_all(["script", "style", "noscript"]):
        tag.decompose()

    text_content = " ".join(container.stripped_strings)
    return text_content[:6000]


def _store_canlii_item(item: Dict, rss_url: str, page_url: str, fetch_mode: str = "sync", enrich_case_text: bool = False) -> int:
    title = item["title"] or "Untitled CanLII Item"
    link = item["link"]
    pub_date = parse_dt(item["pub_date"])
    description = item["description"]
    case_text = _extract_case_text(link) if enrich_case_text else ""

    raw_text = case_text or description or title
    source_uid = sha256_text((link or title) + "|" + (item["pub_date"] or ""))

    item_id = upsert_source_item(
        source_code="canlii",
        source_uid=source_uid,
        title=title,
        item_url=link,
        published_at=pub_date,
        summary=(description or raw_text)[:1000],
        raw_text=raw_text[:6000],
        raw_json={
            "rss_url": rss_url,
            "database_page": page_url,
            "pub_date": item["pub_date"],
            "fetch_mode": fetch_mode,
            "fetched_at": datetime.utcnow().isoformat(),
        },
    )

    keywords = _keywords_from_texts(title, description, raw_text)
    replace_item_keywords(item_id, keywords)
    return 1


def sync_canlii_demo():
    database_pages = _keyword_search_database_pages()
    if not database_pages:
        message = "No CanLII database pages are available for traversal."
        log_sync("canlii", "skipped", message)
        return {
            "source": "canlii",
            "status": "skipped",
            "message": message,
            "items": 0,
            "error_type": "missing_config",
        }

    processed = 0
    page_errors = []

    for raw_page_url in database_pages:
        page_url = _normalize_canlii_url(raw_page_url)

        try:
            rss_url = _discover_rss_url(page_url)
            if not rss_url:
                page_errors.append(f"{page_url}: RSS not found")
                continue

            for item in _parse_rss_items(rss_url):
                processed += _store_canlii_item(
                    item=item,
                    rss_url=rss_url,
                    page_url=page_url,
                    fetch_mode="sync",
                    enrich_case_text=False,
                )

        except Exception as exc:
            page_errors.append(f"{page_url}: {exc}")

    status = "success" if processed > 0 else "failed"
    message = "；".join(page_errors) if page_errors else ""

    if page_errors and processed > 0:
        error_type = "partial_failure"
    elif page_errors:
        error_type = "sync_failed"
    else:
        error_type = ""

    log_sync("canlii", status, f"CanLII sync done, processed={processed}")
    return {
        "source": "canlii",
        "status": status,
        "processed": processed,
        "items": processed,
        "message": message,
        "error_type": error_type,
    }


def sync_canlii_by_keywords(keywords: List[str], target_count: int | None = None):
    clean_keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
    if not clean_keywords:
        return {
            "source": "canlii",
            "status": "skipped",
            "processed": 0,
            "items": 0,
            "message": "No keywords were provided for CanLII hydration.",
            "error_type": "missing_keywords",
        }

    database_pages = _keyword_search_database_pages()
    if not database_pages:
        return {
            "source": "canlii",
            "status": "skipped",
            "processed": 0,
            "items": 0,
            "message": "No CanLII database pages are available for traversal.",
            "error_type": "missing_config",
        }

    processed = 0
    page_errors = []
    configured_limit = max(1, int(getattr(settings, "remote_search_max_items_per_source", 12)))
    requested_limit = max(1, int(target_count or 0)) if target_count else 0
    match_limit = max(configured_limit, requested_limit * 2 if requested_limit else 0)

    for raw_page_url in database_pages:
        if processed >= match_limit:
            break

        page_url = _normalize_canlii_url(raw_page_url)
        try:
            rss_url = _discover_rss_url(page_url)
            if not rss_url:
                page_errors.append(f"{page_url}: RSS not found")
                continue

            for item in _parse_rss_items(rss_url):
                if processed >= match_limit:
                    break
                if not _keyword_match([item.get("title"), item.get("description")], clean_keywords):
                    continue
                processed += _store_canlii_item(
                    item=item,
                    rss_url=rss_url,
                    page_url=page_url,
                    fetch_mode="search_hydration",
                    enrich_case_text=True,
                )
        except Exception as exc:
            page_errors.append(f"{page_url}: {exc}")

    if processed > 0 and page_errors:
        status = "partial_success"
        error_type = "partial_failure"
    elif processed > 0:
        status = "success"
        error_type = ""
    else:
        status = "failed" if page_errors else "skipped"
        error_type = "remote_search_failed" if page_errors else "no_match"

    message = "；".join(page_errors) if page_errors else f"CanLII remote hydration stored {processed} items."
    log_sync("canlii", status, f"CanLII remote hydration done, processed={processed}, keywords={clean_keywords}")
    return {
        "source": "canlii",
        "status": status,
        "processed": processed,
        "items": processed,
        "message": message,
        "error_type": error_type,
    }
