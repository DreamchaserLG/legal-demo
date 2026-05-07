import copy
import time
from datetime import datetime

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, fetch_all
from app.service.bilingual_service import (
    build_bilingual_keyword_bundle,
    build_display_pair,
    enrich_result_rows_bilingual,
)
from app.service.common_service import plain_text_preview, repair_text, split_keywords
from app.service.crawler_service import sync_all_sources
from app.service.ingestion_task_service import (
    enqueue_or_reuse_hydration_task,
    get_recent_terminal_hydration_task,
)
from app.service.module_service import build_module_packet, get_module_definition, normalize_module, resolve_source_for_module
from app.service.ofac_service import OFAC_DISCOVERY_PAGE, OFAC_SEARCH_PORTAL

VALID_SOURCES = {"all", "ofac", "canlii", "canada"}
VALID_SORTS = {"relevance", "recent"}
_SEARCH_CACHE: dict[tuple, tuple[float, dict]] = {}
_MAX_SCORE_PER_KEYWORD = 13.0
_CANADA_SOURCE_CODES = {
    "canlii",
    "ca_federal_act",
    "ca_federal_regulation",
    "on_statute",
    "on_regulation",
    "manual_canada_case",
    "url_canada_case",
    "manual_canada_rule",
    "url_canada_rule",
}
_LEGISLATION_SOURCE_CODES = {"ca_federal_act", "ca_federal_regulation", "on_statute", "on_regulation", "manual_canada_rule", "url_canada_rule"}
_SUPREME_COURT_CODES = {"scc", "uksc"}
_APPEAL_COURT_CODES = {"fca", "onca", "abca", "bcca", "mbca", "nbca", "nlca", "nsca", "ntca", "nuca", "qcca", "skca", "ykca", "pescad"}
_SUPERIOR_COURT_CODES = {"fc", "onsc", "abkb", "abqb", "bcsc", "mbkb", "mbqb", "nbkb", "nbqb", "nlsc", "nssc", "ntsc", "qccs", "skkb", "skqb", "yksc", "pecsc"}
_PROVINCIAL_COURT_CODES = {"oncj", "ocj", "qccq", "skpc", "yktc", "nstc", "nspc", "pecp", "ntpc", "nupc"}



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
    return repair_text(str(value))



def _clip(text, length=240):
    if not text:
        return ""
    cleaned = plain_text_preview(text)
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


def _court_level_label(level_value, court_code: str = "") -> str:
    try:
        level = int(level_value or 0)
    except (TypeError, ValueError):
        level = 0
    code = str(court_code or "").strip().upper()
    if level >= 5:
        return "最高法院"
    if level == 4:
        return "上诉法院"
    if level == 3:
        return "高等法院 / 联邦法院"
    if level == 2:
        return "省级 / 地方法院"
    if level == 1:
        return "Tribunal / Other"
    return code or "-"


def _court_level_sql() -> tuple[str, str]:
    database_page_expr = "LOWER(COALESCE(si.raw_json->>'database_page', ''))"
    item_url_expr = "LOWER(COALESCE(si.item_url, ''))"
    court_code_expr = (
        "COALESCE("
        f"substring({database_page_expr} from '/([a-z0-9]+)/?$'), "
        f"substring({item_url_expr} from '/([a-z0-9]+)/doc/'), "
        "''"
        ")"
    )

    def code_list(values: set[str]) -> str:
        return ", ".join(f"'{value}'" for value in sorted(values))

    court_level_expr = f"""
    CASE
        WHEN si.source_code <> 'canlii' THEN 0
        WHEN {court_code_expr} IN ({code_list(_SUPREME_COURT_CODES)}) THEN 5
        WHEN {court_code_expr} IN ({code_list(_APPEAL_COURT_CODES)}) THEN 4
        WHEN {court_code_expr} IN ({code_list(_SUPERIOR_COURT_CODES)}) THEN 3
        WHEN {court_code_expr} IN ({code_list(_PROVINCIAL_COURT_CODES)}) THEN 2
        WHEN {court_code_expr} <> '' THEN 1
        ELSE 0
    END
    """
    return court_code_expr, court_level_expr


def _decorate_result_rows(rows: list[dict], keyword_count: int) -> list[dict]:
    decorated = []
    for row in rows:
        item = dict(row)
        item["raw_score"] = row.get("score", 0)
        item["score"] = _normalize_relevance_score(row.get("score", 0), keyword_count)
        item["relevance_score"] = item["score"]
        item["court_level"] = int(row.get("court_level") or 0)
        item["court_code"] = str(row.get("court_code") or "").strip().lower()
        item["court_level_label"] = _court_level_label(item["court_level"], item["court_code"])
        decorated.append(item)
    return decorated



