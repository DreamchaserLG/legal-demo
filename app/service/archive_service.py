import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine


def _archive_enabled() -> bool:
    return bool(getattr(settings, "local_archive_enabled", True))


def _archive_root() -> Path:
    return Path(getattr(settings, "local_archive_dir", "data_archive")).resolve()


def _exports_root() -> Path:
    export_dir = getattr(settings, "local_archive_export_dir", "data_archive/exports")
    return Path(export_dir).resolve()


def ensure_archive_directories():
    if not _archive_enabled():
        return
    (_archive_root() / "source_items").mkdir(parents=True, exist_ok=True)
    _exports_root().mkdir(parents=True, exist_ok=True)


def _safe_segment(value: str, fallback: str, limit: int = 96) -> str:
    raw = re.sub(r"\s+", "-", str(value or "").strip())
    raw = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff.]+", "-", raw).strip(" .-_")
    if not raw:
        raw = fallback
    if len(raw) > limit:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        raw = f"{raw[:limit - 13].rstrip(' .-_')}-{digest}"
    return raw


def _item_sql(where_clause: str = "WHERE si.id = :item_id") -> str:
    return f"""
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
        COALESCE(
            array_remove(array_agg(DISTINCT ik.keyword ORDER BY ik.keyword), NULL),
            ARRAY[]::TEXT[]
        ) AS keywords
    FROM source_items si
    LEFT JOIN item_keywords ik ON ik.item_id = si.id
    {where_clause}
    GROUP BY si.id
    ORDER BY si.updated_at DESC, si.id DESC
    """


def _serialize_row(row: dict, archive_event: str = "snapshot") -> dict:
    return {
        "archive_version": 1,
        "archive_event": archive_event,
        "archived_at": datetime.utcnow().isoformat(),
        "db_item_id": row.get("id"),
        "source_code": row.get("source_code"),
        "source_uid": row.get("source_uid"),
        "title": row.get("title"),
        "item_url": row.get("item_url"),
        "published_at": row.get("published_at").isoformat() if row.get("published_at") else None,
        "summary": row.get("summary"),
        "raw_text": row.get("raw_text"),
        "keywords": list(row.get("keywords") or []),
        "raw_json": row.get("raw_json") or {},
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


def _archive_item_path(source_code: str, source_uid: str) -> Path:
    source_dir = _archive_root() / "source_items" / _safe_segment(source_code, "other")
    source_dir.mkdir(parents=True, exist_ok=True)
    uid_segment = _safe_segment(
        source_uid,
        hashlib.sha1(str(source_uid or "").encode("utf-8")).hexdigest()[:16] or "item",
        limit=120,
    )
    return source_dir / f"{uid_segment}.json"


def _archive_item_files(source_filter: str = "all") -> list[Path]:
    root = _archive_root() / "source_items"
    if not root.exists():
        return []
    source_filter = str(source_filter or "all").strip().lower()
    if source_filter == "all":
        return sorted(root.rglob("*.json"))
    target = root / _safe_segment(source_filter, source_filter)
    if not target.exists():
        return []
    return sorted(target.rglob("*.json"))


def _archive_source_counts() -> dict[str, int]:
    counts = Counter()
    for path in _archive_item_files("all"):
        counts[path.parent.name] += 1
    return dict(counts)


def _fetch_item_by_id(item_id: int) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text(_item_sql()), {"item_id": int(item_id)}).mappings().first()
        return dict(row) if row else None


def archive_source_item_by_id(item_id: int, archive_event: str = "upsert") -> dict | None:
    if not _archive_enabled():
        return None
    ensure_archive_directories()
    row = _fetch_item_by_id(item_id)
    if not row:
        return None
    payload = _serialize_row(row, archive_event=archive_event)
    target_path = _archive_item_path(row["source_code"], row["source_uid"])
    target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "item_id": row["id"],
        "source_code": row["source_code"],
        "source_uid": row["source_uid"],
        "path": str(target_path),
    }


def safe_archive_source_item_by_id(item_id: int, archive_event: str = "upsert") -> dict | None:
    try:
        return archive_source_item_by_id(item_id, archive_event=archive_event)
    except Exception:
        return None


def _fetch_all_rows(source_filter: str = "all") -> list[dict]:
    source_filter = str(source_filter or "all").strip().lower()
    if source_filter and source_filter != "all":
        where_clause = "WHERE si.source_code = :source_code"
        params = {"source_code": source_filter}
    else:
        where_clause = ""
        params = {}
    with engine.connect() as conn:
        rows = conn.execute(text(_item_sql(where_clause)), params).mappings().all()
        return [dict(row) for row in rows]


