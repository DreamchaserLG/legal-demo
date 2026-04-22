import csv
import io
import ssl
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin

import certifi
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings
from app.service.common_service import log_sync, replace_item_keywords, upsert_source_item

OFAC_DISCOVERY_PAGE = "https://ofac.treasury.gov/sanctions-list-service"
OFAC_SEARCH_PORTAL = "https://sanctionssearch.ofac.treas.gov/"
DEFAULT_CSV_URLS = {
    "sdn": "https://www.treasury.gov/ofac/downloads/sdn.csv",
    "add": "https://www.treasury.gov/ofac/downloads/add.csv",
    "alt": "https://www.treasury.gov/ofac/downloads/alt.csv",
    "comments": "https://www.treasury.gov/ofac/downloads/sdn_comments.csv",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
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



def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text == "-0-":
        return ""
    return text



def _download_csv_rows(url: str) -> List[Dict]:
    resp = _http_get(url)
    content = resp.content.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))
    return [dict(row) for row in reader]



def _discover_ofac_csv_urls() -> Dict[str, Optional[str]]:
    result = {
        "sdn": settings.ofac_sdn_csv_url or None,
        "add": settings.ofac_add_csv_url or None,
        "alt": settings.ofac_alt_csv_url or None,
        "comments": settings.ofac_sdn_comments_csv_url or None,
    }

    if not (result["sdn"] and result["add"] and result["alt"]):
        try:
            resp = _http_get(OFAC_DISCOVERY_PAGE)
            soup = BeautifulSoup(resp.text, "html.parser")

            for anchor in soup.find_all("a", href=True):
                href = anchor["href"].strip()
                if not href:
                    continue

                full_url = urljoin(OFAC_DISCOVERY_PAGE, href)
                lowered = full_url.lower()

                if "sdn_comments.csv" in lowered and not result["comments"]:
                    result["comments"] = full_url
                elif "sdn.csv" in lowered and not result["sdn"]:
                    result["sdn"] = full_url
                elif "add.csv" in lowered and not result["add"]:
                    result["add"] = full_url
                elif "alt.csv" in lowered and not result["alt"]:
                    result["alt"] = full_url
        except Exception:
            pass

    for key, default_url in DEFAULT_CSV_URLS.items():
        if not result.get(key):
            result[key] = default_url

    return result



def _build_address_map(rows: List[Dict]) -> Dict[str, List[Dict]]:
    result = defaultdict(list)

    for row in rows:
        ent_num = _clean(row.get("Ent_num") or row.get("ent_num"))
        if not ent_num:
            continue

        address = _clean(row.get("Address"))
        city_line = _clean(row.get("City/State/Province/Postal Code") or row.get("City/"))
        country = _clean(row.get("Country"))
        remarks = _clean(row.get("Add_remarks"))

        display = " | ".join([x for x in [address, city_line, country, remarks] if x])
        result[ent_num].append(
            {
                "address": address,
                "city_line": city_line,
                "country": country,
                "remarks": remarks,
                "display": display,
            }
        )

    return result



def _build_alias_map(rows: List[Dict]) -> Dict[str, List[Dict]]:
    result = defaultdict(list)

    for row in rows:
        ent_num = _clean(row.get("Ent_num") or row.get("ent_num"))
        if not ent_num:
            continue

        alt_name = _clean(row.get("alt_name"))
        if not alt_name:
            continue

        result[ent_num].append(
            {
                "type": _clean(row.get("alt_type")),
                "name": alt_name,
                "remarks": _clean(row.get("alt_remarks")),
            }
        )

    return result



def _build_comment_map(rows: List[Dict]) -> Dict[str, str]:
    result = defaultdict(str)

    for row in rows:
        ent_num = _clean(row.get("Ent_num") or row.get("ent_num"))
        remarks = _clean(row.get("Remarks") or row.get("remarks") or row.get("SDN_Comments"))
        if not ent_num or not remarks:
            continue
        result[ent_num] = f"{result[ent_num]} {remarks}".strip()

    return result



