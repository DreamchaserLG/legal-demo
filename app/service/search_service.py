import copy
import time
from datetime import datetime

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, fetch_all
from app.service.common_service import split_keywords
from app.service.crawler_service import sync_all_sources
from app.service.ingestion_task_service import enqueue_or_reuse_hydration_task
from app.service.ofac_service import OFAC_DISCOVERY_PAGE, OFAC_SEARCH_PORTAL

VALID_SOURCES = {"all", "ofac", "canlii"}
VALID_SORTS = {"relevance", "recent"}
_SEARCH_CACHE: dict[tuple, tuple[float, dict]] = {}
_MAX_SCORE_PER_KEYWORD = 13.0



def _normalize_limit(value, default: int = 30) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 100))



def _normalize_offset(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, parsed)



def _normalize_source(value: str) -> str:
    source = (value or "all").strip().lower()
    return source if source in VALID_SOURCES else "all"



def _normalize_sort(value: str) -> str:
    sort = (value or "relevance").strip().lower()
    return sort if sort in VALID_SORTS else "relevance"



def _normalize_keywords_input(value: str | list[str]) -> str:
    if isinstance(value, list):
        return ", ".join([str(item).strip() for item in value if str(item).strip()])
    return str(value or "").strip()



def _cache_ttl() -> int:
    return max(0, int(getattr(settings, "cache_ttl_seconds", 300)))



def _get_cached_result(cache: dict, key: tuple):
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    entry = cache.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at <= time.time():
        cache.pop(key, None)
        return None
    return copy.deepcopy(payload)



def _set_cached_result(cache: dict, key: tuple, value: dict):
    ttl = _cache_ttl()
    if ttl <= 0:
        return
    cache[key] = (time.time() + ttl, copy.deepcopy(value))



def clear_search_cache():
    _SEARCH_CACHE.clear()



def _similarity_threshold(keyword: str) -> float:
    size = len((keyword or "").strip())
    if size <= 4:
        return 0.95
    if size <= 7:
        return 0.75
    return 0.55



