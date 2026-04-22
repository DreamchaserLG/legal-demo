import os
import socket
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import engine
from app.service.canlii_service import sync_canlii_by_keywords, sync_canlii_demo
from app.service.common_service import json_text, sha256_text, split_keywords
from app.service.ofac_service import sync_ofac_by_keywords, sync_ofac_demo

ACTIVE_TASK_STATUSES = {"queued", "running"}
TERMINAL_TASK_STATUSES = {"completed", "failed"}
VALID_SOURCES = {"all", "canlii", "ofac"}


def _table_statements() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS ingestion_tasks (
            id BIGSERIAL PRIMARY KEY,
            fingerprint VARCHAR(128) NOT NULL,
            query_text TEXT NOT NULL,
            normalized_keywords TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
            source_filter VARCHAR(50) NOT NULL DEFAULT 'all',
            origin_page VARCHAR(30) NOT NULL DEFAULT 'search',
            desired_count INTEGER NOT NULL DEFAULT 0,
            current_local_count INTEGER NOT NULL DEFAULT 0,
            result_total_after INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            items_processed INTEGER NOT NULL DEFAULT 0,
            claimed_by VARCHAR(120),
            sources_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            message TEXT,
            last_error TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL,
            last_heartbeat TIMESTAMP NULL
        )
        """,
        """
        ALTER TABLE ingestion_tasks
        ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(120)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_ingestion_tasks_status_created
        ON ingestion_tasks (status, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_ingestion_tasks_fingerprint
        ON ingestion_tasks (fingerprint)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_ingestion_tasks_claimed_by
        ON ingestion_tasks (claimed_by)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestion_tasks_active_fingerprint
        ON ingestion_tasks (fingerprint)
        WHERE status IN ('queued', 'running')
        """,
    ]


def ensure_ingestion_tables():
    with engine.begin() as conn:
        for statement in _table_statements():
            conn.execute(text(statement))


def _normalize_source(source_filter: str) -> str:
    value = str(source_filter or "all").strip().lower()
    return value if value in VALID_SOURCES else "all"


def _normalize_keywords(keywords) -> list[str]:
    normalized = []
    seen = set()
    for keyword in split_keywords(keywords):
        clean = str(keyword or "").strip()
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        normalized.append(clean)
    return normalized


def _task_fingerprint(keywords: list[str], source_filter: str) -> str:
    fingerprint_input = "|".join(sorted(keyword.lower() for keyword in keywords))
    return sha256_text(f"{_normalize_source(source_filter)}::{fingerprint_input}")


def _similarity_threshold(keyword: str) -> float:
    size = len((keyword or "").strip())
    if size <= 4:
        return 0.95
    if size <= 7:
        return 0.75
    return 0.55


def _count_local_matches(keywords: list[str], source_filter: str) -> int:
    if not keywords:
        return 0

    conditions = []
    params = {}
    for index, keyword in enumerate(keywords):
        key = f"kw{index}"
        exact_key = f"kw_exact{index}"
        sim_key = f"kw_sim{index}"
        params[key] = f"%{keyword.lower()}%"
        params[exact_key] = keyword.lower()
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

    source_clause = ""
    normalized_source = _normalize_source(source_filter)
    if normalized_source in {"canlii", "ofac"}:
        source_clause = "AND si.source_code = :source"
        params["source"] = normalized_source

    sql = f"""
    SELECT COUNT(*) AS total
    FROM source_items si
    WHERE ({' OR '.join(conditions)}) {source_clause}
    """
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).mappings().first()
    return int(row["total"] or 0) if row else 0


def _status_label(status: str) -> str:
    mapping = {
        "queued": "已排队",
        "running": "抓取中",
        "completed": "已完成",
        "failed": "失败",
    }
    return mapping.get(str(status or "").strip().lower(), str(status or "-"))


def _serialize_task(row: dict | None) -> dict | None:
    if not row:
        return None
    payload = dict(row)
    payload["normalized_keywords"] = list(payload.get("normalized_keywords") or [])
    payload["sources"] = payload.get("sources_json") or {}
    payload["status_label"] = _status_label(payload.get("status"))
    payload["is_terminal"] = payload.get("status") in TERMINAL_TASK_STATUSES
    payload["should_refresh"] = (
        payload.get("status") == "completed" and int(payload.get("items_processed") or 0) > 0
    )
    return payload


def _get_task_row_by_id(conn, task_id: int):
    return conn.execute(
        text("SELECT * FROM ingestion_tasks WHERE id = :task_id"),
        {"task_id": task_id},
    ).mappings().first()


def _find_active_task(conn, fingerprint: str):
    return conn.execute(
        text(
            """
            SELECT *
            FROM ingestion_tasks
            WHERE fingerprint = :fingerprint
              AND status IN ('queued', 'running')
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"fingerprint": fingerprint},
    ).mappings().first()


