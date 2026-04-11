import hashlib
import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

from sqlalchemy import text

from app.core.database import engine


def split_keywords(value: str | list[str]) -> list[str]:
    if value is None:
        return []

    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,;，；、\n\r\t ]+", str(value))

    result = []
    seen = set()

    for item in raw:
        kw = str(item).strip()
        if not kw:
            continue
        low = kw.lower()
        if low in seen:
            continue
        seen.add(low)
        result.append(kw)

    return result


def parse_dt(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text_value = str(value).strip()
    if not text_value:
        return None

    try:
        return parsedate_to_datetime(text_value)
    except Exception:
        pass

    candidates = [text_value.replace("Z", "+00:00")]
    if "T" in text_value:
        candidates.append(text_value.split("T")[0])

    for item in candidates:
        try:
            return datetime.fromisoformat(item)
        except Exception:
            continue

    return None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_text(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def log_sync(source_code: str, status: str, message: str):
    sql = """
    INSERT INTO sync_logs (source_code, status, message)
    VALUES (:source_code, :status, :message)
    """
    with engine.begin() as conn:
        conn.execute(
            text(sql),
            {
                "source_code": source_code,
                "status": status,
                "message": message,
            },
        )


def upsert_source_item(
    source_code: str,
    source_uid: str,
    title: str,
    item_url: str | None = None,
    published_at=None,
    summary: str | None = None,
    raw_text: str | None = None,
    raw_json: dict | list | None = None,
):
    sql = """
    INSERT INTO source_items (
        source_code, source_uid, title, item_url, published_at,
        summary, raw_text, raw_json, created_at, updated_at
    )
    VALUES (
        :source_code, :source_uid, :title, :item_url, :published_at,
        :summary, :raw_text, CAST(:raw_json AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
    )
    ON CONFLICT (source_code, source_uid)
    DO UPDATE SET
        title = EXCLUDED.title,
        item_url = EXCLUDED.item_url,
        published_at = EXCLUDED.published_at,
        summary = EXCLUDED.summary,
        raw_text = EXCLUDED.raw_text,
        raw_json = EXCLUDED.raw_json,
        updated_at = CURRENT_TIMESTAMP
    RETURNING id
    """

    with engine.begin() as conn:
        row = conn.execute(
            text(sql),
            {
                "source_code": source_code,
                "source_uid": source_uid,
                "title": title,
                "item_url": item_url,
                "published_at": parse_dt(published_at),
                "summary": summary,
                "raw_text": raw_text,
                "raw_json": json_text(raw_json or {}),
            },
        ).mappings().first()
        return row["id"]


def replace_item_keywords(item_id: int, keywords: list[str]):
    clean_keywords = split_keywords(keywords)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM item_keywords WHERE item_id = :item_id"),
            {"item_id": item_id},
        )
        for kw in clean_keywords:
            conn.execute(
                text("""
                    INSERT INTO item_keywords (item_id, keyword)
                    VALUES (:item_id, :keyword)
                    ON CONFLICT (item_id, keyword) DO NOTHING
                """),
                {"item_id": item_id, "keyword": kw},
            )


def keyword_score(text_value: str, keywords: list[str], weight: int) -> int:
    if not text_value:
        return 0

    low = text_value.lower()
    score = 0
    for kw in keywords:
        if kw.lower() in low:
            score += weight
    return score