def _insert_search_log(input_text: str | list[str], result_count: int):
    query_text = _normalize_keywords_input(input_text)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO sync_logs (source_code, status, message)
                VALUES ('search', 'success', :message)
                """
            ),
            {"message": f"query={query_text}, results={result_count}"},
        )



def _fmt_dt(value):
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)



def _clip(text, length=240):
    if not text:
        return ""
    cleaned = str(text).strip().replace("\n", " ")
    if len(cleaned) <= length:
        return cleaned
    return cleaned[:length] + "..."


def _normalize_relevance_score(score_value, keyword_count: int) -> float:
    try:
        raw_score = float(score_value or 0)
    except (TypeError, ValueError):
        raw_score = 0.0

    denominator = max(1.0, float(max(1, keyword_count)) * _MAX_SCORE_PER_KEYWORD)
    normalized = max(0.0, min(raw_score / denominator, 1.0))
    return round(normalized, 4)


def _decorate_result_rows(rows: list[dict], keyword_count: int) -> list[dict]:
    decorated = []
    for row in rows:
        item = dict(row)
        item["raw_score"] = row.get("score", 0)
        item["score"] = _normalize_relevance_score(row.get("score", 0), keyword_count)
        item["relevance_score"] = item["score"]
        decorated.append(item)
    return decorated



def _build_ofac_card(row: dict) -> dict:
    meta = row.get("raw_json") or {}
    aliases = meta.get("aliases") or []
    addresses = meta.get("addresses") or []

    fields = [
        ("名单编号", meta.get("ent_num")),
        ("主体类型", meta.get("sdn_type")),
        ("制裁项目", meta.get("program")),
        ("职务/说明", meta.get("title_name")),
        ("呼号", meta.get("call_sign")),
        ("船舶类型", meta.get("vess_type")),
        ("吨位", meta.get("tonnage")),
        ("GRT", meta.get("grt")),
        ("船旗国", meta.get("vess_flag")),
        ("所有人", meta.get("vess_owner")),
    ]
    fields = [(label, value) for label, value in fields if value]

    return {
        "id": row["id"],
        "source_code": "ofac",
        "source_label": "OFAC 制裁名单",
        "title": meta.get("sdn_name") or row.get("title"),
        "subtitle": " / ".join([part for part in [meta.get("sdn_type"), meta.get("program")] if part]),
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(meta.get("remarks") or row.get("summary"), 280),
        "excerpt": "",
        "url": meta.get("official_search_url") or row.get("item_url") or OFAC_SEARCH_PORTAL,
        "source_url": meta.get("source_csv_url") or ((meta.get("source_urls") or {}).get("sdn")) or OFAC_DISCOVERY_PAGE,
        "score": row.get("score", 0),
        "fields": fields,
        "aliases": aliases[:10],
        "addresses": addresses[:6],
    }



def _build_canlii_card(row: dict) -> dict:
    meta = row.get("raw_json") or {}
    database_page = meta.get("database_page", "")

    fields = [
        ("发布时间", _fmt_dt(row.get("published_at"))),
    ]
    fields = [(label, value) for label, value in fields if value and value != "-"]

    return {
        "id": row["id"],
        "source_code": "canlii",
        "source_label": "CanLII 案例",
        "title": row.get("title"),
        "subtitle": _clip(database_page, 80),
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(row.get("summary"), 260),
        "excerpt": _clip(row.get("raw_text"), 800),
        "url": row.get("item_url"),
        "source_url": database_page if str(database_page).startswith("http") else "",
        "score": row.get("score", 0),
        "fields": fields,
        "aliases": [],
        "addresses": [],
    }



def _prepare_cards(rows: list[dict]) -> dict:
    groups = {"canlii": [], "ofac": [], "other": []}

    for row in rows:
        source_code = row.get("source_code")
        if source_code == "ofac":
            groups["ofac"].append(_build_ofac_card(row))
        elif source_code == "canlii":
            groups["canlii"].append(_build_canlii_card(row))
        else:
            meta = row.get("raw_json") or {}
            groups["other"].append(
                {
                    "id": row["id"],
                    "source_code": source_code,
                    "source_label": source_code,
                    "title": row.get("title"),
                    "subtitle": "",
                    "published_at": _fmt_dt(row.get("published_at")),
                    "summary": _clip(row.get("summary"), 260),
                    "excerpt": _clip(row.get("raw_text"), 800),
                    "url": row.get("item_url"),
                    "source_url": meta.get("source_url", ""),
                    "score": row.get("score", 0),
                    "fields": [],
                    "aliases": [],
                    "addresses": [],
                }
            )

    return groups



def search_items(
    keywords_input: str | list[str],
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    refresh: bool = False,
):
    keywords = split_keywords(keywords_input)
    limit = _normalize_limit(limit)
    offset = _normalize_offset(offset)
    source = _normalize_source(source)
    sort = _normalize_sort(sort)
    cache_key = ("search", tuple(keywords), limit, offset, source, sort)

    if not keywords:
        return {
            "keywords": [],
            "total": 0,
            "page_count": 0,
            "offset": offset,
            "source": source,
            "sort": sort,
            "results": [],
            "grouped_results": {"canlii": [], "ofac": [], "other": []},
            "source_counts": {"canlii": 0, "ofac": 0, "other": 0},
            "has_previous": False,
            "has_next": False,
            "previous_offset": 0,
            "next_offset": 0,
            "cache_status": "miss",
        }

    if not refresh:
        cached = _get_cached_result(_SEARCH_CACHE, cache_key)
        if cached is not None:
            cached["cache_status"] = "hit"
            return cached

    conditions = []
    score_parts = []
    params = {"limit": limit, "offset": offset}

    for index, keyword in enumerate(keywords):
        key = f"kw{index}"
        exact_key = f"kw_exact{index}"
        sim_key = f"kw_sim{index}"
        params[exact_key] = keyword.lower()
        params[key] = f"%{keyword.lower()}%"
        params[sim_key] = _similarity_threshold(keyword)
        conditions.append(
            f"""
            LOWER(COALESCE(si.title, '')) LIKE :{key}
            OR LOWER(COALESCE(si.summary, '')) LIKE :{key}
            OR LOWER(COALESCE(si.raw_text, '')) LIKE :{key}
            OR word_similarity(LOWER(COALESCE(si.title, '')), :{exact_key}) >= :{sim_key}
            OR EXISTS (
                SELECT 1
                FROM item_keywords ik
                WHERE ik.item_id = si.id
                  AND (
                      LOWER(ik.keyword) LIKE :{key}
                      OR word_similarity(LOWER(ik.keyword), :{exact_key}) >= :{sim_key}
                  )
            )
            """
        )
        score_parts.append(
            f"""
            CASE WHEN LOWER(COALESCE(si.title, '')) LIKE :{key} THEN 5 ELSE 0 END
            + CASE WHEN LOWER(COALESCE(si.summary, '')) LIKE :{key} THEN 3 ELSE 0 END
            + CASE WHEN LOWER(COALESCE(si.raw_text, '')) LIKE :{key} THEN 1 ELSE 0 END
            + CASE WHEN word_similarity(LOWER(COALESCE(si.title, '')), :{exact_key}) >= :{sim_key} THEN 2 ELSE 0 END
            + CASE WHEN EXISTS (
                SELECT 1
                FROM item_keywords ik
                WHERE ik.item_id = si.id
                  AND (
                      LOWER(ik.keyword) LIKE :{key}
                      OR word_similarity(LOWER(ik.keyword), :{exact_key}) >= :{sim_key}
                  )
            ) THEN 2 ELSE 0 END
            """
        )

    source_clause = ""
    if source in {"ofac", "canlii"}:
        source_clause = "AND si.source_code = :source"
        params["source"] = source

    where_clause = f"({' OR '.join(conditions)}) {source_clause}"
    score_expr = " + ".join(score_parts)
    order_clause = (
        "score DESC, COALESCE(si.published_at, si.updated_at, si.created_at) DESC"
        if sort == "relevance"
        else "COALESCE(si.published_at, si.updated_at, si.created_at) DESC, score DESC"
    )

    count_sql = f"""
    SELECT COUNT(*) AS total
    FROM source_items si
    WHERE {where_clause}
    """
    count_rows = fetch_all(count_sql, params)
    total_matches = int(count_rows[0]["total"]) if count_rows else 0

    source_count_sql = f"""
    SELECT si.source_code, COUNT(*) AS total
    FROM source_items si
    WHERE {where_clause}
    GROUP BY si.source_code
    """
    source_count_rows = fetch_all(source_count_sql, params)
    source_counts = {"canlii": 0, "ofac": 0, "other": 0}
    for row in source_count_rows:
        key = row.get("source_code")
        count = int(row.get("total") or 0)
        if key in source_counts:
            source_counts[key] = count
        else:
            source_counts["other"] += count

    sql = f"""
    SELECT
        si.id,
        si.source_code,
        si.source_uid,
        si.title,
        si.item_url,
        si.published_at,
        si.summary,
        si.raw_text,
        si.raw_json,
        si.created_at,
        si.updated_at,
        ({score_expr}) AS score
    FROM source_items si
    WHERE {where_clause}
    ORDER BY {order_clause}
    OFFSET :offset
    LIMIT :limit
    """

    rows = _decorate_result_rows(fetch_all(sql, params), len(keywords))
    grouped_results = _prepare_cards(rows)
    _insert_search_log(keywords_input, len(rows))

    previous_offset = max(offset - limit, 0)
    next_offset = offset + limit

    payload = {
        "keywords": keywords,
        "total": total_matches,
        "page_count": len(rows),
        "offset": offset,
        "source": source,
        "sort": sort,
        "results": rows,
        "grouped_results": grouped_results,
        "source_counts": source_counts,
        "has_previous": offset > 0,
        "has_next": next_offset < total_matches,
        "previous_offset": previous_offset,
        "next_offset": next_offset,
        "cache_status": "miss",
    }
    _set_cached_result(_SEARCH_CACHE, cache_key, payload)
    return payload



def _remote_search_threshold(limit: int) -> int:
    configured = max(1, int(getattr(settings, "remote_search_trigger_count", 1)))
    return max(limit, configured)



def _should_hydrate_remotely(result: dict, keywords: list[str], offset: int, limit: int) -> bool:
    if not getattr(settings, "remote_search_enabled", True):
        return False
    if not getattr(settings, "async_hydration_enabled", True):
        return False
    if not keywords:
        return False
    if offset > 0:
        return False
    return int(result.get("total") or 0) < _remote_search_threshold(limit)


def search_with_remote_hydration(
    keywords_input: str | list[str],
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    refresh: bool = False,
    origin_page: str = "search",
):
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    normalized_source = _normalize_source(source)
    normalized_sort = _normalize_sort(sort)

    result = search_items(
        keywords_input,
        limit=normalized_limit,
        offset=normalized_offset,
        source=normalized_source,
        sort=normalized_sort,
        refresh=refresh,
    )

    remote_fetch = {
        "status": "local_only",
        "processed": 0,
        "sources": {},
        "message": "当前直接使用本地数据检索。",
    }
    search_strategy = "local_only"

    if _should_hydrate_remotely(result, result.get("keywords", []), normalized_offset, normalized_limit):
        task = enqueue_or_reuse_hydration_task(
            query_text=_normalize_keywords_input(keywords_input),
            keywords=result.get("keywords", []),
            source_filter=normalized_source,
            desired_count=normalized_limit,
            current_local_count=int(result.get("total") or 0),
            origin_page=origin_page,
        )
        if task:
            terminal_without_growth = (
                task.get("is_terminal")
                and int(task.get("result_total_after") or 0) <= int(result.get("total") or 0)
            )
            remote_fetch = {
                "status": task.get("status", "queued"),
                "processed": int(task.get("items_processed") or 0),
                "sources": task.get("sources", {}),
                "message": (
                    f"当前只找到 {int(result.get('total') or 0)} 条可用结果，冷却期内不会重复补抓同一请求。"
                    if terminal_without_growth
                    else task.get("message")
                    or "本地命中不足，已提交后台扩库任务，完成后页面会自动刷新。"
                ),
                "task": task,
            }
            search_strategy = "local_then_async"

    return {
        **result,
        "remote_fetch": remote_fetch,
        "search_strategy": search_strategy,
    }



def search_and_optionally_sync(
    keywords_input: str | list[str],
    sync_first: bool = False,
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    refresh: bool = False,
    origin_page: str = "search",
):
    sync_info = None
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    normalized_source = _normalize_source(source)
    normalized_sort = _normalize_sort(sort)

    if sync_first:
        sync_info = sync_all_sources()
        clear_search_cache()
        result = search_items(
            keywords_input,
            limit=normalized_limit,
            offset=normalized_offset,
            source=normalized_source,
            sort=normalized_sort,
            refresh=True,
        )
        result["remote_fetch"] = {
            "status": "manual_sync",
            "processed": 0,
            "sources": sync_info.get("sources", {}),
            "message": "已执行手动同步，当前结果来自同步后的本地库。",
        }
        result["search_strategy"] = "manual_sync_then_local"
    else:
        result = search_with_remote_hydration(
            keywords_input,
            limit=normalized_limit,
            offset=normalized_offset,
            source=normalized_source,
            sort=normalized_sort,
            refresh=refresh,
            origin_page=origin_page,
        )

    return {
        "keywords_input": _normalize_keywords_input(keywords_input),
        "limit": normalized_limit,
        "offset": normalized_offset,
        "source": normalized_source,
        "sort": normalized_sort,
        "sync_info": sync_info,
        **result,
    }
