import copy
import json
import re
import time

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, fetch_all
from app.service.llm_service import (
    LLMServiceError,
    create_structured_response,
    get_llm_provider,
    is_llm_configured,
)
from app.service.search_service import search_with_remote_hydration

EN_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "because", "been", "before",
    "by", "can", "could", "did", "do", "does", "for", "from", "had", "has",
    "have", "he", "her", "his", "if", "in", "into", "is", "it", "its", "may",
    "might", "of", "on", "or", "our", "should", "that", "the", "their",
    "them", "they", "this", "to", "was", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
    "about", "against", "case", "claim", "claims", "concern", "concerns",
    "dispute", "event", "events", "fact", "facts", "issue", "issues",
    "matter", "related",
}

RELIEF_MARKERS = {
    "seek", "seeks", "request", "requests", "ask", "asks", "asked", "demand",
    "demands", "claim", "claims", "claimed", "relief", "compensation", "damages",
    "injunction", "declaration", "buyout", "restitution", "specific performance",
    "请求", "要求", "主张", "申请", "索赔", "赔偿", "返还", "回购", "分割", "确认",
}

ISSUE_MARKERS = {
    "whether", "validity", "breach", "oppression", "inheritance", "liability",
    "ownership", "division", "trust", "fiduciary", "sanction",
    "是否", "效力", "责任", "继承", "侵权", "违约", "所有权", "分割", "信托", "制裁",
}

RETRYABLE_ERROR_CATEGORIES = {"timeout", "network", "invalid_json", "provider_busy"}
_ANALYSIS_CACHE: dict[tuple, tuple[float, dict]] = {}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "string"},
        "disputed_issues": {"type": "array", "items": {"type": "string"}},
        "requested_relief": {"type": "string"},
        "search_keywords": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "jurisdiction": {"type": "string"},
        "legal_topics": {"type": "array", "items": {"type": "string"}},
        "claims": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "facts",
        "disputed_issues",
        "requested_relief",
        "search_keywords",
        "summary",
        "jurisdiction",
        "legal_topics",
        "claims",
        "risk_flags",
    ],
    "additionalProperties": False,
}


def _retry_count() -> int:
    return max(1, int(getattr(settings, "llm_retry_count", 2)))


def _retry_backoff_seconds(attempt: int) -> float:
    backoff_ms = max(0, int(getattr(settings, "llm_retry_backoff_ms", 800)))
    return (backoff_ms * attempt) / 1000.0


def _cache_ttl_seconds() -> int:
    return max(0, int(getattr(settings, "cache_ttl_seconds", 300)))


def _get_cached_analysis(cache_key: tuple):
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return None
    cached = _ANALYSIS_CACHE.get(cache_key)
    if not cached:
        return None
    expires_at, payload = cached
    if expires_at <= time.time():
        _ANALYSIS_CACHE.pop(cache_key, None)
        return None
    cloned = copy.deepcopy(payload)
    cloned["analysis_cache_status"] = "hit"
    return cloned


def _set_cached_analysis(cache_key: tuple, payload: dict):
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return
    cached_payload = copy.deepcopy(payload)
    cached_payload["analysis_cache_status"] = "miss"
    _ANALYSIS_CACHE[cache_key] = (time.time() + ttl, cached_payload)


def clear_analysis_cache():
    _ANALYSIS_CACHE.clear()


def _safe_excerpt(text: str, limit: int = 140) -> str:
    raw = str(text or "").strip().replace("\n", " ")
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "..."


def extract_search_keywords(text: str, max_keywords: int = 8) -> list[str]:
    source_text = str(text or "").strip()
    if not source_text:
        return []

    english_tokens = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", source_text.lower())
    chinese_tokens = re.findall(r"[\u4e00-\u9fff]{2,}", source_text)

    english_terms = [token for token in english_tokens if token not in EN_STOPWORDS]
    phrase_terms = [
        f"{left} {right}"
        for left, right in zip(english_terms, english_terms[1:])
        if left != right
    ]

    ordered_terms = phrase_terms + english_terms + chinese_tokens

    result = []
    seen = set()
    for term in ordered_terms:
        normalized = term.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(term.strip())
        if len(result) >= max_keywords:
            break

    return result