def _find_recent_terminal_task(conn, fingerprint: str, desired_count: int):
    cooldown_seconds = max(0, int(getattr(settings, "ingestion_task_requeue_cooldown_seconds", 900)))
    if cooldown_seconds <= 0:
        return None

    cutoff = datetime.utcnow() - timedelta(seconds=cooldown_seconds)
    row = conn.execute(
        text(
            """
            SELECT *
            FROM ingestion_tasks
            WHERE fingerprint = :fingerprint
              AND status IN ('completed', 'failed')
              AND COALESCE(finished_at, created_at) >= :cutoff
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"fingerprint": fingerprint, "cutoff": cutoff},
    ).mappings().first()

    if not row:
        return None
    if int(desired_count or 0) <= int(row.get("desired_count") or 0):
        return row
    return None


def enqueue_or_reuse_hydration_task(
    *,
    query_text: str,
    keywords,
    source_filter: str,
    desired_count: int,
    current_local_count: int,
    origin_page: str,
) -> dict | None:
    if not getattr(settings, "remote_search_enabled", True):
        return None
    if not getattr(settings, "async_hydration_enabled", True):
        return None

    normalized_keywords = _normalize_keywords(keywords)
    if not normalized_keywords:
        return None

    source_filter = _normalize_source(source_filter)
    desired_count = max(1, int(desired_count or 0))
    current_local_count = max(0, int(current_local_count or 0))
    fingerprint = _task_fingerprint(normalized_keywords, source_filter)
    task_message = f"本地命中 {current_local_count} 条，低于目标 {desired_count} 条，已提交后台扩库任务。"

    with engine.begin() as conn:
        existing = _find_active_task(conn, fingerprint)
        if existing:
            conn.execute(
                text(
                    """
                    UPDATE ingestion_tasks
                    SET desired_count = GREATEST(desired_count, :desired_count),
                        current_local_count = :current_local_count,
                        message = :message
                    WHERE id = :task_id
                    """
                ),
                {
                    "task_id": existing["id"],
                    "desired_count": desired_count,
                    "current_local_count": current_local_count,
                    "message": task_message,
                },
            )
            return _serialize_task(_get_task_row_by_id(conn, existing["id"]))

        recent_terminal = _find_recent_terminal_task(conn, fingerprint, desired_count)
        if recent_terminal:
            return _serialize_task(recent_terminal)

        try:
            row = conn.execute(
                text(
                    """
                    INSERT INTO ingestion_tasks (
                        fingerprint,
                        query_text,
                        normalized_keywords,
                        source_filter,
                        origin_page,
                        desired_count,
                        current_local_count,
                        message,
                        status
                    )
                    VALUES (
                        :fingerprint,
                        :query_text,
                        :normalized_keywords,
                        :source_filter,
                        :origin_page,
                        :desired_count,
                        :current_local_count,
                        :message,
                        'queued'
                    )
                    RETURNING *
                    """
                ),
                {
                    "fingerprint": fingerprint,
                    "query_text": str(query_text or "").strip() or ", ".join(normalized_keywords),
                    "normalized_keywords": normalized_keywords,
                    "source_filter": source_filter,
                    "origin_page": str(origin_page or "search").strip().lower() or "search",
                    "desired_count": desired_count,
                    "current_local_count": current_local_count,
                    "message": task_message,
                },
            ).mappings().first()
            return _serialize_task(row)
        except IntegrityError:
            existing = _find_active_task(conn, fingerprint)
            return _serialize_task(existing)


def get_ingestion_task(task_id: int) -> dict | None:
    ensure_ingestion_tables()
    with engine.connect() as conn:
        row = _get_task_row_by_id(conn, task_id)
    return _serialize_task(row)


def _worker_identity() -> str:
    configured = str(getattr(settings, "ingestion_worker_name", "") or "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}"


def _requeue_stale_running_tasks(conn):
    stale_seconds = max(60, int(getattr(settings, "ingestion_worker_stale_seconds", 600)))
    cutoff = datetime.utcnow() - timedelta(seconds=stale_seconds)
    conn.execute(
        text(
            """
            UPDATE ingestion_tasks
            SET status = 'queued',
                claimed_by = NULL,
                message = '检测到上一个 worker 心跳超时，任务已重新排队。',
                last_error = COALESCE(last_error, 'stale_worker_requeue')
            WHERE status = 'running'
              AND COALESCE(last_heartbeat, started_at, created_at) < :cutoff
            """
        ),
        {"cutoff": cutoff},
    )


def _claim_next_task(worker_name: str):
    with engine.begin() as conn:
        _requeue_stale_running_tasks(conn)
        row = conn.execute(
            text(
                """
                SELECT *
                FROM ingestion_tasks
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
        ).mappings().first()
        if not row:
            return None

        conn.execute(
            text(
                """
                UPDATE ingestion_tasks
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    last_heartbeat = CURRENT_TIMESTAMP,
                    claimed_by = :claimed_by,
                    message = :message
                WHERE id = :task_id
                """
            ),
            {
                "task_id": row["id"],
                "claimed_by": worker_name,
                "message": f"后台扩库已开始，当前 worker={worker_name}。",
            },
        )
        return _serialize_task(_get_task_row_by_id(conn, row["id"]))


def _update_task_progress(task_id: int, *, worker_name: str, message: str, items_processed: int, sources: dict):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE ingestion_tasks
                SET items_processed = :items_processed,
                    sources_json = CAST(:sources_json AS jsonb),
                    message = :message,
                    claimed_by = :claimed_by,
                    last_heartbeat = CURRENT_TIMESTAMP
                WHERE id = :task_id
                """
            ),
            {
                "task_id": task_id,
                "items_processed": int(items_processed or 0),
                "sources_json": json_text(sources or {}),
                "message": message,
                "claimed_by": worker_name,
            },
        )


def _complete_task(
    task_id: int,
    *,
    worker_name: str,
    message: str,
    items_processed: int,
    result_total_after: int,
    sources: dict,
):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE ingestion_tasks
                SET status = 'completed',
                    items_processed = :items_processed,
                    result_total_after = :result_total_after,
                    claimed_by = :claimed_by,
                    sources_json = CAST(:sources_json AS jsonb),
                    message = :message,
                    last_error = NULL,
                    finished_at = CURRENT_TIMESTAMP,
                    last_heartbeat = CURRENT_TIMESTAMP
                WHERE id = :task_id
                """
            ),
            {
                "task_id": task_id,
                "items_processed": int(items_processed or 0),
                "result_total_after": int(result_total_after or 0),
                "claimed_by": worker_name,
                "sources_json": json_text(sources or {}),
                "message": message,
            },
        )


