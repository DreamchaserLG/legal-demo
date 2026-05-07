import hashlib
import html
import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

from sqlalchemy import text

from app.core.database import engine

_COMMON_MOJIBAKE_REPLACEMENTS = {
    "â€™": "’",
    "â€œ": "“",
    "â€\x9d": "”",
    "â€“": "–",
    "â€”": "—",
    "â€¦": "…",
    "Â·": "·",
    "Â§": "§",
    "Â": "",
    "鈥檚": "’s",
    "鈥檛": "’t",
    "鈥檇": "’d",
    "鈥檒": "’ll",
    "鈥檓": "’m",
    "鈥檙": "’re",
    "鈥檝": "’ve",
    "鈥?": " — ",
    "鈥�": " — ",
}
_MOJIBAKE_MARKERS = (
    "\ufffd",
    "鈥",
    "â€",
    "Ã",
    "锟",
    "鍏",
    "妗",
    "璇",
    "棰",
)


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
        keyword = repair_text(item)
        if not keyword:
            continue
        lowered = keyword.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(keyword)

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


def looks_mojibake(value: str | None) -> bool:
    raw = str(value or "")
    if not raw.strip():
        return False
    marker_hits = sum(raw.count(marker) for marker in _MOJIBAKE_MARKERS)
    if marker_hits >= 2:
        return True
    if "\ufffd" in raw or "â€" in raw:
        return True
    return "鈥" in raw


def repair_text(value: str | None) -> str:
    raw = str(value or "")
    if not raw:
        return ""

    text_value = html.unescape(raw.replace("\ufeff", ""))
    text_value = text_value.replace("\r\n", "\n").replace("\r", "\n")
    text_value = re.sub(r"(?i)<br\s*/?>", "\n", text_value)

    for source, target in _COMMON_MOJIBAKE_REPLACEMENTS.items():
        text_value = text_value.replace(source, target)

    text_value = re.sub(r"\s+\n", "\n", text_value)
    text_value = re.sub(r"\n{3,}", "\n\n", text_value)
    text_value = re.sub(r"[ \t]{2,}", " ", text_value)
    return text_value.strip()


def repair_json_text(data):
    if isinstance(data, dict):
        return {key: repair_json_text(value) for key, value in data.items()}
    if isinstance(data, list):
        return [repair_json_text(item) for item in data]
    if isinstance(data, str):
        return repair_text(data)
    return data


def plain_text_preview(value: str | None) -> str:
    text_value = repair_text(value)
    if not text_value:
        return ""
    text_value = re.sub(r"<[^>]+>", " ", text_value)
    return re.sub(r"\s+", " ", text_value).strip()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_text(data) -> str:
    return json.dumps(repair_json_text(data), ensure_ascii=False, default=str)


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
                "message": repair_text(message),
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
    clean_title = repair_text(title)
    clean_summary = repair_text(summary)
    clean_raw_text = repair_text(raw_text)

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
                "title": clean_title,
                "item_url": item_url,
                "published_at": parse_dt(published_at),
                "summary": clean_summary,
                "raw_text": clean_raw_text,
                "raw_json": json_text(raw_json or {}),
            },
        ).mappings().first()
        item_id = row["id"]

    try:
        from app.service.archive_service import safe_archive_source_item_by_id

        safe_archive_source_item_by_id(item_id, archive_event="upsert")
    except Exception:
        pass

    return item_id


def replace_item_keywords(item_id: int, keywords: list[str]):
    clean_keywords = split_keywords(keywords)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM item_keywords WHERE item_id = :item_id"),
            {"item_id": item_id},
        )
        for keyword in clean_keywords:
            conn.execute(
                text(
                    """
                    INSERT INTO item_keywords (item_id, keyword)
                    VALUES (:item_id, :keyword)
                    ON CONFLICT (item_id, keyword) DO NOTHING
                    """
                ),
                {"item_id": item_id, "keyword": keyword},
            )


def keyword_score(text_value: str, keywords: list[str], weight: int) -> int:
    preview_text = plain_text_preview(text_value)
    if not preview_text:
        return 0

    lowered = preview_text.lower()
    score = 0
    for keyword in keywords:
        if keyword.lower() in lowered:
            score += weight
    return score