def _split_clauses(text: str) -> list[str]:
    chunks = re.split(r"[。！？；;\n]+|(?<=[.!?])\s+", str(text or "").strip())
    return [chunk.strip(" ,;，；") for chunk in chunks if chunk.strip(" ,;，；")]


def _contains_marker(text: str, markers: set[str]) -> bool:
    raw_text = str(text or "")
    lowered = raw_text.lower()
    return any(marker in raw_text or marker in lowered for marker in markers)


def _join_clauses(clauses: list[str], limit: int | None = None) -> str:
    values = [str(item or "").strip() for item in clauses if str(item or "").strip()]
    if limit:
        values = values[:limit]
    return "; ".join(values)


def _build_fallback_relief(clauses: list[str]) -> str:
    relief_lines = [clause for clause in clauses if _contains_marker(clause, RELIEF_MARKERS)]
    return _join_clauses(relief_lines, limit=2)


def _build_fallback_issues(clauses: list[str], keywords: list[str]) -> list[str]:
    issues = [clause for clause in clauses if _contains_marker(clause, ISSUE_MARKERS)]
    if issues:
        return issues[:4]
    return keywords[:4]


def _normalize_string_list(values, limit: int | None = None) -> list[str]:
    normalized = []
    seen = set()
    for value in values or []:
        text_value = str(value or "").strip()
        key = text_value.lower()
        if not text_value or key in seen:
            continue
        seen.add(key)
        normalized.append(text_value)
        if limit and len(normalized) >= limit:
            break
    return normalized


def build_intake_outline(analysis: dict) -> dict:
    facts = str(analysis.get("facts") or analysis.get("summary") or "").strip()
    issues = _normalize_string_list(
        analysis.get("disputed_issues") or analysis.get("claims") or analysis.get("legal_topics"),
        limit=6,
    )
    keywords = _normalize_string_list(analysis.get("search_keywords"), limit=8)

    return {
        "facts": facts,
        "disputed_issues": issues,
        "requested_relief": str(analysis.get("requested_relief") or "").strip(),
        "keywords": keywords,
    }


def build_local_analysis(text: str) -> dict:
    cleaned = str(text or "").strip()
    keywords = extract_search_keywords(cleaned)
    clauses = _split_clauses(cleaned)
    requested_relief = _build_fallback_relief(clauses)

    relief_parts = {part.strip() for part in requested_relief.split(";") if part.strip()}
    fact_clauses = [clause for clause in clauses if clause not in relief_parts]
    facts = _join_clauses(fact_clauses, limit=3) or cleaned[:240]
    disputed_issues = _build_fallback_issues(clauses, keywords)

    return {
        "facts": facts or cleaned[:240],
        "disputed_issues": disputed_issues,
        "requested_relief": requested_relief,
        "search_keywords": keywords,
        "summary": cleaned[:240],
        "jurisdiction": "",
        "legal_topics": keywords[:4],
        "claims": disputed_issues[:4],
        "risk_flags": [],
    }


def _classify_model_error(message: str) -> str:
    lowered = str(message or "").lower()
    if any(token in lowered for token in ["timed out", "timeout", "time out"]):
        return "timeout"
    if any(token in lowered for token in ["connect failed", "receive failed", "connection", "refused", "reset"]):
        return "network"
    if "invalid json" in lowered:
        return "invalid_json"
    if any(token in lowered for token in ["429", "too many requests", "busy", "rate limit"]):
        return "provider_busy"
    if any(token in lowered for token in ["credential", "api key", "api secret", "appid", "not configured"]):
        return "configuration"
    return "provider_error"


