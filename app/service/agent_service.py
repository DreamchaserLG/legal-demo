import copy
import json
import re
import time

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, fetch_all
from app.service.analysis_service import analyze_sentence_search
from app.service.llm_service import (
    LLMServiceError,
    create_structured_response,
    get_llm_model_name,
    get_llm_provider,
    is_llm_configured,
)

PREDICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "predicted_outcome": {"type": "string"},
        "likely_prevailing_party": {"type": "string"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "key_factors": {"type": "array", "items": {"type": "string"}},
        "supporting_case_titles": {"type": "array", "items": {"type": "string"}},
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "predicted_outcome",
        "likely_prevailing_party",
        "confidence",
        "reasoning",
        "key_factors",
        "supporting_case_titles",
        "caveats",
    ],
    "additionalProperties": False,
}

CASE_COMPARISON_SCHEMA = {
    "type": "object",
    "properties": {
        "comparisons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_index": {"type": "integer"},
                    "title": {"type": "string"},
                    "similarities": {"type": "array", "items": {"type": "string"}},
                    "differences": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["source_index", "title", "similarities", "differences"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["comparisons"],
    "additionalProperties": False,
}

RETRYABLE_ERROR_CATEGORIES = {"timeout", "network", "invalid_json", "provider_busy"}
_PREDICTION_CACHE: dict[tuple, tuple[float, dict]] = {}


def _retry_count() -> int:
    return max(1, int(getattr(settings, "llm_retry_count", 2)))


def _retry_backoff_seconds(attempt: int) -> float:
    backoff_ms = max(0, int(getattr(settings, "llm_retry_backoff_ms", 800)))
    return (backoff_ms * attempt) / 1000.0


def _cache_ttl_seconds() -> int:
    return max(0, int(getattr(settings, "cache_ttl_seconds", 300)))


def _get_cached_prediction(cache_key: tuple):
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return None
    cached = _PREDICTION_CACHE.get(cache_key)
    if not cached:
        return None
    expires_at, payload = cached
    if expires_at <= time.time():
        _PREDICTION_CACHE.pop(cache_key, None)
        return None
    cloned = copy.deepcopy(payload)
    cloned["prediction_cache_status"] = "hit"
    return cloned


def _set_cached_prediction(cache_key: tuple, payload: dict):
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return
    cached_payload = copy.deepcopy(payload)
    cached_payload["prediction_cache_status"] = "miss"
    _PREDICTION_CACHE[cache_key] = (time.time() + ttl, cached_payload)


def clear_prediction_cache():
    _PREDICTION_CACHE.clear()


def _safe_excerpt(text: str, limit: int = 160) -> str:
    raw = str(text or "").strip().replace("\n", " ")
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "..."


def _tokenize_text(text: str) -> set[str]:
    english_terms = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", str(text or "").lower())
    chinese_terms = re.findall(r"[\u4e00-\u9fff]{2,}", str(text or ""))
    return {term.strip() for term in english_terms + chinese_terms if term.strip()}


def _compact_text_list(values, limit: int = 3) -> list[str]:
    merged = []
    seen = set()
    for value in values or []:
        text_value = str(value or "").strip()
        key = text_value.lower()
        if not text_value or key in seen:
            continue
        seen.add(key)
        merged.append(text_value)
        if len(merged) >= limit:
            break
    return merged


def _normalize_case_comparison_lists(similarities, differences) -> tuple[list[str], list[str]]:
    normalized_similarities = _compact_text_list(similarities, limit=3)
    normalized_differences = _compact_text_list(differences, limit=3)

    if not normalized_similarities:
        normalized_similarities = [
            "This record was retrieved for lexical similarity, but the summary is too thin for a stronger match."
        ]
    if not normalized_differences:
        normalized_differences = [
            "The precedent appears close at a high level, but the full text should still be checked for fact-level differences."
        ]

    similarity_keys = {item.lower() for item in normalized_similarities}
    filtered_differences = [item for item in normalized_differences if item.lower() not in similarity_keys]
    if not filtered_differences:
        filtered_differences = [
            "The available precedent summary is thin, so the fact-level differences still need to be verified against the full text."
        ]

    difference_keys = {item.lower() for item in filtered_differences}
    filtered_similarities = [item for item in normalized_similarities if item.lower() not in difference_keys]
    if not filtered_similarities:
        filtered_similarities = [
            "The retrieval score indicates topical overlap, but the summary does not provide enough detail for a stronger similarity statement."
        ]

    return filtered_similarities[:3], filtered_differences[:3]


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


def _invoke_model_with_retry(
    *,
    stage: str,
    schema_name: str,
    schema: dict,
    instructions: str,
    user_input: str,
    attempts: int | None = None,
) -> dict:
    max_attempts = attempts or _retry_count()
    attempt_log = []
    last_error = ""
    last_category = ""

    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        try:
            response = create_structured_response(
                schema_name=schema_name,
                schema=schema,
                instructions=instructions,
                user_input=user_input,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            attempt_log.append(
                {
                    "stage": stage,
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
            retryable = attempt < max_attempts and error_category in RETRYABLE_ERROR_CATEGORIES
            attempt_log.append(
                {
                    "stage": stage,
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


def get_dashboard_metrics() -> dict:
    try:
        rows = fetch_all(
            """
            SELECT source_code, COUNT(*) AS total
            FROM source_items
            GROUP BY source_code
            ORDER BY source_code
            """
        )
    except Exception:
        rows = []

    counts = {"ofac": 0, "canlii": 0, "other": 0}
    for row in rows:
        source_code = row.get("source_code")
        total = int(row.get("total") or 0)
        if source_code in counts:
            counts[source_code] = total
        else:
            counts["other"] += total

    return {
        "counts": counts,
        "total_items": sum(counts.values()),
        "llm_configured": is_llm_configured(),
        "llm_provider": get_llm_provider(),
        "llm_model": get_llm_model_name(),
    }


def _build_precedent_packets(rows: list[dict], limit: int = 5) -> list[dict]:
    packets = []
    for row in rows[:limit]:
        meta = row.get("raw_json") or {}
        packets.append(
            {
                "title": row.get("title", ""),
                "source_code": row.get("source_code", ""),
                "published_at": str(row.get("published_at") or ""),
                "summary": _safe_excerpt(row.get("summary") or meta.get("remarks") or row.get("raw_text")),
                "url": row.get("item_url", ""),
                "source_url": meta.get("database_page") or meta.get("source_csv_url") or "",
            }
        )
    return packets


def _build_heuristic_case_comparisons(intake_outline: dict, precedents: list[dict]) -> list[dict]:
    issue_terms = intake_outline.get("disputed_issues", []) or []
    keyword_terms = intake_outline.get("keywords", []) or []
    requested_relief = str(intake_outline.get("requested_relief") or "").strip()
    reference_terms = _compact_text_list(issue_terms + keyword_terms, limit=6)

    comparisons = []
    for precedent in precedents:
        precedent_text = " ".join([
            str(precedent.get("title") or ""),
            str(precedent.get("summary") or ""),
        ]).strip()
        precedent_tokens = _tokenize_text(precedent_text)

        matched_terms = []
        unmatched_terms = []
        for term in reference_terms:
            term_tokens = _tokenize_text(term)
            if term_tokens and term_tokens & precedent_tokens:
                matched_terms.append(term)
            else:
                unmatched_terms.append(term)

        similarities = _compact_text_list(
            [f"Shared issue or keyword: {term}" for term in matched_terms[:2]]
            + ([f"Both involve requested relief: {requested_relief}"] if requested_relief and _tokenize_text(requested_relief) & precedent_tokens else []),
            limit=3,
        )

        differences = _compact_text_list(
            [f"The current matter emphasizes '{term}', which is not clearly reflected in the precedent summary." for term in unmatched_terms[:2]]
            + (["The current matter contains a defined request for relief, but the precedent summary does not show a matching remedy."] if requested_relief and not (_tokenize_text(requested_relief) & precedent_tokens) else []),
            limit=3,
        )
        similarities, differences = _normalize_case_comparison_lists(similarities, differences)

        annotated = dict(precedent)
        annotated["similarities"] = similarities
        annotated["differences"] = differences
        comparisons.append(annotated)

    return comparisons


def _build_model_case_comparisons(intake_outline: dict, precedents: list[dict]) -> tuple[list[dict], dict]:
    heuristic = _build_heuristic_case_comparisons(intake_outline, precedents)
    if not precedents:
        return heuristic, {
            "stage": "case_comparison",
            "status": "skipped_no_precedents",
            "model_status": "skipped_no_precedents",
            "error_message": "No precedents were available for case comparison.",
            "error_category": "input",
            "attempt_log": [],
        }
    if not is_llm_configured():
        return heuristic, {
            "stage": "case_comparison",
            "status": "heuristic_only",
            "model_status": "missing_credentials",
            "error_message": "LLM credentials are not configured, so precedent comparison stayed heuristic.",
            "error_category": "configuration",
            "attempt_log": [],
        }
    if not getattr(settings, "prediction_use_model_case_comparison", False):
        return heuristic, {
            "stage": "case_comparison",
            "status": "heuristic_fast_path",
            "model_status": "disabled_for_latency",
            "error_message": "Model-based precedent comparison is disabled to reduce latency.",
            "error_category": "",
            "attempt_log": [],
        }

    instructions = (
        "You are a legal case comparison assistant. Compare each retrieved precedent against the user's current "
        "case outline. For every precedent, return 1 to 3 concise similarities and 1 to 3 concise differences. "
        "Anchor the comparison to facts, disputed issues, requested relief, and jurisdiction when available. "
        "Do not invent facts that are not present in the precedent summary. If the precedent summary is thin, "
        "say that the comparison is uncertain because the summary is thin."
    )
    user_payload = json.dumps(
        {
            "current_case": intake_outline,
            "precedents": [
                {
                    "source_index": index,
                    "title": precedent.get("title", ""),
                    "source_code": precedent.get("source_code", ""),
                    "published_at": precedent.get("published_at", ""),
                    "summary": precedent.get("summary", ""),
                }
                for index, precedent in enumerate(precedents)
            ],
        },
        ensure_ascii=False,
    )

    model_call = _invoke_model_with_retry(
        stage="case_comparison",
        schema_name="legal_case_comparisons",
        schema=CASE_COMPARISON_SCHEMA,
        instructions=instructions,
        user_input=user_payload,
    )
    if not model_call["ok"]:
        model_status = "timeout_fallback" if model_call["error_category"] == "timeout" else "heuristic_fallback"
        return heuristic, {
            "stage": "case_comparison",
            "status": "heuristic_fallback",
            "model_status": model_status,
            "error_message": model_call["error_message"],
            "error_category": model_call["error_category"],
            "attempt_log": model_call["attempt_log"],
        }

    comparisons_by_index = {}
    for item in model_call["response"]["data"].get("comparisons", []):
        try:
            index = int(item.get("source_index"))
        except (TypeError, ValueError):
            continue
        comparisons_by_index[index] = {
            "similarities": _compact_text_list(item.get("similarities"), limit=3),
            "differences": _compact_text_list(item.get("differences"), limit=3),
        }

    merged = []
    for index, precedent in enumerate(precedents):
        fallback = heuristic[index]
        model_item = comparisons_by_index.get(index, {})
        similarities, differences = _normalize_case_comparison_lists(
            model_item.get("similarities") or fallback.get("similarities", []),
            model_item.get("differences") or fallback.get("differences", []),
        )
        annotated = dict(precedent)
        annotated["similarities"] = similarities
        annotated["differences"] = differences
        merged.append(annotated)

    return merged, {
        "stage": "case_comparison",
        "status": "model",
        "model_status": "configured",
        "error_message": "",
        "error_category": "",
        "attempt_log": model_call["attempt_log"],
    }


def _merge_unique_strings(*groups: list[str]) -> list[str]:
    merged = []
    seen = set()
    for group in groups:
        for value in group or []:
            text_value = str(value or "").strip()
            key = text_value.lower()
            if not text_value or key in seen:
                continue
            seen.add(key)
            merged.append(text_value)
    return merged


def _normalize_confidence(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, 1.0))


def _stage_summary(label: str, status: str, attempt_log: list[dict], error_message: str = "") -> str:
    attempt_count = len(attempt_log or [])
    if error_message:
        return f"{label}: {status} after {attempt_count} attempt(s). Last error: {error_message}"
    return f"{label}: {status} after {attempt_count} attempt(s)."


def _decorate_prediction(
    analysis_result: dict,
    prediction: dict,
    precedents: list[dict],
    execution_trace: dict,
) -> dict:
    analysis = analysis_result.get("analysis", {})
    intake_outline = analysis_result.get("intake_outline", {})
    prediction["confidence"] = _normalize_confidence(prediction.get("confidence"))
    prediction["confidence_percent"] = int(round(prediction["confidence"] * 100))
    prediction["reason_points"] = _merge_unique_strings(
        prediction.get("key_factors", []),
        intake_outline.get("disputed_issues", []),
        analysis.get("claims", []),
        analysis.get("legal_topics", []),
    )[:6]
    prediction["risk_points"] = _merge_unique_strings(
        analysis.get("risk_flags", []),
        prediction.get("caveats", []),
    )[:6]
    prediction["requested_relief"] = str(analysis.get("requested_relief") or "").strip()
    prediction["jurisdiction"] = str(analysis.get("jurisdiction") or "").strip()
    prediction["facts"] = str(intake_outline.get("facts") or analysis.get("facts") or "").strip()
    prediction["disputed_issues"] = intake_outline.get("disputed_issues", [])
    prediction["keywords"] = intake_outline.get("keywords", [])
    prediction["support_case_count"] = len(precedents)
    prediction["prediction_status"] = prediction.get("status", "preview")
    prediction["execution_trace"] = execution_trace
    prediction["execution_summary"] = [
        _stage_summary(
            "Analysis",
            execution_trace.get("analysis", {}).get("status", analysis_result.get("analysis_mode", "unknown")),
            execution_trace.get("analysis", {}).get("attempt_log", []),
            execution_trace.get("analysis", {}).get("error_message", ""),
        ),
        _stage_summary(
            "Case comparison",
            execution_trace.get("case_comparison", {}).get("status", "skipped"),
            execution_trace.get("case_comparison", {}).get("attempt_log", []),
            execution_trace.get("case_comparison", {}).get("error_message", ""),
        ),
        _stage_summary(
            "Prediction",
            execution_trace.get("prediction", {}).get("status", prediction.get("status", "preview")),
            execution_trace.get("prediction", {}).get("attempt_log", []),
            execution_trace.get("prediction", {}).get("error_message", ""),
        ),
    ]
    return prediction


def _preview_prediction(analysis_result: dict, status: str = "preview", model_status: str = "preview_only", model_error: str = "") -> dict:
    supporting_titles = [item["title"] for item in _build_precedent_packets(analysis_result["results"], limit=3)]
    provider = get_llm_provider()

    prediction = {
        "status": status,
        "predicted_outcome": "A full prediction was not produced.",
        "likely_prevailing_party": "Undetermined",
        "confidence": 0,
        "reasoning": (
            "The pipeline completed event parsing and precedent retrieval, but the final model opinion was not available. "
            "Use the extracted facts, issues, requested relief, and supporting records as a research starting point."
        ),
        "key_factors": analysis_result.get("analysis", {}).get("legal_topics", [])[:4],
        "supporting_case_titles": supporting_titles,
        "caveats": [
            "Local corpus coverage is limited, so prediction quality depends on what was retrieved.",
            "This demo supports legal research and triage only; it is not legal advice.",
        ],
        "model_status": model_status,
        "model_error": model_error or (
            "" if is_llm_configured() else f"{provider} credentials are not configured, so only preview output is available."
        ),
        "model_name": get_llm_model_name() if is_llm_configured() else "preview",
        "response_id": "",
    }
    prediction["prediction_status"] = status
    return prediction


def _persist_agent_run(payload: dict, prediction: dict):
    structured_analysis = {
        "analysis": payload.get("analysis", {}),
        "intake_outline": payload.get("intake_outline", {}),
        "analysis_mode": payload.get("analysis_mode", ""),
        "analysis_error": payload.get("analysis_error", ""),
        "analysis_error_category": payload.get("analysis_error_category", ""),
        "analysis_attempt_log": payload.get("analysis_attempt_log", []),
        "coverage_note": payload.get("coverage_note", ""),
    }

    try:
        with engine.begin() as conn:
            run_row = conn.execute(
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
                    RETURNING id
                    """
                ),
                {
                    "input_text": payload["input_text"],
                    "source_filter": payload["source"],
                    "sort_mode": payload["sort"],
                    "extracted_keywords": payload["extracted_keywords"],
                    "structured_analysis": json.dumps(structured_analysis, ensure_ascii=False),
                    "result_count": payload["total"],
                },
            ).mappings().first()

            if not run_row:
                return

            conn.execute(
                text(
                    """
                    INSERT INTO agent_predictions (
                        agent_run_id,
                        model_provider,
                        model_name,
                        status,
                        predicted_outcome,
                        likely_prevailing_party,
                        confidence,
                        reasoning,
                        key_factors,
                        supporting_items,
                        raw_json
                    )
                    VALUES (
                        :agent_run_id,
                        :model_provider,
                        :model_name,
                        :status,
                        :predicted_outcome,
                        :likely_prevailing_party,
                        :confidence,
                        :reasoning,
                        CAST(:key_factors AS jsonb),
                        CAST(:supporting_items AS jsonb),
                        CAST(:raw_json AS jsonb)
                    )
                    """
                ),
                {
                    "agent_run_id": run_row["id"],
                    "model_provider": get_llm_provider() if is_llm_configured() else "local",
                    "model_name": prediction.get("model_name") or (get_llm_model_name() if is_llm_configured() else "preview"),
                    "status": prediction.get("status", ""),
                    "predicted_outcome": prediction.get("predicted_outcome", ""),
                    "likely_prevailing_party": prediction.get("likely_prevailing_party", ""),
                    "confidence": prediction.get("confidence", 0),
                    "reasoning": prediction.get("reasoning", ""),
                    "key_factors": json.dumps(prediction.get("key_factors", []), ensure_ascii=False),
                    "supporting_items": json.dumps(prediction.get("supporting_case_titles", []), ensure_ascii=False),
                    "raw_json": json.dumps(prediction, ensure_ascii=False),
                },
            )
    except Exception:
        return


def _find_cached_prediction(text_input: str, source: str, sort: str) -> dict | None:
    cleaned = str(text_input or "").strip()
    if not cleaned:
        return None

    try:
        rows = fetch_all(
            """
            SELECT
                ap.raw_json,
                ap.model_name,
                ap.model_provider,
                ap.status,
                ap.created_at
            FROM agent_predictions ap
            JOIN agent_runs ar ON ar.id = ap.agent_run_id
            WHERE LOWER(ar.input_text) = LOWER(:input_text)
              AND ar.source_filter = :source_filter
              AND ar.sort_mode = :sort_mode
            ORDER BY ap.created_at DESC
            LIMIT 1
            """,
            {
                "input_text": cleaned,
                "source_filter": str(source or "all"),
                "sort_mode": str(sort or "relevance"),
            },
        )
    except Exception:
        return None

    if not rows:
        return None
    return rows[0]


def predict_legal_outcome(
    text: str,
    limit: int = 8,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    refresh: bool = False,
):
    cache_key = ("predict", str(text or "").strip(), int(limit), int(offset), str(source or "all"), str(sort or "relevance"))
    if not refresh:
        cached = _get_cached_prediction(cache_key)
        if cached is not None:
            return cached

    analysis_result = analyze_sentence_search(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        refresh=refresh,
        origin_page="predict",
    )

    precedents = _build_precedent_packets(analysis_result["results"], limit=3)
    annotated_precedents, case_comparison_trace = _build_model_case_comparisons(
        analysis_result.get("intake_outline", {}),
        precedents,
    )

    execution_trace = {
        "analysis": {
            "status": analysis_result.get("analysis_mode", "heuristic"),
            "error_message": analysis_result.get("analysis_error", ""),
            "error_category": analysis_result.get("analysis_error_category", ""),
            "attempt_log": analysis_result.get("analysis_attempt_log", []),
        },
        "case_comparison": case_comparison_trace,
        "prediction": {
            "status": "pending",
            "error_message": "",
            "error_category": "",
            "attempt_log": [],
        },
    }

    if not annotated_precedents:
        prediction = _preview_prediction(
            analysis_result,
            status="preview",
            model_status="skipped_no_precedents",
            model_error="Prediction was skipped because no supporting precedents were retrieved.",
        )
        execution_trace["prediction"] = {
            "status": "skipped_no_precedents",
            "error_message": "Prediction was skipped because no supporting precedents were retrieved.",
            "error_category": "input",
            "attempt_log": [],
        }
        prediction = _decorate_prediction(analysis_result, prediction, annotated_precedents, execution_trace)
        response_payload = {
            **analysis_result,
            "precedents": annotated_precedents,
            "prediction": prediction,
            "prediction_cache_status": "miss",
        }
        _persist_agent_run(analysis_result, prediction)
        _set_cached_prediction(cache_key, response_payload)
        return response_payload

    if not is_llm_configured():
        prediction = _preview_prediction(
            analysis_result,
            status="preview",
            model_status="missing_credentials",
        )
        execution_trace["prediction"] = {
            "status": "preview",
            "error_message": prediction.get("model_error", ""),
            "error_category": "configuration",
            "attempt_log": [],
        }
        prediction = _decorate_prediction(analysis_result, prediction, annotated_precedents, execution_trace)
        response_payload = {
            **analysis_result,
            "precedents": annotated_precedents,
            "prediction": prediction,
            "prediction_cache_status": "miss",
        }
        _persist_agent_run(analysis_result, prediction)
        _set_cached_prediction(cache_key, response_payload)
        return response_payload

    cached_prediction_row = None if refresh else _find_cached_prediction(text, source, sort)
    if cached_prediction_row:
        prediction = dict(cached_prediction_row.get("raw_json") or {})
        prediction.update(
            {
                "status": "history_reuse",
                "prediction_status": "history_reuse",
                "model_status": "history_reuse",
                "model_error": "",
                "model_name": prediction.get("model_name") or cached_prediction_row.get("model_name", ""),
                "response_id": prediction.get("response_id", ""),
            }
        )
        execution_trace["prediction"] = {
            "status": "history_reuse",
            "error_message": "",
            "error_category": "",
            "attempt_log": [],
        }
        prediction = _decorate_prediction(analysis_result, prediction, annotated_precedents, execution_trace)
        response_payload = {
            **analysis_result,
            "precedents": annotated_precedents,
            "prediction": prediction,
            "prediction_cache_status": "miss",
        }
        _set_cached_prediction(cache_key, response_payload)
        return response_payload

    instructions = (
        "You are a legal research assistant producing a cautious demo prediction. "
        "Use only the supplied event analysis and retrieved precedents. "
        "If the precedents are thin or noisy, lower confidence and say so. "
        "Do not invent courts, statutes, or cases."
    )
    user_payload = json.dumps(
        {
            "event_text": analysis_result["input_text"],
            "intake_outline": analysis_result.get("intake_outline", {}),
            "analysis": analysis_result["analysis"],
            "precedents": annotated_precedents,
        },
        ensure_ascii=False,
    )

    prediction_call = _invoke_model_with_retry(
        stage="prediction",
        schema_name="legal_prediction_demo",
        schema=PREDICTION_SCHEMA,
        instructions=instructions,
        user_input=user_payload,
    )

    if prediction_call["ok"]:
        response = prediction_call["response"]
        prediction = dict(response["data"])
        prediction.update(
            {
                "status": "model",
                "model_status": "configured",
                "model_error": "",
                "model_name": response.get("model", get_llm_model_name()),
                "response_id": response.get("response_id", ""),
            }
        )
        execution_trace["prediction"] = {
            "status": "model",
            "error_message": "",
            "error_category": "",
            "attempt_log": prediction_call["attempt_log"],
        }
    else:
        error_category = prediction_call["error_category"] or "provider_error"
        model_status = "timeout_fallback" if error_category == "timeout" else "fallback_error"
        prediction = _preview_prediction(
            analysis_result,
            status="fallback",
            model_status=model_status,
            model_error=prediction_call["error_message"],
        )
        execution_trace["prediction"] = {
            "status": "fallback",
            "error_message": prediction_call["error_message"],
            "error_category": error_category,
            "attempt_log": prediction_call["attempt_log"],
        }

    prediction = _decorate_prediction(analysis_result, prediction, annotated_precedents, execution_trace)
    response_payload = {
        **analysis_result,
        "precedents": annotated_precedents,
        "prediction": prediction,
        "prediction_cache_status": "miss",
    }
    _persist_agent_run(analysis_result, prediction)
    if prediction.get("status") != "fallback":
        _set_cached_prediction(cache_key, response_payload)

    return response_payload