def _fail_task(task_id: int, *, worker_name: str, message: str, error: str, sources: dict | None = None):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE ingestion_tasks
                SET status = 'failed',
                    claimed_by = :claimed_by,
                    message = :message,
                    last_error = :last_error,
                    sources_json = CAST(:sources_json AS jsonb),
                    finished_at = CURRENT_TIMESTAMP,
                    last_heartbeat = CURRENT_TIMESTAMP
                WHERE id = :task_id
                """
            ),
            {
                "task_id": task_id,
                "claimed_by": worker_name,
                "message": message,
                "last_error": str(error or "").strip(),
                "sources_json": json_text(sources or {}),
            },
        )


def _merge_remote_messages(source_results: list[dict]) -> str:
    messages = [
        str(item.get("message") or "").strip()
        for item in source_results
        if str(item.get("message") or "").strip()
    ]
    return "；".join(messages)


def _source_stage_map(source_results: list[dict]) -> dict:
    mapped = {}
    for index, item in enumerate(source_results):
        source_name = str(item.get("source") or f"source_{index}").strip().lower()
        stage = str(item.get("stage") or "hydrate").strip().lower()
        mapped[f"{source_name}_{stage}"] = item
    return mapped


def _clear_runtime_caches():
    try:
        from app.service.analysis_service import clear_analysis_cache
        from app.service.agent_service import clear_prediction_cache
        from app.service.search_service import clear_search_cache

        clear_search_cache()
        clear_analysis_cache()
        clear_prediction_cache()
    except Exception:
        return


def _run_task(task: dict, worker_name: str):
    keywords = task.get("normalized_keywords") or _normalize_keywords(task.get("query_text"))
    source_filter = _normalize_source(task.get("source_filter"))
    desired_count = max(1, int(task.get("desired_count") or 0))
    current_local_count = _count_local_matches(keywords, source_filter)
    deficit = max(0, desired_count - current_local_count)
    source_results = []

    if source_filter in {"all", "canlii"}:
        result = sync_canlii_by_keywords(keywords, target_count=deficit or desired_count)
        result["stage"] = "keyword"
        source_results.append(result)
    if source_filter in {"all", "ofac"}:
        result = sync_ofac_by_keywords(keywords, target_count=deficit or desired_count)
        result["stage"] = "keyword"
        source_results.append(result)

    processed = sum(int(item.get("processed") or item.get("items") or 0) for item in source_results)
    sources = _source_stage_map(source_results)
    _update_task_progress(
        int(task["id"]),
        worker_name=worker_name,
        message=_merge_remote_messages(source_results) or "关键词补抓已完成，正在评估是否需要补充批量同步。",
        items_processed=processed,
        sources=sources,
    )

    total_after_keyword = _count_local_matches(keywords, source_filter)
    remaining = max(0, desired_count - total_after_keyword)
    if remaining > 0:
        bulk_results = []
        if source_filter in {"all", "canlii"}:
            result = sync_canlii_demo()
            result["stage"] = "bulk"
            bulk_results.append(result)
        if source_filter in {"all", "ofac"}:
            result = sync_ofac_demo()
            result["stage"] = "bulk"
            bulk_results.append(result)
        source_results.extend(bulk_results)
        processed += sum(int(item.get("processed") or item.get("items") or 0) for item in bulk_results)
        sources = _source_stage_map(source_results)
        _update_task_progress(
            int(task["id"]),
            worker_name=worker_name,
            message=_merge_remote_messages(source_results) or "批量同步已完成，正在统计本地命中。",
            items_processed=processed,
            sources=sources,
        )

    result_total_after = _count_local_matches(keywords, source_filter)
    if processed > 0:
        message = f"后台扩库已完成，累计处理 {processed} 条记录，本地当前可命中 {result_total_after} 条。"
    else:
        message = f"后台扩库已完成，但没有补到新的匹配记录。本地当前仍为 {result_total_after} 条。"

    _complete_task(
        int(task["id"]),
        worker_name=worker_name,
        message=message,
        items_processed=processed,
        result_total_after=result_total_after,
        sources=sources,
    )
    _clear_runtime_caches()


def process_next_ingestion_task(worker_name: str | None = None) -> bool:
    ensure_ingestion_tables()
    resolved_worker_name = str(worker_name or _worker_identity()).strip()
    task = _claim_next_task(resolved_worker_name)
    if not task:
        return False
    try:
        _run_task(task, resolved_worker_name)
    except Exception as exc:
        _fail_task(
            int(task["id"]),
            worker_name=resolved_worker_name,
            message="后台扩库失败，任务已终止。",
            error=str(exc),
        )
    return True


def run_ingestion_worker_forever(
    *,
    worker_name: str | None = None,
    stop_event: threading.Event | None = None,
    poll_seconds: float | None = None,
):
    ensure_ingestion_tables()
    if not getattr(settings, "remote_search_enabled", True):
        return
    if not getattr(settings, "async_hydration_enabled", True):
        return

    resolved_worker_name = str(worker_name or _worker_identity()).strip()
    resolved_stop_event = stop_event or threading.Event()
    sleep_seconds = max(0.5, float(poll_seconds or getattr(settings, "ingestion_worker_poll_seconds", 2)))

    while not resolved_stop_event.is_set():
        handled = process_next_ingestion_task(resolved_worker_name)
        if handled:
            continue
        resolved_stop_event.wait(sleep_seconds)