def _source_title_pair(original_title: str, preview: dict | None) -> dict:
    preview = preview or {}
    original = repair_text(original_title)
    english = repair_text(preview.get("title_en")) or original
    chinese = repair_text(preview.get("title_zh"))
    secondary = chinese if chinese and chinese not in {original, english} else ""
    return {"primary": english or original, "secondary": secondary}


def _build_ofac_card(row: dict, query_language: str) -> dict:
    meta = row.get("raw_json") or {}
    preview = ((meta.get("translations") or {}).get("preview") or {})
    title_pair = _source_title_pair(meta.get("sdn_name") or row.get("title"), preview)
    summary_pair = build_display_pair(
        meta.get("remarks") or row.get("summary"),
        {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
        query_language,
    )
    aliases = meta.get("aliases") or []
    addresses = meta.get("addresses") or []

    fields = [
        ("Record", meta.get("ent_num")),
        ("Type", meta.get("sdn_type")),
        ("Program", meta.get("program")),
        ("Title", meta.get("title_name")),
        ("Flag", meta.get("vess_flag")),
        ("Owner", meta.get("vess_owner")),
    ]
    fields = [(label, repair_text(value)) for label, value in fields if repair_text(value)]

    return {
        "id": row["id"],
        "source_code": "ofac",
        "source_label": "OFAC Record",
        "title": repair_text(meta.get("sdn_name") or row.get("title")),
        "title_primary": title_pair["primary"],
        "title_secondary": title_pair["secondary"],
        "subtitle": " / ".join(
            [repair_text(part) for part in [meta.get("sdn_type"), meta.get("program")] if repair_text(part)]
        ),
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(meta.get("remarks") or row.get("summary"), 280),
        "summary_primary": summary_pair["primary"],
        "summary_secondary": summary_pair["secondary"],
        "excerpt": "",
        "url": meta.get("official_search_url") or row.get("item_url") or OFAC_SEARCH_PORTAL,
        "source_url": meta.get("source_csv_url") or ((meta.get("source_urls") or {}).get("sdn")) or OFAC_DISCOVERY_PAGE,
        "score": row.get("score", 0),
        "fields": fields,
        "aliases": aliases[:10],
        "addresses": addresses[:6],
    }


def _build_canlii_card(row: dict, query_language: str) -> dict:
    meta = row.get("raw_json") or {}
    preview = ((meta.get("translations") or {}).get("preview") or {})
    title_pair = _source_title_pair(row.get("title"), preview)
    summary_pair = build_display_pair(
        row.get("summary"),
        {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
        query_language,
    )
    database_page = meta.get("database_page", "")

    fields = [
        ("Date", _fmt_dt(row.get("published_at"))),
        ("Court Level", row.get("court_level_label")),
        ("Court Code", str(row.get("court_code") or "").upper() or "-"),
    ]
    fields = [(label, repair_text(value)) for label, value in fields if value and value != "-"]

    return {
        "id": row["id"],
        "source_code": "canlii",
        "source_label": "Case",
        "title": repair_text(row.get("title")),
        "title_primary": title_pair["primary"],
        "title_secondary": title_pair["secondary"],
        "subtitle": _clip(database_page, 80),
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(row.get("summary"), 260),
        "summary_primary": summary_pair["primary"],
        "summary_secondary": summary_pair["secondary"],
        "excerpt": _clip(row.get("raw_text"), 800),
        "url": row.get("item_url"),
        "source_url": database_page if str(database_page).startswith("http") else "",
        "score": row.get("score", 0),
        "fields": fields,
        "aliases": [],
        "addresses": [],
    }


def _build_legislation_card(row: dict, query_language: str) -> dict:
    meta = row.get("raw_json") or {}
    preview = ((meta.get("translations") or {}).get("preview") or {})
    title_pair = _source_title_pair(row.get("title"), preview)
    summary_pair = build_display_pair(
        row.get("summary") or row.get("raw_text"),
        {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
        query_language,
    )
    source_code = str(row.get("source_code") or "")
    jurisdiction = repair_text(meta.get("jurisdiction") or "Canada")
    level = str(meta.get("level") or ("federal" if source_code.startswith("ca_federal_") else "provincial")).lower()
    kind = str(meta.get("kind") or ("act" if "act" in source_code or "statute" in source_code else "regulation")).lower()
    citation = repair_text(meta.get("citation") or meta.get("code") or "")
    fields = [
        ("Jurisdiction", jurisdiction),
        ("Level", "Federal" if level == "federal" else "Provincial / Local"),
        ("Type", {"act": "Act", "statute": "Statute", "regulation": "Regulation"}.get(kind, kind.title())),
    ]
    if citation:
        fields.append(("Citation", citation))
    return {
        "id": row["id"],
        "source_code": source_code,
        "source_label": "Official Law",
        "title": repair_text(row.get("title")),
        "title_primary": title_pair["primary"],
        "title_secondary": title_pair["secondary"],
        "subtitle": citation,
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(row.get("summary") or row.get("raw_text"), 280),
        "summary_primary": summary_pair["primary"],
        "summary_secondary": summary_pair["secondary"],
        "excerpt": _clip(row.get("raw_text"), 900),
        "url": row.get("item_url"),
        "source_url": meta.get("xml_url") or meta.get("source_csv_url") or meta.get("index_url") or "",
        "score": row.get("score", 0),
        "fields": fields,
        "aliases": [],
        "addresses": [],
    }


def _prepare_cards(rows: list[dict], query_language: str) -> dict:
    groups = {"legislation": [], "canlii": [], "ofac": [], "other": []}

    for row in rows:
        source_code = row.get("source_code")
        if source_code in _LEGISLATION_SOURCE_CODES:
            groups["legislation"].append(_build_legislation_card(row, query_language))
            continue
        if source_code == "ofac":
            groups["ofac"].append(_build_ofac_card(row, query_language))
            continue
        if source_code == "canlii":
            groups["canlii"].append(_build_canlii_card(row, query_language))
            continue

        meta = row.get("raw_json") or {}
        preview = ((meta.get("translations") or {}).get("preview") or {})
        title_pair = _source_title_pair(row.get("title"), preview)
        summary_pair = build_display_pair(
            row.get("summary"),
            {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
            query_language,
        )
        groups["other"].append(
            {
                "id": row["id"],
                "source_code": source_code,
                "source_label": repair_text(str(source_code or "record").upper()),
                "title": repair_text(row.get("title")),
                "title_primary": title_pair["primary"],
                "title_secondary": title_pair["secondary"],
                "subtitle": "",
                "published_at": _fmt_dt(row.get("published_at")),
                "summary": _clip(row.get("summary"), 260),
                "summary_primary": summary_pair["primary"],
                "summary_secondary": summary_pair["secondary"],
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
    module: str = "canada",
    refresh: bool = False,
    display_language: str | None = None,
):
    keyword_bundle = build_bilingual_keyword_bundle(keywords_input, module)
    keywords = keyword_bundle.get("retrieval_keywords") or split_keywords(keywords_input)
    limit = _normalize_limit(limit)
    offset = _normalize_offset(offset)
    module = normalize_module(module)
    source = resolve_source_for_module(module, source)
    sort = _normalize_sort(sort)
    query_language = str(display_language or keyword_bundle.get("query_language") or "zh")
    cache_key = ("search", module, tuple(keywords), limit, offset, source, sort, query_language)

    if not keywords:
        return {
            "input_text": _normalize_keywords_input(keywords_input),
            "keywords": [],
            "total": 0,
            "page_count": 0,
            "offset": offset,
            "source": source,
            "sort": sort,
            "module_code": module,
            "module_profile": get_module_definition(module),
            "query_language": query_language,
            "bilingual_query": keyword_bundle,
            "results": [],
            "grouped_results": {"legislation": [], "canlii": [], "ofac": [], "other": []},
            "source_counts": {"legislation": 0, "canlii": 0, "ofac": 0, "other": 0},
            "has_previous": False,
            "has_next": False,
            "previous_offset": 0,
            "next_offset": 0,
            "cache_status": "miss",
            "module_packet": build_module_packet(
                module,
                {
                    "input_text": _normalize_keywords_input(keywords_input),
                    "module_code": module,
                    "query_language": query_language,
                    "results": [],
                    "analysis": {},
                    "intake_outline": {},
                },
                refresh=refresh,
            ),
        }

    if not refresh:
        cached = _get_cached_result(_SEARCH_CACHE, cache_key)
        if cached is not None:
            cached["cache_status"] = "hit"
            return cached

    conditions = []
    score_parts = []
    params = {"limit": limit, "offset": offset}
    court_code_expr, court_level_expr = _court_level_sql()

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
    elif source == "canada":
        source_clause = "AND si.source_code = ANY(:canada_sources)"
        params["canada_sources"] = list(_CANADA_SOURCE_CODES)

    where_clause = f"({' OR '.join(conditions)}) {source_clause}"
    score_expr = " + ".join(score_parts)
    order_clause = (
        "court_level DESC, score DESC, COALESCE(si.published_at, si.updated_at, si.created_at) DESC"
        if sort == "relevance"
        else "court_level DESC, COALESCE(si.published_at, si.updated_at, si.created_at) DESC, score DESC"
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
    source_counts = {"legislation": 0, "canlii": 0, "ofac": 0, "other": 0}
    for row in source_count_rows:
        key = row.get("source_code")
        count = int(row.get("total") or 0)
        if key in _LEGISLATION_SOURCE_CODES:
            source_counts["legislation"] += count
        elif key in {"canlii", "manual_canada_case", "url_canada_case"}:
            source_counts["canlii"] += count
        elif key in source_counts:
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
        ({score_expr}) AS score,
        {court_code_expr} AS court_code,
        ({court_level_expr}) AS court_level
    FROM source_items si
    WHERE {where_clause}
    ORDER BY {order_clause}
    OFFSET :offset
    LIMIT :limit
    """

    rows = _decorate_result_rows(fetch_all(sql, params), len(keywords))
    for row in rows:
        row["query_language"] = query_language
    rows = enrich_result_rows_bilingual(rows, query_language=query_language)
    grouped_results = _prepare_cards(rows, query_language)
    _insert_search_log(keywords_input, len(rows))

    previous_offset = max(offset - limit, 0)
    next_offset = offset + limit

    payload = {
        "input_text": _normalize_keywords_input(keywords_input),
        "keywords": keywords,
        "total": total_matches,
        "page_count": len(rows),
        "offset": offset,
        "source": source,
        "sort": sort,
        "module_code": module,
        "module_profile": get_module_definition(module),
        "query_language": query_language,
        "bilingual_query": keyword_bundle,
        "results": rows,
        "grouped_results": grouped_results,
        "source_counts": source_counts,
        "has_previous": offset > 0,
        "has_next": next_offset < total_matches,
        "previous_offset": previous_offset,
        "next_offset": next_offset,
        "cache_status": "miss",
    }
    payload["module_packet"] = build_module_packet(module, payload, refresh=refresh)
    _set_cached_result(_SEARCH_CACHE, cache_key, payload)
    return payload



def _remote_search_threshold(limit: int) -> int:
    configured = max(1, int(getattr(settings, "remote_search_trigger_count", 1)))
    return max(limit, configured)



def _should_hydrate_remotely(
    result: dict,
    keywords: list[str],
    offset: int,
    limit: int,
    *,
    force_hydration: bool = False,
    hydration_target_count: int | None = None,
) -> bool:
    if not getattr(settings, "remote_search_enabled", True):
        return False
    if not getattr(settings, "async_hydration_enabled", True):
        return False
    if not keywords:
        return False
    if offset > 0:
        return False
    threshold = _remote_search_threshold(limit)
    if hydration_target_count:
        threshold = max(threshold, max(1, int(hydration_target_count)))
    if force_hydration:
        return True
    return int(result.get("total") or 0) < _remote_search_threshold(limit)


def _should_skip_remote_hydration_after_zero_result(result: dict, recent_terminal_task: dict | None) -> bool:
    if not recent_terminal_task:
        return False
    if not recent_terminal_task.get("is_terminal"):
        return False
    if str(recent_terminal_task.get("status") or "").strip().lower() != "completed":
        return False
    if int(result.get("total") or 0) > 0:
        return False
    return int(recent_terminal_task.get("result_total_after") or 0) <= 0


def search_with_remote_hydration(
    keywords_input: str | list[str],
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    module: str = "canada",
    refresh: bool = False,
    origin_page: str = "search",
    force_hydration: bool = False,
    hydration_target_count: int | None = None,
    hydration_reason: str = "",
    display_language: str | None = None,
    local_only: bool = False,
):
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    normalized_sort = _normalize_sort(sort)
    normalized_module = normalize_module(module)
    normalized_source = resolve_source_for_module(normalized_module, source)

    result = search_items(
        keywords_input,
        limit=normalized_limit,
        offset=normalized_offset,
        source=normalized_source,
        sort=normalized_sort,
        module=normalized_module,
        refresh=refresh,
        display_language=display_language,
    )

    remote_fetch = {
        "status": "local_only",
        "processed": 0,
        "sources": {},
        "message": "当前直接使用本地数据检索。",
    }
    search_strategy = "local_only"

    if not local_only and _should_hydrate_remotely(
        result,
        result.get("keywords", []),
        normalized_offset,
        normalized_limit,
        force_hydration=force_hydration,
        hydration_target_count=hydration_target_count,
    ):
        recent_terminal_task = get_recent_terminal_hydration_task(
            keywords=result.get("keywords", []),
            source_filter=normalized_source,
            desired_count=hydration_target_count or normalized_limit,
        )
        if _should_skip_remote_hydration_after_zero_result(result, recent_terminal_task):
            remote_fetch = {
                "status": "zero_result_cached",
                "processed": int(recent_terminal_task.get("items_processed") or 0),
                "sources": recent_terminal_task.get("sources", {}),
                "message": "同一组关键词最近已经补抓过且仍然没有结果，本次直接返回 0。",
            }
            search_strategy = "local_zero_result"
        else:
            task = enqueue_or_reuse_hydration_task(
                query_text=_normalize_keywords_input(keywords_input),
                keywords=result.get("keywords", []),
                source_filter=normalized_source,
                desired_count=hydration_target_count or normalized_limit,
                current_local_count=int(result.get("total") or 0),
                origin_page=origin_page,
            )
            if task:
                terminal_without_growth = (
                    task.get("is_terminal")
                    and int(task.get("result_total_after") or 0) <= int(result.get("total") or 0)
                )
                if force_hydration and hydration_reason == "new_case_enrichment":
                    remote_message = (
                        f"这是一条新案情。系统已在本地命中 {int(result.get('total') or 0)} 条结果，"
                        f"但仍会额外扩充外部资料，目标补到 {int(hydration_target_count or normalized_limit)} 条附近。"
                    )
                elif terminal_without_growth:
                    remote_message = f"当前只找到 {int(result.get('total') or 0)} 条可用结果，冷却期内不会重复补抓同一请求。"
                else:
                    remote_message = task.get("message") or "本地命中不足，已提交后台扩库任务，完成后页面会自动刷新。"
                remote_fetch = {
                    "status": task.get("status", "queued"),
                    "processed": int(task.get("items_processed") or 0),
                    "sources": task.get("sources", {}),
                    "message": remote_message,
                    "task": task,
                }
                search_strategy = "local_then_async_enrichment" if force_hydration else "local_then_async"

    payload = {
        **result,
        "module_code": normalized_module,
        "module_profile": get_module_definition(normalized_module),
        "remote_fetch": remote_fetch,
        "search_strategy": "local_only" if local_only else search_strategy,
        "hydration_target_count": hydration_target_count or normalized_limit,
        "force_hydration": bool(force_hydration),
        "hydration_reason": hydration_reason,
    }
    if local_only:
        payload["remote_fetch"] = {
            "status": "local_only",
            "processed": 0,
            "sources": {},
            "message": "当前仅使用本地数据库匹配，不会实时访问远程网页。",
        }
    return payload



def search_and_optionally_sync(
    keywords_input: str | list[str],
    sync_first: bool = False,
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    module: str = "canada",
    refresh: bool = False,
    origin_page: str = "search",
    display_language: str | None = None,
):
    sync_info = None
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    normalized_sort = _normalize_sort(sort)
    normalized_module = normalize_module(module)
    normalized_source = resolve_source_for_module(normalized_module, source)

    if sync_first:
        sync_info = sync_all_sources()
        clear_search_cache()
        result = search_items(
            keywords_input,
            limit=normalized_limit,
            offset=normalized_offset,
            source=normalized_source,
            sort=normalized_sort,
            module=normalized_module,
            refresh=True,
            display_language=display_language,
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
            module=normalized_module,
            refresh=refresh,
            origin_page=origin_page,
            display_language=display_language,
        )

    return {
        "keywords_input": _normalize_keywords_input(keywords_input),
        "limit": normalized_limit,
        "offset": normalized_offset,
        "source": normalized_source,
        "sort": normalized_sort,
        "module_code": normalized_module,
        "module_profile": get_module_definition(normalized_module),
        "sync_info": sync_info,
        **result,
    }