def rebuild_local_archive_from_db(source_filter: str = "all") -> dict:
    if not _archive_enabled():
        return {
            "archive_enabled": False,
            "source_filter": source_filter,
            "items_archived": 0,
            "snapshot": None,
        }
    ensure_archive_directories()
    rows = _fetch_all_rows(source_filter=source_filter)
    for row in rows:
        payload = _serialize_row(row, archive_event="rebuild")
        target_path = _archive_item_path(row["source_code"], row["source_uid"])
        target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot = export_source_items_snapshot(source_filter=source_filter)
    return {
        "archive_enabled": True,
        "source_filter": source_filter,
        "items_archived": len(rows),
        "snapshot": snapshot,
    }


def export_source_items_snapshot(source_filter: str = "all") -> dict:
    if not _archive_enabled():
        return {"archive_enabled": False, "source_filter": source_filter, "items_exported": 0}
    ensure_archive_directories()
    rows = _fetch_all_rows(source_filter=source_filter)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    source_slug = _safe_segment(source_filter or "all", "all", limit=24)
    target_path = _exports_root() / f"source-items-{source_slug}-{timestamp}.jsonl"
    with target_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_serialize_row(row, archive_event="export"), ensure_ascii=False))
            handle.write("\n")
    return {
        "archive_enabled": True,
        "source_filter": source_filter,
        "items_exported": len(rows),
        "path": str(target_path),
    }


def get_archive_status() -> dict:
    ensure_archive_directories()
    item_files = list((_archive_root() / "source_items").rglob("*.json")) if _archive_enabled() else []
    export_files = list(_exports_root().glob("*.jsonl")) if _archive_enabled() else []
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    COUNT(*) AS total_items,
                    COUNT(*) FILTER (WHERE source_code = 'canlii') AS canlii_items,
                    COUNT(*) FILTER (WHERE source_code = 'ofac') AS ofac_items
                FROM source_items
                """
            )
        ).mappings().first()
    return {
        "archive_enabled": _archive_enabled(),
        "archive_root": str(_archive_root()),
        "export_root": str(_exports_root()),
        "database_counts": dict(row or {}),
        "archived_item_files": len(item_files),
        "snapshot_files": len(export_files),
        "latest_snapshot": str(max(export_files, key=lambda item: item.stat().st_mtime)) if export_files else "",
    }


def restore_source_items_from_archive(source_filter: str = "all", limit: int | None = None) -> dict:
    if not _archive_enabled():
        return {
            "archive_enabled": False,
            "source_filter": source_filter,
            "restored_items": 0,
        }

    ensure_archive_directories()
    item_files = _archive_item_files(source_filter=source_filter)
    if limit and limit > 0:
        item_files = item_files[: int(limit)]

    from app.service.common_service import replace_item_keywords, upsert_source_item

    restored = 0
    errors = []
    for path in item_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            item_id = upsert_source_item(
                source_code=str(payload.get("source_code") or path.parent.name),
                source_uid=str(payload.get("source_uid") or path.stem),
                title=str(payload.get("title") or path.stem),
                item_url=payload.get("item_url"),
                published_at=payload.get("published_at"),
                summary=payload.get("summary"),
                raw_text=payload.get("raw_text"),
                raw_json=payload.get("raw_json") or {},
            )
            replace_item_keywords(item_id, list(payload.get("keywords") or []))
            restored += 1
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")

    return {
        "archive_enabled": True,
        "source_filter": source_filter,
        "restored_items": restored,
        "errors": errors[:10],
    }


def bootstrap_database_from_archive() -> dict:
    if not _archive_enabled():
        return {"archive_enabled": False, "bootstrapped": False, "reason": "archive_disabled"}

    ensure_archive_directories()
    archive_counts = _archive_source_counts()
    if not archive_counts:
        return {"archive_enabled": True, "bootstrapped": False, "reason": "no_archive_files"}

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT source_code, COUNT(*) AS total
                FROM source_items
                GROUP BY source_code
                """
            )
        ).mappings().all()
    db_counts = {str(row.get("source_code") or ""): int(row.get("total") or 0) for row in rows}

    needs_restore = any(int(db_counts.get(source_code, 0)) < count for source_code, count in archive_counts.items())
    if not needs_restore:
        return {
            "archive_enabled": True,
            "bootstrapped": False,
            "reason": "database_already_covers_archive",
            "archive_counts": archive_counts,
            "database_counts": db_counts,
        }

    result = restore_source_items_from_archive("all")
    result.update(
        {
            "bootstrapped": True,
            "reason": "archive_restored",
            "archive_counts": archive_counts,
            "database_counts": db_counts,
        }
    )
    return result
