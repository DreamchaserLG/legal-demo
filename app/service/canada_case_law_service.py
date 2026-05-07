import json
import re
from collections import Counter, defaultdict

from sqlalchemy import text

from app.core.database import engine
from app.service.common_service import plain_text_preview, repair_text

LEGISLATION_SOURCE_CODES = {"ca_federal_act", "ca_federal_regulation", "on_statute", "on_regulation"}

_LAW_NAME_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z0-9\u2018\u2019'().,&/\-]*(?:\s+(?:[A-Z][A-Za-z0-9\u2018\u2019'().,&/\-]*|of|and|the|for|to|in|on|de|du|des|la|le|et)){0,8}\s+(?:Act|Code|Rules|Regulation(?:s)?|Charter|Convention|Order))\b"
)
_SECTION_PREFIX_RE = re.compile(
    r"^(?:(?:r(?:ule)?|rules?|s(?:ection)?|ss?\.?)\s*[A-Za-z0-9().,\- ]+\s*(?:of|under)\s+)+",
    re.IGNORECASE,
)
_EXPLICIT_LAW_PHRASES = ("Rules of Civil Procedure",)


def ensure_canada_law_tables():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS canada_laws (
            id BIGSERIAL PRIMARY KEY,
            source_item_id BIGINT NULL,
            source_code VARCHAR(50) NOT NULL DEFAULT '',
            source_uid TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            slug VARCHAR(240) NOT NULL,
            citation TEXT NOT NULL DEFAULT '',
            jurisdiction TEXT NOT NULL DEFAULT 'Canada',
            law_level TEXT NOT NULL DEFAULT '',
            law_kind TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            aliases_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            origin VARCHAR(30) NOT NULL DEFAULT 'inferred',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_canada_laws_normalized_title
        ON canada_laws(normalized_title)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_canada_laws_slug
        ON canada_laws(slug)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_canada_laws_source_item
        ON canada_laws(source_item_id)
        WHERE source_item_id IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS canada_case_law_links (
            id BIGSERIAL PRIMARY KEY,
            case_item_id BIGINT NOT NULL,
            law_id BIGINT NOT NULL,
            matched_alias TEXT NOT NULL DEFAULT '',
            match_source VARCHAR(40) NOT NULL DEFAULT '',
            match_score NUMERIC(6, 4) NOT NULL DEFAULT 0,
            evidence_excerpt TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_canada_case_law_links_pair
        ON canada_case_law_links(case_item_id, law_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_canada_case_law_links_case
        ON canada_case_law_links(case_item_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_canada_case_law_links_law
        ON canada_case_law_links(law_id)
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _law_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", repair_text(title).lower()).strip("-")
    return slug[:220] or "law"


def _normalize_law_title(raw_title: str) -> str:
    title = repair_text(raw_title)
    title = _SECTION_PREFIX_RE.sub("", title).strip()
    title = re.sub(r"\s+", " ", title).strip(" ,.;:()[]{}")
    lowered = title.lower()
    for marker in [" under the ", " under an ", " under a ", " under ", " pursuant to "]:
        if marker in lowered:
            title = title[lowered.rfind(marker) + len(marker):].strip()
            lowered = title.lower()
    title = re.sub(r"^(?:the\s+)", "", title, flags=re.IGNORECASE).strip()
    return title


def _is_valid_law_title(title: str) -> bool:
    normalized = _normalize_law_title(title)
    if len(normalized) < 8 or len(normalized) > 180:
        return False
    first_word = normalized.split(" ", 1)[0].lower()
    if first_word in {"whether", "should", "must", "could", "would", "did", "does", "is", "are", "was", "were", "not", "no", "breach", "failure", "issue", "application"}:
        return False
    if normalized.lower().endswith("order"):
        return False
    if normalized.lower().endswith("procedure") and "rules of" not in normalized.lower():
        return False
    if normalized.lower().endswith("act rules"):
        return False
    return True


def _law_aliases(title: str, citation: str = "", extra_aliases=None) -> list[str]:
    values = [repair_text(title), _normalize_law_title(title), repair_text(citation)]
    base_title = repair_text(title)
    if "," in base_title:
        values.append(base_title.split(",", 1)[0].strip())
    normalized = _normalize_law_title(base_title)
    if normalized.lower().endswith("charter"):
        values.extend(
            [
                "Canadian Charter of Rights and Freedoms",
                "Canadian Charter",
                "Charter rights",
            ]
        )
    for item in extra_aliases or []:
        values.append(repair_text(item))

    aliases = []
    seen = set()
    for item in values:
        clean = repair_text(item)
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        aliases.append(clean)
    aliases.sort(key=lambda item: (-len(item), item.lower()))
    return aliases


def _case_lookup_text(row: dict, limit: int = 8000) -> str:
    summary = plain_text_preview(row.get("summary"))
    raw_text = plain_text_preview(row.get("raw_text"))[:limit]
    return repair_text(" ".join([summary, raw_text]))


def _extract_inferred_titles(case_rows: list[dict]) -> list[dict]:
    title_counts = Counter()
    title_samples: dict[str, str] = {}

    for row in case_rows:
        seen_for_case = set()
        combined_text = _case_lookup_text(row, limit=2600)
        for match in _LAW_NAME_PATTERN.findall(combined_text):
            normalized = _normalize_law_title(match)
            if not _is_valid_law_title(normalized):
                continue
            lowered = normalized.lower()
            if lowered in seen_for_case:
                continue
            seen_for_case.add(lowered)
            title_counts[lowered] += 1
            title_samples.setdefault(lowered, normalized)
        lowered_text = combined_text.lower()
        for phrase in _EXPLICIT_LAW_PHRASES:
            if phrase.lower() not in lowered_text:
                continue
            normalized = _normalize_law_title(phrase)
            lowered = normalized.lower()
            if lowered in seen_for_case or not _is_valid_law_title(normalized):
                continue
            seen_for_case.add(lowered)
            title_counts[lowered] += 1
            title_samples.setdefault(lowered, normalized)

    records = []
    for normalized, count in title_counts.items():
        records.append(
            {
                "title": title_samples[normalized],
                "normalized_title": normalized,
                "mention_count": count,
            }
        )
    return records


def _upsert_law_record(
    *,
    title: str,
    normalized_title: str,
    citation: str = "",
    jurisdiction: str = "Canada",
    law_level: str = "",
    law_kind: str = "",
    source_code: str = "",
    source_uid: str = "",
    source_url: str = "",
    source_item_id: int | None = None,
    aliases=None,
    origin: str = "inferred",
):
    payload = {
        "source_item_id": source_item_id,
        "source_code": source_code,
        "source_uid": source_uid,
        "title": repair_text(title),
        "normalized_title": repair_text(normalized_title).lower(),
        "slug": _law_slug(normalized_title or title),
        "citation": repair_text(citation),
        "jurisdiction": repair_text(jurisdiction or "Canada"),
        "law_level": repair_text(law_level),
        "law_kind": repair_text(law_kind),
        "source_url": repair_text(source_url),
        "aliases_json": json.dumps(_law_aliases(title, citation, aliases), ensure_ascii=False),
        "origin": origin,
    }

    def resolve_slug(conn, base_slug: str, normalized_value: str) -> str:
        existing = conn.execute(
            text(
                """
                SELECT slug
                FROM canada_laws
                WHERE normalized_title = :normalized_title
                LIMIT 1
                """
            ),
            {"normalized_title": normalized_value},
        ).mappings().first()
        if existing and repair_text(existing.get("slug")):
            return repair_text(existing.get("slug"))

        slug = repair_text(base_slug) or "law"
        counter = 2
        while True:
            slug_row = conn.execute(
                text(
                    """
                    SELECT normalized_title
                    FROM canada_laws
                    WHERE slug = :slug
                    LIMIT 1
                    """
                ),
                {"slug": slug},
            ).mappings().first()
            if not slug_row:
                return slug
            if repair_text(slug_row.get("normalized_title")).lower() == normalized_value.lower():
                return slug
            suffix = f"-{counter}"
            trimmed = slug[: max(1, 220 - len(suffix))]
            slug = f"{trimmed}{suffix}"
            counter += 1

    sql = """
    INSERT INTO canada_laws (
        source_item_id,
        source_code,
        source_uid,
        title,
        normalized_title,
        slug,
        citation,
        jurisdiction,
        law_level,
        law_kind,
        source_url,
        aliases_json,
        origin,
        created_at,
        updated_at
    )
    VALUES (
        :source_item_id,
        :source_code,
        :source_uid,
        :title,
        :normalized_title,
        :slug,
        :citation,
        :jurisdiction,
        :law_level,
        :law_kind,
        :source_url,
        CAST(:aliases_json AS jsonb),
        :origin,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    )
    ON CONFLICT (normalized_title)
    DO UPDATE SET
        source_item_id = COALESCE(EXCLUDED.source_item_id, canada_laws.source_item_id),
        source_code = CASE WHEN EXCLUDED.origin = 'official' THEN EXCLUDED.source_code ELSE canada_laws.source_code END,
        source_uid = CASE WHEN EXCLUDED.origin = 'official' THEN EXCLUDED.source_uid ELSE canada_laws.source_uid END,
        title = CASE WHEN EXCLUDED.origin = 'official' THEN EXCLUDED.title ELSE canada_laws.title END,
        citation = CASE WHEN EXCLUDED.origin = 'official' AND EXCLUDED.citation <> '' THEN EXCLUDED.citation ELSE canada_laws.citation END,
        jurisdiction = CASE WHEN EXCLUDED.origin = 'official' THEN EXCLUDED.jurisdiction ELSE canada_laws.jurisdiction END,
        law_level = CASE WHEN EXCLUDED.origin = 'official' AND EXCLUDED.law_level <> '' THEN EXCLUDED.law_level ELSE canada_laws.law_level END,
        law_kind = CASE WHEN EXCLUDED.origin = 'official' AND EXCLUDED.law_kind <> '' THEN EXCLUDED.law_kind ELSE canada_laws.law_kind END,
        source_url = CASE WHEN EXCLUDED.origin = 'official' AND EXCLUDED.source_url <> '' THEN EXCLUDED.source_url ELSE canada_laws.source_url END,
        aliases_json = CASE WHEN EXCLUDED.origin = 'official' THEN EXCLUDED.aliases_json ELSE canada_laws.aliases_json END,
        origin = CASE WHEN EXCLUDED.origin = 'official' THEN 'official' ELSE canada_laws.origin END,
        updated_at = CURRENT_TIMESTAMP
    RETURNING id
    """
    with engine.begin() as conn:
        payload["slug"] = resolve_slug(conn, payload.get("slug", ""), payload["normalized_title"])
        row = conn.execute(text(sql), payload).mappings().first()
    return int(row["id"]) if row else 0


def _seed_official_laws(law_rows: list[dict]):
    for row in law_rows:
        meta = row.get("raw_json") or {}
        title = repair_text(row.get("title"))
        normalized = _normalize_law_title(title)
        if not _is_valid_law_title(normalized):
            continue
        _upsert_law_record(
            title=title,
            normalized_title=normalized,
            citation=repair_text(meta.get("citation") or meta.get("code") or ""),
            jurisdiction=repair_text(meta.get("jurisdiction") or "Canada"),
            law_level=repair_text(meta.get("level") or ""),
            law_kind=repair_text(meta.get("kind") or ""),
            source_code=repair_text(row.get("source_code")),
            source_uid=repair_text(row.get("source_uid")),
            source_url=repair_text(row.get("item_url") or meta.get("xml_url") or meta.get("source_csv_url") or ""),
            source_item_id=int(row.get("id")) if row.get("id") is not None else None,
            aliases=[repair_text(meta.get("alias") or "")],
            origin="official",
        )


def _seed_inferred_laws(case_rows: list[dict]):
    for record in _extract_inferred_titles(case_rows):
        _upsert_law_record(
            title=record["title"],
            normalized_title=record["normalized_title"],
            aliases=[],
            origin="inferred",
        )


def _fetch_laws_by_normalized(normalized_titles: list[str]) -> list[dict]:
    if not normalized_titles:
        return []
    sql = """
    SELECT *
    FROM canada_laws
    WHERE normalized_title = ANY(:normalized_titles)
    ORDER BY origin = 'official' DESC, title ASC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"normalized_titles": normalized_titles}).mappings().all()
    return [dict(row) for row in rows]


def _fetch_law_by_slug(law_slug: str) -> dict | None:
    sql = """
    SELECT *
    FROM canada_laws
    WHERE slug = :slug
    LIMIT 1
    """
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"slug": repair_text(law_slug)}).mappings().first()
    return dict(row) if row else None


def _match_excerpt(text_value: str, alias: str, radius: int = 180) -> tuple[str, int]:
    haystack = repair_text(text_value)
    if not haystack or not alias:
        return "", 0
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)
    match = pattern.search(haystack)
    if not match:
        return "", 0
    start = max(0, match.start() - radius)
    end = min(len(haystack), match.end() + radius)
    return haystack[start:end].strip(), len(pattern.findall(haystack))


def _law_match_payload(case_row: dict, law_row: dict) -> dict | None:
    text_value = _case_lookup_text(case_row)
    aliases = list(law_row.get("aliases_json") or [])
    if not aliases:
        aliases = _law_aliases(law_row.get("title"), law_row.get("citation"))

    best_match = None
    for alias in aliases:
        clean_alias = repair_text(alias)
        if len(clean_alias) < 6 and clean_alias.lower() not in {"charter"}:
            continue
        excerpt, occurrences = _match_excerpt(text_value, clean_alias)
        if not occurrences:
            continue
        if clean_alias.lower() == repair_text(law_row.get("title")).lower():
            base_score = 0.97 if law_row.get("origin") == "official" else 0.88
            match_source = "official_title" if law_row.get("origin") == "official" else "inferred_title"
        elif clean_alias.lower() == repair_text(law_row.get("citation")).lower():
            base_score = 0.95
            match_source = "citation"
        else:
            base_score = 0.92 if law_row.get("origin") == "official" else 0.83
            match_source = "alias"
        score = min(0.99, base_score + min(0.03, max(0, occurrences - 1) * 0.01))
        candidate = {
            "case_item_id": int(case_row["id"]),
            "law_id": int(law_row["id"]),
            "matched_alias": clean_alias,
            "match_source": match_source,
            "match_score": round(score, 4),
            "evidence_excerpt": excerpt[:600],
        }
        if not best_match or candidate["match_score"] > best_match["match_score"]:
            best_match = candidate
    return best_match


def _replace_case_links(case_rows: list[dict], law_rows: list[dict]):
    case_ids = [int(row["id"]) for row in case_rows if row.get("id") is not None]
    if not case_ids:
        return

    payloads = []
    for case_row in case_rows:
        for law_row in law_rows:
            match = _law_match_payload(case_row, law_row)
            if match:
                payloads.append(match)

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM canada_case_law_links WHERE case_item_id = ANY(:case_ids)"),
            {"case_ids": case_ids},
        )
        for payload in payloads:
            conn.execute(
                text(
                    """
                    INSERT INTO canada_case_law_links (
                        case_item_id,
                        law_id,
                        matched_alias,
                        match_source,
                        match_score,
                        evidence_excerpt,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :case_item_id,
                        :law_id,
                        :matched_alias,
                        :match_source,
                        :match_score,
                        :evidence_excerpt,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (case_item_id, law_id)
                    DO UPDATE SET
                        matched_alias = EXCLUDED.matched_alias,
                        match_source = EXCLUDED.match_source,
                        match_score = EXCLUDED.match_score,
                        evidence_excerpt = EXCLUDED.evidence_excerpt,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                payload,
            )


def _replace_links_for_single_law(case_rows: list[dict], law_row: dict):
    case_ids = [int(row["id"]) for row in case_rows if row.get("id") is not None]
    if not case_ids:
        return

    payloads = []
    for case_row in case_rows:
        match = _law_match_payload(case_row, law_row)
        if match:
            payloads.append(match)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM canada_case_law_links
                WHERE case_item_id = ANY(:case_ids)
                  AND law_id = :law_id
                """
            ),
            {"case_ids": case_ids, "law_id": int(law_row["id"])},
        )
        for payload in payloads:
            conn.execute(
                text(
                    """
                    INSERT INTO canada_case_law_links (
                        case_item_id,
                        law_id,
                        matched_alias,
                        match_source,
                        match_score,
                        evidence_excerpt,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :case_item_id,
                        :law_id,
                        :matched_alias,
                        :match_source,
                        :match_score,
                        :evidence_excerpt,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (case_item_id, law_id)
                    DO UPDATE SET
                        matched_alias = EXCLUDED.matched_alias,
                        match_source = EXCLUDED.match_source,
                        match_score = EXCLUDED.match_score,
                        evidence_excerpt = EXCLUDED.evidence_excerpt,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                payload,
            )


def _fetch_case_links(case_ids: list[int]) -> list[dict]:
    if not case_ids:
        return []
    sql = """
    SELECT
        cl.case_item_id,
        cl.law_id,
        cl.matched_alias,
        cl.match_source,
        cl.match_score,
        cl.evidence_excerpt,
        l.title,
        l.normalized_title,
        l.slug,
        l.citation,
        l.jurisdiction,
        l.law_level,
        l.law_kind,
        l.source_url,
        l.origin
    FROM canada_case_law_links cl
    JOIN canada_laws l ON l.id = cl.law_id
    WHERE cl.case_item_id = ANY(:case_ids)
    ORDER BY cl.case_item_id ASC, cl.match_score DESC, l.origin = 'official' DESC, l.title ASC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"case_ids": case_ids}).mappings().all()
    return [dict(row) for row in rows]


def _fetch_all_canlii_cases() -> list[dict]:
    sql = """
    SELECT
        id,
        source_code,
        source_uid,
        title,
        item_url,
        published_at,
        summary,
        raw_text,
        raw_json,
        created_at,
        updated_at
    FROM source_items
    WHERE source_code = 'canlii'
    ORDER BY published_at DESC NULLS LAST, id DESC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).mappings().all()
    return [dict(row) for row in rows]


def build_canada_result_graph(results: list[dict], refresh: bool = False) -> dict:
    ensure_canada_law_tables()
    case_rows = [row for row in results if row.get("source_code") == "canlii"]
    law_rows_from_results = [row for row in results if row.get("source_code") in LEGISLATION_SOURCE_CODES]

    _seed_official_laws(law_rows_from_results)
    _seed_inferred_laws(case_rows)

    normalized_titles = {
        _normalize_law_title(row.get("title"))
        for row in law_rows_from_results
        if _is_valid_law_title(row.get("title") or "")
    }
    normalized_titles.update(
        record["normalized_title"]
        for record in _extract_inferred_titles(case_rows)
        if record.get("normalized_title")
    )
    law_rows = _fetch_laws_by_normalized(sorted(normalized_titles))
    if case_rows and (refresh or normalized_titles):
        _replace_case_links(case_rows, law_rows)

    case_ids = [int(row["id"]) for row in case_rows if row.get("id") is not None]
    links = _fetch_case_links(case_ids)
    links_by_case: dict[int, list[dict]] = defaultdict(list)
    case_count_by_law = Counter()
    for item in links:
        links_by_case[int(item["case_item_id"])].append(item)
        case_count_by_law[int(item["law_id"])] += 1

    law_map = {int(item["id"]): item for item in law_rows}
    laws = []
    for law_row in law_rows:
        law_id = int(law_row["id"])
        link_count = int(case_count_by_law.get(law_id, 0))
        title = repair_text(law_row.get("title"))
        reason = (
            f"当前结果里有 {link_count} 个案例正文直接提及这部法规。"
            if link_count
            else "这是当前结果中优先保留的法规锚点。"
        )
        laws.append(
            {
                "law_id": law_id,
                "title": title,
                "citation": repair_text(law_row.get("citation")),
                "origin": repair_text(law_row.get("origin") or "inferred"),
                "level": repair_text(law_row.get("law_level") or ("official" if law_row.get("origin") == "official" else "inferred")),
                "kind": repair_text(law_row.get("law_kind")),
                "jurisdiction": repair_text(law_row.get("jurisdiction") or "Canada"),
                "source_url": repair_text(law_row.get("source_url")),
                "slug": repair_text(law_row.get("slug")),
                "linked_case_count": link_count,
                "reason": reason,
            }
        )

    laws.sort(
        key=lambda item: (
            0 if item["origin"] == "official" else 1,
            -int(item.get("linked_case_count") or 0),
            item["title"].lower(),
        )
    )
    return {
        "laws": laws,
        "links_by_case": {key: value for key, value in links_by_case.items()},
        "law_map": law_map,
    }


def refresh_law_links_for_all_cases(law_slug: str):
    ensure_canada_law_tables()
    law_row = _fetch_law_by_slug(law_slug)
    if not law_row:
        return None
    case_rows = _fetch_all_canlii_cases()
    _replace_links_for_single_law(case_rows, law_row)
    return law_row


def get_canada_law_detail_data(law_slug: str, refresh: bool = False) -> dict | None:
    ensure_canada_law_tables()
    law_row = _fetch_law_by_slug(law_slug)
    if not law_row:
        return None
    if refresh:
        law_row = refresh_law_links_for_all_cases(law_slug)
        if not law_row:
            return None

    sql = """
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
        cl.matched_alias,
        cl.match_source,
        cl.match_score,
        cl.evidence_excerpt
    FROM canada_case_law_links cl
    JOIN source_items si ON si.id = cl.case_item_id
    WHERE cl.law_id = :law_id
    ORDER BY cl.match_score DESC, si.published_at DESC NULLS LAST, si.id DESC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"law_id": int(law_row["id"])}).mappings().all()

    return {
        "law": dict(law_row),
        "cases": [dict(row) for row in rows],
    }


def _prune_orphan_inferred_laws():
    sql = """
    DELETE FROM canada_laws
    WHERE origin = 'inferred'
      AND id NOT IN (
          SELECT DISTINCT law_id
          FROM canada_case_law_links
      )
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def bootstrap_canada_law_graph(force: bool = False) -> dict:
    ensure_canada_law_tables()
    case_rows = _fetch_all_canlii_cases()

    official_sql = """
    SELECT
        id,
        source_code,
        source_uid,
        title,
        item_url,
        published_at,
        summary,
        raw_text,
        raw_json,
        created_at,
        updated_at
    FROM source_items
    WHERE source_code = ANY(:source_codes)
    ORDER BY id DESC
    """
    with engine.connect() as conn:
        official_rows = conn.execute(
            text(official_sql),
            {"source_codes": list(LEGISLATION_SOURCE_CODES)},
        ).mappings().all()

    _seed_official_laws([dict(row) for row in official_rows])
    _seed_inferred_laws(case_rows)

    normalized_titles = [
        record["normalized_title"]
        for record in _extract_inferred_titles(case_rows)
        if record.get("normalized_title")
    ]
    normalized_titles.extend(
        _normalize_law_title(row.get("title"))
        for row in official_rows
        if _is_valid_law_title(row.get("title") or "")
    )
    law_rows = _fetch_laws_by_normalized(sorted(set(filter(None, normalized_titles))))
    if force or case_rows:
        _replace_case_links(case_rows, law_rows)
    _prune_orphan_inferred_laws()

    with engine.connect() as conn:
        law_count_row = conn.execute(text("SELECT COUNT(*) AS total FROM canada_laws")).mappings().first()
        link_count_row = conn.execute(text("SELECT COUNT(*) AS total FROM canada_case_law_links")).mappings().first()

    return {
        "cases_processed": len(case_rows),
        "laws_total": int((law_count_row or {}).get("total") or 0),
        "links_total": int((link_count_row or {}).get("total") or 0),
    }