def _extract_keywords_from_ofac_record(title: str, program: str, sdn_type: str, aliases: List[Dict], addresses: List[Dict]) -> List[str]:
    parts = []

    if title:
        parts.extend(title.replace(",", " ").replace("/", " ").split())
    if program:
        parts.extend(program.replace(";", ",").split(","))
    if sdn_type:
        parts.extend(sdn_type.split())

    for alias in aliases[:10]:
        alias_name = alias.get("name", "")
        if alias_name:
            parts.extend(alias_name.replace(",", " ").split())

    for address in addresses[:5]:
        country = address.get("country", "")
        if country:
            parts.append(country)

    result = []
    seen = set()
    for piece in parts + ["OFAC", "SDN", "sanction"]:
        keyword = str(piece).strip(" ,.;:()[]{}\"'")
        if len(keyword) < 3:
            continue
        lowered = keyword.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(keyword)
        if len(result) >= 25:
            break

    return result



def _matches_keywords(keywords: List[str], *values) -> bool:
    haystack = " ".join([str(value or "") for value in values]).lower()
    return any(str(keyword or "").strip().lower() in haystack for keyword in keywords if str(keyword or "").strip())



def _upsert_ofac_record(
    row: Dict,
    aliases: List[Dict],
    addresses: List[Dict],
    remarks: str,
    urls: Dict[str, Optional[str]],
    fetch_mode: str = "sync",
) -> int:
    ent_num = _clean(row.get("ent_num") or row.get("Ent_num"))
    sdn_name = _clean(row.get("SDN_Name"))
    sdn_type = _clean(row.get("SDN_Type"))
    program = _clean(row.get("Program"))
    title_name = _clean(row.get("Title"))
    call_sign = _clean(row.get("Call_Sign"))
    vess_type = _clean(row.get("Vess_type"))
    tonnage = _clean(row.get("Tonnage"))
    grt = _clean(row.get("GRT"))
    vess_flag = _clean(row.get("Vess_flag"))
    vess_owner = _clean(row.get("Vess_owner"))

    summary = " | ".join([x for x in [sdn_type, program, remarks] if x])
    raw_json = {
        "ent_num": ent_num,
        "sdn_name": sdn_name,
        "sdn_type": sdn_type,
        "program": program,
        "title_name": title_name,
        "call_sign": call_sign,
        "vess_type": vess_type,
        "tonnage": tonnage,
        "grt": grt,
        "vess_flag": vess_flag,
        "vess_owner": vess_owner,
        "remarks": remarks,
        "aliases": aliases,
        "addresses": addresses,
        "source_urls": urls,
        "official_search_url": OFAC_SEARCH_PORTAL,
        "source_csv_url": urls.get("sdn") or "",
        "fetch_mode": fetch_mode,
        "fetched_at": datetime.utcnow().isoformat(),
    }

    item_id = upsert_source_item(
        source_code="ofac",
        source_uid=ent_num,
        title=sdn_name,
        item_url=OFAC_SEARCH_PORTAL,
        published_at=None,
        summary=summary[:1000],
        raw_text=remarks[:3000] if remarks else "",
        raw_json=raw_json,
    )

    replace_item_keywords(
        item_id,
        _extract_keywords_from_ofac_record(
            title=sdn_name,
            program=program,
            sdn_type=sdn_type,
            aliases=aliases,
            addresses=addresses,
        ),
    )
    return 1



def _load_ofac_reference_maps(urls: Dict[str, Optional[str]]):
    sdn_rows = _download_csv_rows(urls["sdn"])
    add_rows = _download_csv_rows(urls["add"])
    alt_rows = _download_csv_rows(urls["alt"])
    comment_rows = _download_csv_rows(urls["comments"]) if urls.get("comments") else []

    address_map = _build_address_map(add_rows)
    alias_map = _build_alias_map(alt_rows)
    comment_map = _build_comment_map(comment_rows)
    return sdn_rows, address_map, alias_map, comment_map



