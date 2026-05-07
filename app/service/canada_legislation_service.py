import csv
import io
import re
import ssl
from datetime import datetime
from urllib.parse import quote, urljoin

import certifi
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings
from app.service.common_service import (
    log_sync,
    replace_item_keywords,
    sha256_text,
    upsert_source_item,
)

JUSTICE_LAWS_BASE = "https://laws-lois.justice.gc.ca"
JUSTICE_ACT_INDEX_TEMPLATE = JUSTICE_LAWS_BASE + "/eng/acts/index.html/{letter}.html"
JUSTICE_REG_INDEX_TEMPLATE = JUSTICE_LAWS_BASE + "/eng/regulations/index.html/{letter}.html"

ONTARIO_LAWS_BASE = "https://www.ontario.ca/laws"
ONTARIO_API_BASE = "https://www.ontario.ca/laws/api/v2"
ONTARIO_AUTOCOMPLETE_URL = ONTARIO_API_BASE + "/laws/autocomplete?term={term}"
ONTARIO_STATUTES_CSV_URL = "https://www.ontario.ca/laws/csv/2_public_statutes_e.csv"
ONTARIO_REGULATIONS_CSV_URL = "https://www.ontario.ca/laws/csv/3_regulations_e.csv"

FEDERAL_SOURCE_CODES = {"ca_federal_act", "ca_federal_regulation"}
ONTARIO_SOURCE_CODES = {"on_statute", "on_regulation"}
CANADA_LEGISLATION_SOURCE_CODES = FEDERAL_SOURCE_CODES | ONTARIO_SOURCE_CODES

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xml,application/json,text/csv,*/*;q=0.8",
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
    resp = _HTTP_SESSION.get(url, timeout=settings.request_timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp


def _clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or sha256_text(str(value or ""))[:16]


def _keyword_list(*values, limit: int = 20) -> list[str]:
    result = []
    seen = set()
    for value in values:
        raw = _clean_text(value)
        if not raw:
            continue
        parts = re.split(r"[\s,;/()\-]+", raw)
        for part in parts:
            token = part.strip(" .'\"")
            if len(token) < 3:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(token)
            if len(result) >= limit:
                return result
    return result


def _xml_text(xml_content: str, limit: int = 12000) -> str:
    soup = BeautifulSoup(xml_content or "", "xml")
    text_value = " ".join(soup.stripped_strings)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return text_value[:limit]


def _html_text(html_content: str, limit: int = 12000) -> str:
    soup = BeautifulSoup(html_content or "", "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    text_value = " ".join(soup.stripped_strings)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return text_value[:limit]


def _federal_index_urls(kind: str) -> list[str]:
    template = JUSTICE_ACT_INDEX_TEMPLATE if kind == "act" else JUSTICE_REG_INDEX_TEMPLATE
    letters = [chr(code) for code in range(ord("A"), ord("Z") + 1)]
    return [template.format(letter=letter) for letter in letters]


def _parse_federal_index(kind: str) -> list[dict]:
    results = []
    seen = set()
    prefix = "/eng/acts/" if kind == "act" else "/eng/regulations/"
    for index_url in _federal_index_urls(kind):
        try:
            soup = BeautifulSoup(_http_get(index_url).text, "html.parser")
        except Exception:
            continue
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href.startswith(prefix) or not href.endswith("/index.html"):
                continue
            title = _clean_text(anchor.get_text(" ", strip=True))
            if not title:
                continue
            full_url = urljoin(index_url, href)
            if full_url in seen:
                continue
            seen.add(full_url)
            code = full_url.split("/")[-2]
            results.append(
                {
                    "kind": kind,
                    "code": code,
                    "title": title,
                    "index_url": index_url,
                    "page_url": full_url,
                }
            )
    return results


def _fetch_federal_document(entry: dict) -> dict | None:
    try:
        soup = BeautifulSoup(_http_get(entry["page_url"]).text, "html.parser")
    except Exception:
        return None

    xml_url = ""
    pdf_url = ""
    for anchor in soup.find_all("a", href=True):
        text_value = _clean_text(anchor.get_text(" ", strip=True)).lower()
        href = urljoin(entry["page_url"], anchor["href"])
        if "xml full document" in text_value:
            xml_url = href
        elif "pdf full document" in text_value:
            pdf_url = href

    if not xml_url:
        return None

    xml_content = _http_get(xml_url).text
    raw_text = _xml_text(xml_content)
    page_title = _clean_text((soup.find("h1") or {}).get_text(" ", strip=True) if soup.find("h1") else entry["title"])
    source_code = "ca_federal_act" if entry["kind"] == "act" else "ca_federal_regulation"
    source_uid = entry["code"]

    raw_json = {
        "jurisdiction": "Canada",
        "level": "federal",
        "kind": entry["kind"],
        "title": page_title,
        "code": entry["code"],
        "xml_url": xml_url,
        "pdf_url": pdf_url,
        "page_url": entry["page_url"],
        "index_url": entry["index_url"],
        "official_source": "Justice Laws Website",
        "fetched_at": datetime.utcnow().isoformat(),
    }

    item_id = upsert_source_item(
        source_code=source_code,
        source_uid=source_uid,
        title=page_title or entry["title"],
        item_url=entry["page_url"],
        published_at=None,
        summary=(raw_text or page_title)[:1000],
        raw_text=raw_text,
        raw_json=raw_json,
    )
    replace_item_keywords(item_id, _keyword_list(page_title, entry["code"], raw_text[:3000], limit=24))
    return {"item_id": item_id, "source_code": source_code, "source_uid": source_uid}


def sync_federal_legislation_demo(max_items: int | None = None) -> dict:
    processed = 0
    items = _parse_federal_index("act") + _parse_federal_index("regulation")
    if max_items and max_items > 0:
        items = items[: int(max_items)]

    errors = []
    for entry in items:
        try:
            stored = _fetch_federal_document(entry)
            if stored:
                processed += 1
        except Exception as exc:
            errors.append(f"{entry['page_url']}: {exc}")

    status = "success" if processed > 0 else "failed"
    message = "; ".join(errors[:8])
    log_sync("ca_legislation", status, f"Federal legislation sync completed, processed={processed}")
    return {
        "source": "ca_legislation",
        "stage": "bulk",
        "status": status,
        "processed": processed,
        "items": processed,
        "message": message,
        "error_type": "partial_failure" if errors and processed > 0 else ("sync_failed" if errors else ""),
    }


def _fetch_csv_rows(url: str) -> list[dict]:
    resp = _http_get(url)
    text_value = resp.content.decode("utf-8-sig", errors="ignore")
    return list(csv.DictReader(io.StringIO(text_value, newline="")))


def _extract_ontario_alias(title: str, kind: str) -> str:
    endpoint = ONTARIO_AUTOCOMPLETE_URL.format(term=quote(str(title or "").strip()))
    try:
        data = _http_get(endpoint).json()
    except Exception:
        return ""
    hits = (((data or {}).get("hits") or {}).get("hits") or [])
    expected_prefix = "statute/" if kind == "statute" else "regulation/"
    title_lc = _clean_text(title).lower()
    for hit in hits:
        source = hit.get("_source") or {}
        alias = ((source.get("alias") or {}).get("en") or "").strip()
        state = ((source.get("state") or {}).get("en") or "").strip().lower()
        hit_title = _clean_text((source.get("title") or {}).get("en") or "").lower()
        if not alias.startswith(expected_prefix):
            continue
        if state != "current":
            continue
        if title_lc and title_lc[:48] not in hit_title:
            continue
        return alias
    return ""


def _fetch_ontario_doc(alias: str) -> tuple[str, str]:
    parts = [part for part in str(alias or "").split("/") if part]
    if len(parts) != 2:
        return "", ""
    endpoint = f"{ONTARIO_API_BASE}/legislation/en/doc-search/{parts[0]}/{parts[1]}"
    try:
        data = _http_get(endpoint).json()
    except Exception:
        return "", ""
    content = _html_text(data.get("content") or "")
    volume = _clean_text(data.get("volume") or "")
    return content, volume


def _store_ontario_metadata_row(kind: str, row: dict, with_full_text: bool = False) -> int:
    if kind == "statute":
        title = _clean_text(row.get("Statute"))
        citation = title.split(",")[-1].strip() if "," in title else title
        source_code = "on_statute"
        summary = " | ".join(
            value for value in [
                _clean_text(row.get("Minister(s) Responsible")),
                _clean_text(row.get("Legislative History")),
            ] if value
        )
        keywords = _keyword_list(title, citation, row.get("Minister(s) Responsible"), row.get("Legislative History"), limit=24)
        on_elaws = _clean_text(row.get("On e-Laws")).lower() == "yes"
    else:
        title = _clean_text(row.get("Regulation"))
        citation = _clean_text(row.get("Citation"))
        source_code = "on_regulation"
        summary = " | ".join(
            value for value in [
                _clean_text(row.get("Enabling Statute")),
                _clean_text(row.get("Legislative History")),
            ] if value
        )
        keywords = _keyword_list(title, citation, row.get("Enabling Statute"), row.get("Legislative History"), limit=24)
        on_elaws = _clean_text(row.get("On e-Laws")).lower() == "yes"

    alias = _extract_ontario_alias(title, kind) if on_elaws and with_full_text else ""
    raw_text = ""
    if alias:
        raw_text, volume = _fetch_ontario_doc(alias)
        if volume and citation and volume not in citation:
            summary = " | ".join(value for value in [citation, summary] if value)

    item_url = f"{ONTARIO_LAWS_BASE}/{alias}" if alias else f"{ONTARIO_LAWS_BASE}?search={quote(title)}"
    source_uid = alias or citation or sha256_text(title)
    raw_json = {
        "jurisdiction": "Ontario",
        "level": "provincial",
        "kind": kind,
        "citation": citation,
        "title": title,
        "on_elaws": on_elaws,
        "alias": alias,
        "source_csv_url": ONTARIO_STATUTES_CSV_URL if kind == "statute" else ONTARIO_REGULATIONS_CSV_URL,
        "official_source": "Ontario e-Laws",
        "fetched_at": datetime.utcnow().isoformat(),
        "metadata": row,
    }
    item_id = upsert_source_item(
        source_code=source_code,
        source_uid=source_uid,
        title=title,
        item_url=item_url,
        published_at=None,
        summary=(summary or title)[:1000],
        raw_text=(raw_text or summary or title)[:12000],
        raw_json=raw_json,
    )
    replace_item_keywords(item_id, keywords)
    return item_id


def sync_ontario_legislation_demo(*, include_full_text: bool = False, max_items: int | None = None) -> dict:
    processed = 0
    errors = []
    rows = [("statute", row) for row in _fetch_csv_rows(ONTARIO_STATUTES_CSV_URL)] + [
        ("regulation", row) for row in _fetch_csv_rows(ONTARIO_REGULATIONS_CSV_URL)
    ]
    if max_items and max_items > 0:
        rows = rows[: int(max_items)]

    for kind, row in rows:
        try:
            _store_ontario_metadata_row(kind, row, with_full_text=include_full_text)
            processed += 1
        except Exception as exc:
            title = _clean_text(row.get("Statute") or row.get("Regulation"))
            errors.append(f"{title}: {exc}")

    status = "success" if processed > 0 else "failed"
    message = "; ".join(errors[:8])
    log_sync("on_legislation", status, f"Ontario legislation sync completed, processed={processed}")
    return {
        "source": "on_legislation",
        "stage": "bulk",
        "status": status,
        "processed": processed,
        "items": processed,
        "message": message,
        "error_type": "partial_failure" if errors and processed > 0 else ("sync_failed" if errors else ""),
    }


def sync_canada_legislation_demo() -> dict:
    federal_limit = max(0, int(getattr(settings, "canada_federal_sync_max_items", 0) or 0)) or None
    ontario_limit = max(0, int(getattr(settings, "ontario_legislation_sync_max_items", 0) or 0)) or None
    include_ontario_full_text = bool(getattr(settings, "ontario_legislation_include_full_text", False))
    results = [
        sync_federal_legislation_demo(max_items=federal_limit),
        sync_ontario_legislation_demo(include_full_text=include_ontario_full_text, max_items=ontario_limit),
    ]
    processed = sum(int(item.get("processed") or item.get("items") or 0) for item in results)
    return {
        "source": "canada_legislation",
        "status": "success" if processed > 0 else "failed",
        "processed": processed,
        "items": processed,
        "sources": {item["source"]: item for item in results},
        "message": "; ".join(_clean_text(item.get("message")) for item in results if item.get("message")),
    }


def sync_canada_legislation_by_keywords(keywords: list[str], target_count: int | None = None) -> dict:
    clean_keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
    if not clean_keywords:
        return {
            "source": "canada_legislation",
            "stage": "keyword",
            "status": "skipped",
            "processed": 0,
            "items": 0,
            "message": "No legislation keywords provided.",
            "error_type": "missing_keywords",
        }

    processed = 0
    errors = []
    max_items = max(1, int(target_count or getattr(settings, "remote_search_max_items_per_source", 12)))
    seen_aliases = set()

    for keyword in clean_keywords:
        try:
            hits = ((_http_get(ONTARIO_AUTOCOMPLETE_URL.format(term=quote(keyword))).json() or {}).get("hits") or {}).get("hits") or []
        except Exception as exc:
            errors.append(f"Ontario autocomplete {keyword}: {exc}")
            continue
        for hit in hits:
            source = hit.get("_source") or {}
            alias = ((source.get("alias") or {}).get("en") or "").strip()
            title = _clean_text((source.get("title") or {}).get("en") or "")
            if not alias or alias in seen_aliases:
                continue
            state = ((source.get("state") or {}).get("en") or "").strip().lower()
            if state != "current":
                continue
            seen_aliases.add(alias)
            kind = "statute" if alias.startswith("statute/") else "regulation"
            try:
                content, volume = _fetch_ontario_doc(alias)
                citation = _clean_text(volume or title.split(",")[-1])
                source_code = "on_statute" if kind == "statute" else "on_regulation"
                item_id = upsert_source_item(
                    source_code=source_code,
                    source_uid=alias,
                    title=title,
                    item_url=f"{ONTARIO_LAWS_BASE}/{alias}",
                    published_at=None,
                    summary=(citation or title)[:1000],
                    raw_text=(content or title)[:12000],
                    raw_json={
                        "jurisdiction": "Ontario",
                        "level": "provincial",
                        "kind": kind,
                        "citation": citation,
                        "title": title,
                        "alias": alias,
                        "state": state,
                        "source_api": "autocomplete+doc-search",
                        "official_source": "Ontario e-Laws",
                        "fetched_at": datetime.utcnow().isoformat(),
                    },
                )
                replace_item_keywords(item_id, _keyword_list(title, citation, content[:3000], limit=24))
                processed += 1
                if processed >= max_items:
                    break
            except Exception as exc:
                errors.append(f"{alias}: {exc}")
        if processed >= max_items:
            break

    status = "success" if processed > 0 else "failed"
    return {
        "source": "canada_legislation",
        "stage": "keyword",
        "status": status,
        "processed": processed,
        "items": processed,
        "message": "; ".join(errors[:8]),
        "error_type": "partial_failure" if errors and processed > 0 else ("sync_failed" if errors else ""),
    }

