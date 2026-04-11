from datetime import datetime

from sqlalchemy import text

from app.core.database import engine, fetch_all
from app.service.common_service import split_keywords
from app.service.crawler_service import sync_all_sources

VALID_SOURCES = {"all", "ofac", "canlii"}
VALID_SORTS = {"relevance", "recent"}


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


def _insert_search_log(input_text: str, result_count: int):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO sync_logs (source_code, status, message)
                VALUES ('search', 'success', :message)
                """
            ),
            {"message": f"query={input_text}, results={result_count}"},
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
    text = str(text).strip().replace("\n", " ")
    if len(text) <= length:
        return text
    return text[:length] + "..."


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
    fields = [(k, v) for k, v in fields if v]

    return {
        "id": row["id"],
        "source_code": "ofac",
        "source_label": "OFAC 制裁名单",
        "title": meta.get("sdn_name") or row.get("title"),
        "subtitle": " / ".join([x for x in [meta.get("sdn_type"), meta.get("program")] if x]),
        "published_at": _fmt_dt(row.get("published_at")),
        "summary": _clip(meta.get("remarks") or row.get("summary"), 280),
        "excerpt": "",
        "url": row.get("item_url"),
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
        ("来源数据库页", database_page),
    ]
    fields = [(k, v) for k, v in fields if v and v != "-"]

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
                    "score": row.get("score", 0),
                    "fields": [],
                    "aliases": [],
                    "addresses": [],
                }
            )

    return groups


def search_items(
    keywords_input: str,
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
):
    keywords = split_keywords(keywords_input)
    limit = _normalize_limit(limit)
    offset = _normalize_offset(offset)
    source = _normalize_source(source)
    sort = _normalize_sort(sort)

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
        }

    conditions = []
    score_parts = []
    params = {"limit": limit, "offset": offset}

    for i, kw in enumerate(keywords):
        key = f"kw{i}"
        params[key] = f"%{kw.lower()}%"
        conditions.append(
            f"""
            LOWER(COALESCE(si.title, '')) LIKE :{key}
            OR LOWER(COALESCE(si.summary, '')) LIKE :{key}
            OR LOWER(COALESCE(si.raw_text, '')) LIKE :{key}
            OR EXISTS (
                SELECT 1
                FROM item_keywords ik
                WHERE ik.item_id = si.id
                  AND LOWER(ik.keyword) LIKE :{key}
            )
            """
        )
        score_parts.append(
            f"""
            CASE WHEN LOWER(COALESCE(si.title, '')) LIKE :{key} THEN 5 ELSE 0 END
            + CASE WHEN LOWER(COALESCE(si.summary, '')) LIKE :{key} THEN 3 ELSE 0 END
            + CASE WHEN LOWER(COALESCE(si.raw_text, '')) LIKE :{key} THEN 1 ELSE 0 END
            + CASE WHEN EXISTS (
                SELECT 1
                FROM item_keywords ik
                WHERE ik.item_id = si.id
                  AND LOWER(ik.keyword) LIKE :{key}
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

    rows = fetch_all(sql, params)
    results = rows
    grouped_results = _prepare_cards(results)

    _insert_search_log(keywords_input, len(results))

    previous_offset = max(offset - limit, 0)
    next_offset = offset + limit

    return {
        "keywords": keywords,
        "total": total_matches,
        "page_count": len(results),
        "offset": offset,
        "source": source,
        "sort": sort,
        "results": results,
        "grouped_results": grouped_results,
        "source_counts": source_counts,
        "has_previous": offset > 0,
        "has_next": next_offset < total_matches,
        "previous_offset": previous_offset,
        "next_offset": next_offset,
    }


def search_and_optionally_sync(
    keywords_input: str,
    sync_first: bool = False,
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
):
    sync_info = None
    if sync_first:
        sync_info = sync_all_sources()

    result = search_items(
        keywords_input,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
    )

    return {
        "keywords_input": keywords_input,
        "sync_first": sync_first,
        "limit": _normalize_limit(limit),
        "offset": _normalize_offset(offset),
        "source": _normalize_source(source),
        "sort": _normalize_sort(sort),
        "sync_info": sync_info,
        **result,
    }