def sync_ofac_demo():
    try:
        urls = _discover_ofac_csv_urls()
        required = ["sdn", "add", "alt"]
        missing = [key.upper() for key in required if not urls.get(key)]
        if missing:
            message = f"Missing OFAC CSV URLs: {', '.join(missing)}"
            log_sync("ofac", "skipped", message)
            return {
                "source": "ofac",
                "status": "skipped",
                "items": 0,
                "message": message,
                "error_type": "missing_config",
            }

        sdn_rows, address_map, alias_map, comment_map = _load_ofac_reference_maps(urls)
        processed = 0

        for row in sdn_rows:
            ent_num = _clean(row.get("ent_num") or row.get("Ent_num"))
            sdn_name = _clean(row.get("SDN_Name"))
            if not ent_num or not sdn_name:
                continue

            remarks = _clean(row.get("Remarks"))
            if comment_map.get(ent_num):
                remarks = f"{remarks} {comment_map[ent_num]}".strip()

            processed += _upsert_ofac_record(
                row=row,
                aliases=alias_map.get(ent_num, []),
                addresses=address_map.get(ent_num, []),
                remarks=remarks,
                urls=urls,
                fetch_mode="sync",
            )

        message = f"OFAC sync completed, processed={processed}"
        log_sync("ofac", "success", message)
        return {
            "source": "ofac",
            "status": "success",
            "processed": processed,
            "items": processed,
            "message": "",
            "error_type": "",
        }
    except Exception as exc:
        message = f"OFAC sync failed: {exc}"
        log_sync("ofac", "failed", message)
        return {
            "source": "ofac",
            "status": "failed",
            "items": 0,
            "message": str(exc),
            "error_type": type(exc).__name__,
        }



def sync_ofac_by_keywords(keywords: List[str], target_count: int | None = None):
    clean_keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
    if not clean_keywords:
        return {
            "source": "ofac",
            "status": "skipped",
            "processed": 0,
            "items": 0,
            "message": "No keywords were provided for OFAC hydration.",
            "error_type": "missing_keywords",
        }

    try:
        urls = _discover_ofac_csv_urls()
        sdn_rows, address_map, alias_map, comment_map = _load_ofac_reference_maps(urls)
        processed = 0
        configured_limit = max(1, int(getattr(settings, "remote_search_max_items_per_source", 12)))
        requested_limit = max(1, int(target_count or 0)) if target_count else 0
        match_limit = max(configured_limit, requested_limit * 2 if requested_limit else 0)

        for row in sdn_rows:
            if processed >= match_limit:
                break

            ent_num = _clean(row.get("ent_num") or row.get("Ent_num"))
            sdn_name = _clean(row.get("SDN_Name"))
            if not ent_num or not sdn_name:
                continue

            aliases = alias_map.get(ent_num, [])
            addresses = address_map.get(ent_num, [])
            remarks = _clean(row.get("Remarks"))
            if comment_map.get(ent_num):
                remarks = f"{remarks} {comment_map[ent_num]}".strip()

            if not _matches_keywords(
                clean_keywords,
                sdn_name,
                row.get("Program"),
                row.get("SDN_Type"),
                remarks,
                " ".join([alias.get("name", "") for alias in aliases]),
                " ".join([address.get("display", "") for address in addresses]),
            ):
                continue

            processed += _upsert_ofac_record(
                row=row,
                aliases=aliases,
                addresses=addresses,
                remarks=remarks,
                urls=urls,
                fetch_mode="search_hydration",
            )

        status = "success" if processed > 0 else "skipped"
        error_type = "" if processed > 0 else "no_match"
        message = (
            f"OFAC remote hydration stored {processed} items."
            if processed > 0
            else "No OFAC records matched the requested keywords."
        )
        log_sync("ofac", status, f"OFAC remote hydration done, processed={processed}, keywords={clean_keywords}")
        return {
            "source": "ofac",
            "status": status,
            "processed": processed,
            "items": processed,
            "message": message,
            "error_type": error_type,
        }
    except Exception as exc:
        message = f"OFAC remote hydration failed: {exc}"
        log_sync("ofac", "failed", message)
        return {
            "source": "ofac",
            "status": "failed",
            "processed": 0,
            "items": 0,
            "message": str(exc),
            "error_type": type(exc).__name__,
        }