def _call_analysis_model(cleaned: str, instructions: str) -> dict:
    attempt_log = []
    last_error = ""
    last_category = ""

    for attempt in range(1, _retry_count() + 1):
        started = time.monotonic()
        try:
            response = create_structured_response(
                schema_name="legal_event_analysis",
                schema=ANALYSIS_SCHEMA,
                instructions=instructions,
                user_input=cleaned,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            attempt_log.append(
                {
                    "stage": "analysis",
                    "attempt": attempt,
                    "status": "success",
                    "duration_ms": duration_ms,
                    "model": response.get("model", ""),
                    "response_id": response.get("response_id", ""),
                    "error_category": "",
                    "error_message": "",
                }
            )
            return {
                "ok": True,
                "response": response,
                "attempt_log": attempt_log,
                "error_message": "",
                "error_category": "",
            }
        except LLMServiceError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            error_message = str(exc).strip()
            error_category = _classify_model_error(error_message)
            retryable = attempt < _retry_count() and error_category in RETRYABLE_ERROR_CATEGORIES
            attempt_log.append(
                {
                    "stage": "analysis",
                    "attempt": attempt,
                    "status": "retrying" if retryable else "failed",
                    "duration_ms": duration_ms,
                    "model": "",
                    "response_id": "",
                    "error_category": error_category,
                    "error_message": error_message,
                }
            )
            last_error = error_message
            last_category = error_category
            if retryable:
                time.sleep(_retry_backoff_seconds(attempt))
                continue
            break

    return {
        "ok": False,
        "response": None,
        "attempt_log": attempt_log,
        "error_message": last_error,
        "error_category": last_category,
    }


def _history_similarity_threshold() -> float:
    return 0.3


def _fetch_history_candidates(text_input: str, limit: int = 4) -> list[dict]:
    cleaned = str(text_input or "").strip()
    if not cleaned:
        return []
    sql = """
    SELECT
        id,
        input_text,
        source_filter,
        sort_mode,
        extracted_keywords,
        structured_analysis,
        result_count,
        created_at,
        similarity(LOWER(input_text), LOWER(:input_text)) AS similarity_score
    FROM agent_runs
    WHERE LOWER(input_text) = LOWER(:input_text)
       OR similarity(LOWER(input_text), LOWER(:input_text)) >= :threshold
    ORDER BY
        CASE WHEN LOWER(input_text) = LOWER(:input_text) THEN 1 ELSE 0 END DESC,
        similarity_score DESC,
        created_at DESC
    LIMIT :limit
    """
    try:
        return fetch_all(
            sql,
            {
                "input_text": cleaned,
                "threshold": _history_similarity_threshold(),
                "limit": max(1, int(limit)),
            },
        )
    except Exception:
        return []


def _build_history_matches(text_input: str, rows: list[dict]) -> list[dict]:
    cleaned = str(text_input or "").strip().lower()
    matches = []
    for row in rows:
        structured = row.get("structured_analysis") or {}
        analysis = structured.get("analysis") or {}
        intake_outline = structured.get("intake_outline") or {}
        similarity = max(0.0, min(float(row.get("similarity_score") or 0.0), 1.0))
        keywords = _normalize_string_list(
            row.get("extracted_keywords") or intake_outline.get("keywords") or analysis.get("search_keywords"),
            limit=6,
        )
        matches.append(
            {
                "run_id": row.get("id"),
                "is_exact": str(row.get("input_text") or "").strip().lower() == cleaned,
                "similarity": round(similarity, 2),
                "source_filter": row.get("source_filter", "all"),
                "sort_mode": row.get("sort_mode", "relevance"),
                "result_count": int(row.get("result_count") or 0),
                "created_at": str(row.get("created_at") or "")[:19],
                "input_excerpt": _safe_excerpt(row.get("input_text")),
                "summary": _safe_excerpt(
                    analysis.get("summary")
                    or intake_outline.get("facts")
                    or row.get("input_text")
                ),
                "keywords": keywords,
            }
        )
    return matches


def _reuse_structured_analysis_from_history(text_input: str, rows: list[dict]) -> dict | None:
    cleaned = str(text_input or "").strip().lower()
    for row in rows:
        if str(row.get("input_text") or "").strip().lower() != cleaned:
            continue
        structured = row.get("structured_analysis") or {}
        analysis = structured.get("analysis") or {}
        if not analysis:
            continue
        intake_outline = structured.get("intake_outline") or build_intake_outline(analysis)
        return {
            "analysis": analysis,
            "intake_outline": intake_outline,
            "analysis_mode": "history_reuse",
            "analysis_error": "",
            "analysis_error_category": "",
            "analysis_attempt_log": [],
            "llm_configured": is_llm_configured(),
            "llm_model": "",
            "llm_response_id": "",
            "history_reused": True,
            "history_match_id": row.get("id"),
            "history_similarity": 1.0,
        }
    return None


def _persist_analysis_run(payload: dict):
    structured_analysis = {
        "analysis": payload.get("analysis", {}),
        "intake_outline": payload.get("intake_outline", {}),
        "analysis_mode": payload.get("analysis_mode", ""),
        "analysis_error": payload.get("analysis_error", ""),
        "analysis_error_category": payload.get("analysis_error_category", ""),
        "analysis_attempt_log": payload.get("analysis_attempt_log", []),
        "coverage_note": payload.get("coverage_note", ""),
        "history_matches": payload.get("history_matches", []),
    }

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                        input_text,
                        source_filter,
                        sort_mode,
                        extracted_keywords,
                        structured_analysis,
                        result_count
                    )
                    VALUES (
                        :input_text,
                        :source_filter,
                        :sort_mode,
                        :extracted_keywords,
                        CAST(:structured_analysis AS jsonb),
                        :result_count
                    )
                    """
                ),
                {
                    "input_text": payload.get("input_text", ""),
                    "source_filter": payload.get("source", "all"),
                    "sort_mode": payload.get("sort", "relevance"),
                    "extracted_keywords": payload.get("extracted_keywords", []),
                    "structured_analysis": json.dumps(structured_analysis, ensure_ascii=False),
                    "result_count": int(payload.get("total") or 0),
                },
            )
    except Exception:
        return


def build_structured_analysis(text: str) -> dict:
    cleaned = str(text or "").strip()
    fallback = build_local_analysis(cleaned)

    if not cleaned:
        analysis = fallback
        return {
            "analysis": analysis,
            "intake_outline": build_intake_outline(analysis),
            "analysis_mode": "empty",
            "analysis_error": "",
            "analysis_error_category": "",
            "analysis_attempt_log": [],
            "llm_configured": is_llm_configured(),
            "llm_model": "",
            "llm_response_id": "",
        }

    if not is_llm_configured():
        analysis = fallback
        return {
            "analysis": analysis,
            "intake_outline": build_intake_outline(analysis),
            "analysis_mode": "heuristic",
            "analysis_error": f"LLM credentials are not configured for provider={get_llm_provider()}.",
            "analysis_error_category": "configuration",
            "analysis_attempt_log": [],
            "llm_configured": False,
            "llm_model": "",
            "llm_response_id": "",
        }

    instructions = (
        "You are a legal intake analyst. Split the user's event into four core parts before retrieval: "
        "facts, disputed issues, requested relief, and 4 to 8 search keywords. "
        "Also provide a concise summary, likely jurisdiction, legal topics, claims, and risk flags. "
        "Keep close to the user's facts. Do not invent case citations, statutes, or outcomes."
    )

    model_call = _call_analysis_model(cleaned, instructions)
    if not model_call["ok"]:
        analysis = fallback
        return {
            "analysis": analysis,
            "intake_outline": build_intake_outline(analysis),
            "analysis_mode": "heuristic_fallback",
            "analysis_error": model_call["error_message"],
            "analysis_error_category": model_call["error_category"],
            "analysis_attempt_log": model_call["attempt_log"],
            "llm_configured": True,
            "llm_model": "",
            "llm_response_id": "",
        }

    response = model_call["response"]
    analysis = dict(response["data"])
    analysis["search_keywords"] = _normalize_string_list(analysis.get("search_keywords"), limit=8) or fallback["search_keywords"]
    analysis["disputed_issues"] = _normalize_string_list(analysis.get("disputed_issues"), limit=6) or fallback["disputed_issues"]
    analysis["legal_topics"] = _normalize_string_list(analysis.get("legal_topics"), limit=6) or fallback["legal_topics"]
    analysis["claims"] = _normalize_string_list(analysis.get("claims"), limit=6) or analysis["disputed_issues"]
    analysis["risk_flags"] = _normalize_string_list(analysis.get("risk_flags"), limit=6)
    analysis["facts"] = str(analysis.get("facts") or analysis.get("summary") or fallback["facts"]).strip()
    analysis["requested_relief"] = str(analysis.get("requested_relief") or fallback["requested_relief"]).strip()
    analysis["summary"] = str(analysis.get("summary") or analysis["facts"][:240]).strip()
    analysis["jurisdiction"] = str(analysis.get("jurisdiction") or "").strip()

    return {
        "analysis": analysis,
        "intake_outline": build_intake_outline(analysis),
        "analysis_mode": "model",
        "analysis_error": "",
        "analysis_error_category": "",
        "analysis_attempt_log": model_call["attempt_log"],
        "llm_configured": True,
        "llm_model": response.get("model", ""),
        "llm_response_id": response.get("response_id", ""),
    }


def analyze_sentence_search(
    text: str,
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    refresh: bool = False,
    origin_page: str = "analyze",
):
    cache_key = ("analyze", str(text or "").strip(), int(limit), int(offset), str(source or "all"), str(sort or "relevance"))
    if not refresh:
        cached = _get_cached_analysis(cache_key)
        if cached is not None:
            return cached

    history_rows = _fetch_history_candidates(text)
    history_matches = _build_history_matches(text, history_rows)
    structured = _reuse_structured_analysis_from_history(text, history_rows) or build_structured_analysis(text)
    analysis = structured["analysis"]
    intake_outline = structured["intake_outline"]
    extracted_keywords = intake_outline.get("keywords", [])

    result = search_with_remote_hydration(
        extracted_keywords,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh=refresh,
        origin_page=origin_page,
    )

    coverage_note = ""
    if not result["total"]:
        coverage_note = (
            "当前本地库没有命中提取出的关键词。系统会优先返回现有结果；如果可扩库，会在后台继续补抓并回写本地。"
        )

    response_payload = {
        "input_text": str(text or "").strip(),
        "analysis": analysis,
        "intake_outline": intake_outline,
        "analysis_mode": structured.get("analysis_mode", "heuristic"),
        "analysis_error": structured.get("analysis_error", ""),
        "analysis_error_category": structured.get("analysis_error_category", ""),
        "analysis_attempt_log": structured.get("analysis_attempt_log", []),
        "llm_configured": structured.get("llm_configured", False),
        "llm_model": structured.get("llm_model", ""),
        "llm_response_id": structured.get("llm_response_id", ""),
        "history_reused": structured.get("history_reused", False),
        "history_match_id": structured.get("history_match_id"),
        "history_similarity": structured.get("history_similarity", 0),
        "history_matches": history_matches,
        "extracted_keywords": extracted_keywords,
        "coverage_note": coverage_note,
        "analysis_cache_status": "miss",
        **result,
    }

    if not structured.get("history_reused") and structured.get("analysis_mode") != "heuristic_fallback":
        _persist_analysis_run(response_payload)
    _set_cached_analysis(cache_key, response_payload)
    return response_payload
