import copy
import json
import re
import time

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, fetch_all
from app.service.analysis_service import analyze_sentence_search
from app.service.bilingual_service import build_bilingual_prediction_pack
from app.service.common_service import plain_text_preview, repair_text
from app.service.llm_service import (
    LLMServiceError,
    create_structured_response,
    get_llm_model_name,
    get_llm_provider,
    is_llm_configured,
)
from app.service.module_service import get_module_definition, normalize_module

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
    raw = plain_text_preview(text)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "..."


def _tokenize_text(text: str) -> set[str]:
    english_terms = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", repair_text(text).lower())
    chinese_terms = re.findall(r"[\u4e00-\u9fff]{2,}", repair_text(text))
    return {term.strip() for term in english_terms + chinese_terms if term.strip()}


def _compact_text_list(values, limit: int = 3) -> list[str]:
    merged = []
    seen = set()
    for value in values or []:
        text_value = repair_text(value)
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
            "当前材料在主题上接近，但摘要信息偏薄，仍需回到原文核对事实对应关系。"
        ]
    if not normalized_differences:
        normalized_differences = [
            "当前案例与命中材料在事实层面仍可能有关键差异，需要继续核对原始记录。"
        ]

    similarity_keys = {item.lower() for item in normalized_similarities}
    filtered_differences = [item for item in normalized_differences if item.lower() not in similarity_keys]
    if not filtered_differences:
        filtered_differences = [
            "现有摘要不足以直接证明两案事实完全一致，仍需核对细节。"
        ]

    difference_keys = {item.lower() for item in filtered_differences}
    filtered_similarities = [item for item in normalized_similarities if item.lower() not in difference_keys]
    if not filtered_similarities:
        filtered_similarities = [
            "检索结果显示主题重合，但仍需要更细的事实比对。"
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

    try:
        case_rows = fetch_all(
            """
            SELECT
                COUNT(*) AS total_runs,
                COUNT(DISTINCT LOWER(BTRIM(input_text))) AS unique_cases
            FROM agent_runs
            WHERE COALESCE(BTRIM(input_text), '') <> ''
            """
        )
    except Exception:
        case_rows = []

    counts = {"ofac": 0, "canlii": 0, "other": 0}
    for row in rows:
        source_code = row.get("source_code")
        total = int(row.get("total") or 0)
        if source_code in counts:
            counts[source_code] = total
        else:
            counts["other"] += total

    case_stats = case_rows[0] if case_rows else {}
    external_total_items = sum(counts.values())
    analyzed_case_count = int(case_stats.get("unique_cases") or 0)
    analysis_run_count = int(case_stats.get("total_runs") or 0)

    return {
        "counts": counts,
        "total_items": external_total_items,
        "external_total_items": external_total_items,
        "analyzed_case_count": analyzed_case_count,
        "analysis_run_count": analysis_run_count,
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
                "title": repair_text(row.get("title", "")),
                "source_code": row.get("source_code", ""),
                "published_at": str(row.get("published_at") or ""),
                "summary": _safe_excerpt(row.get("summary") or meta.get("remarks") or row.get("raw_text")),
                "url": row.get("item_url", ""),
                "source_url": meta.get("database_page") or meta.get("source_csv_url") or "",
            }
        )
    return packets


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _support_scope_label(scope: str) -> str:
    normalized = repair_text(scope).lower()
    if normalized == "national_federal":
        return "National / Federal"
    if normalized == "provincial_local":
        return "Provincial / Local"
    return "Case Support"


def _flatten_related_cases(law_entry: dict) -> list[dict]:
    items = []
    for column in law_entry.get("case_columns", []) or []:
        column_key = repair_text(column.get("key") or "")
        for case in column.get("items", []) or []:
            merged = dict(case)
            merged["scope"] = repair_text(merged.get("scope") or column_key)
            items.append(merged)
    if not items:
        items.extend(dict(case) for case in (law_entry.get("related_cases") or []))
    return items


def _build_supporting_case_groups(module_packet: dict, limit_laws: int = 4, cases_per_law: int = 3) -> list[dict]:
    packet = module_packet or {}
    case_lookup = {}
    for case in packet.get("case_law_rows", []) or []:
        case_id = _safe_int(case.get("case_id"))
        if case_id:
            case_lookup[case_id] = dict(case)

    groups = []
    seen_laws = set()
    for law in packet.get("relevant_laws", []) or []:
        law_id = _safe_int(law.get("rule_id"))
        law_key = law_id or repair_text(law.get("title")).lower()
        if not law_key or law_key in seen_laws:
            continue
        seen_laws.add(law_key)

        cases = []
        seen_cases = set()
        for raw_case in _flatten_related_cases(dict(law)):
            case_id = _safe_int(raw_case.get("case_id"))
            full_case = dict(case_lookup.get(case_id) or {})
            merged_case = {**raw_case, **full_case}
            case_title = repair_text(merged_case.get("title"))
            case_key = case_id or case_title.lower()
            if not case_key or case_key in seen_cases:
                continue
            seen_cases.add(case_key)
            linked_law_titles = _compact_text_list(
                [rule.get("title") for rule in (merged_case.get("rules") or [])],
                limit=4,
            )
            if not linked_law_titles:
                linked_law_titles = _compact_text_list([law.get("title")], limit=1)
            cases.append(
                {
                    "case_id": case_id,
                    "title": case_title,
                    "court_level": repair_text(merged_case.get("court_level")),
                    "court_rank": _safe_int(merged_case.get("court_rank")),
                    "case_type": repair_text(merged_case.get("case_type")),
                    "judgment_date": str(merged_case.get("judgment_date") or "")[:10],
                    "summary": _safe_excerpt(merged_case.get("summary") or merged_case.get("facts") or "", limit=150),
                    "source_url": repair_text(merged_case.get("source_url")),
                    "scope": repair_text(merged_case.get("scope")),
                    "scope_label": _support_scope_label(merged_case.get("scope")),
                    "match_score": _safe_float(merged_case.get("match_score")),
                    "match_reason": repair_text(merged_case.get("match_reason")),
                    "linked_law_titles": linked_law_titles,
                }
            )

        if not cases:
            continue

        cases = sorted(
            cases,
            key=lambda item: (
                _safe_float(item.get("match_score")),
                _safe_int(item.get("court_rank")),
                item.get("judgment_date") or "",
            ),
            reverse=True,
        )[:cases_per_law]
        case_titles = _compact_text_list([case.get("title") for case in cases], limit=3)
        groups.append(
            {
                "rule_id": law_id,
                "title": repair_text(law.get("title")),
                "article_no": repair_text(law.get("article_no")),
                "article_summary": repair_text(law.get("article_summary")),
                "country": repair_text(law.get("country")),
                "legal_type": repair_text(law.get("legal_type") or law.get("rule_level")),
                "detail_url": repair_text(law.get("detail_url")),
                "source_url": repair_text(law.get("source_url")),
                "linked_case_count": _safe_int(law.get("linked_case_count")) or len(cases),
                "case_scope_summary": _compact_text_list([case.get("scope_label") for case in cases], limit=2),
                "support_summary": f"{repair_text(law.get('title'))}：{'、'.join(case_titles)}" if case_titles else "",
                "cases": cases,
            }
        )
        if len(groups) >= limit_laws:
            break

    return groups


def _build_precedent_packets_from_module_packet(module_packet: dict, limit: int = 5) -> list[dict]:
    packets = []
    for row in (module_packet or {}).get("case_law_rows", [])[:limit]:
        law_refs = []
        for law in row.get("rules", []) or []:
            law_refs.append(
                {
                    "rule_id": _safe_int(law.get("rule_id")),
                    "title": repair_text(law.get("title")),
                    "article_no": repair_text(law.get("article_no")),
                    "detail_url": repair_text(law.get("detail_url")),
                }
            )
        packets.append(
            {
                "title": repair_text(row.get("title", "")),
                "source_code": "case_rule_relation",
                "published_at": str(row.get("judgment_date") or ""),
                "summary": _safe_excerpt(row.get("summary") or row.get("facts") or ""),
                "url": row.get("source_url", ""),
                "source_url": row.get("source_url", ""),
                "court_level": repair_text(row.get("court_level")),
                "case_type": repair_text(row.get("case_type")),
                "scope": repair_text(row.get("scope")),
                "match_reason": repair_text(row.get("match_reason")),
                "law_titles": _compact_text_list([law.get("title") for law in (row.get("rules") or [])], limit=4),
                "law_refs": law_refs,
            }
        )
    return packets


def _build_heuristic_case_comparisons(intake_outline: dict, precedents: list[dict]) -> list[dict]:
    issue_terms = intake_outline.get("disputed_issues", []) or []
    keyword_terms = intake_outline.get("keywords", []) or []
    requested_relief = repair_text(intake_outline.get("requested_relief") or "")
    reference_terms = _compact_text_list(issue_terms + keyword_terms, limit=6)

    comparisons = []
    for precedent in precedents:
        precedent_text = " ".join(
            [
                repair_text(precedent.get("title") or ""),
                repair_text(precedent.get("summary") or ""),
            ]
        ).strip()
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
            [f"共享争议点或关键词：{term}" for term in matched_terms[:2]]
            + (
                [f"请求事项上存在重合：{requested_relief}"]
                if requested_relief and _tokenize_text(requested_relief) & precedent_tokens
                else []
            ),
            limit=3,
        )

        differences = _compact_text_list(
            [
                f"当前案情强调“{term}”，但该材料摘要里未明确体现。"
                for term in unmatched_terms[:2]
            ]
            + (
                ["当前案情的请求事项更明确，但该材料摘要尚不足以确认救济路径。"]
                if requested_relief and not (_tokenize_text(requested_relief) & precedent_tokens)
                else []
            ),
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
        "You are a legal case comparison assistant. Compare each retrieved precedent against the user's current case outline. "
        "For every precedent, return 1 to 3 concise similarities and 1 to 3 concise differences. "
        "Anchor the comparison to facts, disputed issues, requested relief, and jurisdiction when available. "
        "Do not invent facts that are not present in the precedent summary."
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
            text_value = repair_text(value)
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


def _build_prediction_story(module_code: str, analysis_result: dict, prediction: dict, precedents: list[dict], execution_trace: dict) -> list[dict]:
    normalized_module = normalize_module(module_code)
    module_profile = get_module_definition(normalized_module)
    intake = analysis_result.get("intake_outline", {}) or {}
    module_packet = analysis_result.get("module_packet") or {}
    law_titles = [repair_text(item.get("title")) for item in module_packet.get("relevant_laws", [])[:3]]
    precedent_titles = [repair_text(item.get("title")) for item in precedents[:3]]
    risk_points = _merge_unique_strings(
        analysis_result.get("analysis", {}).get("risk_flags", []),
        prediction.get("caveats", []),
    )[:3]

    if normalized_module == "us_sanctions":
        material_focus = "本次重点参考 OFAC 规则、名单记录和程序路径，而不是泛化到普通美国判例。"
    else:
        material_focus = "本轮先把法规放在前面，再把相关案例拆成国家/联邦与省级/地方两组来衡量。"

    return [
        {
            "slug": "facts",
            "kicker": "Facts",
            "title": "先把争议说清楚",
            "detail": repair_text(intake.get("facts") or analysis_result.get("analysis", {}).get("facts") or "系统没有提炼出足够完整的事实段落。"),
            "status": execution_trace.get("analysis", {}).get("status", "completed"),
        },
        {
            "slug": "materials",
            "kicker": module_profile["label_en"],
            "title": "再确定应该先看哪些法律与材料",
            "detail": "；".join(law_titles) if law_titles else material_focus,
            "status": "completed",
        },
        {
            "slug": "comparison",
            "kicker": "Comparison",
            "title": "把当前案情和已命中的材料逐一对照",
            "detail": "；".join(precedent_titles) if precedent_titles else "当前支持材料偏少，系统只能在有限材料上做谨慎比对。",
            "status": execution_trace.get("case_comparison", {}).get("status", "completed"),
        },
        {
            "slug": "uncertainty",
            "kicker": "Risk",
            "title": "最后看哪些不确定因素会让判断摇摆",
            "detail": "；".join(risk_points) if risk_points else "当前没有额外提炼出显著的不确定因素，但仍应回到原文核对。",
            "status": execution_trace.get("prediction", {}).get("status", prediction.get("status", "preview")),
        },
        {
            "slug": "outcome",
            "kicker": "Preliminary View",
            "title": "据此给出一个谨慎的初步判断",
            "detail": repair_text(prediction.get("reasoning") or prediction.get("predicted_outcome") or "当前只能给出预览级判断。"),
            "status": execution_trace.get("prediction", {}).get("status", prediction.get("status", "preview")),
        },
    ]


def _decorate_prediction(
    analysis_result: dict,
    prediction: dict,
    precedents: list[dict],
    execution_trace: dict,
) -> dict:
    analysis = analysis_result.get("analysis", {})
    intake_outline = analysis_result.get("intake_outline", {})
    module_code = normalize_module(analysis_result.get("module_code"))
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
    prediction["requested_relief"] = repair_text(analysis.get("requested_relief") or "")
    prediction["jurisdiction"] = repair_text(analysis.get("jurisdiction") or "")
    prediction["facts"] = repair_text(intake_outline.get("facts") or analysis.get("facts") or "")
    prediction["disputed_issues"] = intake_outline.get("disputed_issues", [])
    prediction["keywords"] = intake_outline.get("keywords", [])
    prediction["support_case_count"] = len(precedents)
    prediction["prediction_status"] = prediction.get("status", "preview")
    prediction["module_code"] = module_code
    prediction["module_profile"] = get_module_definition(module_code)
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
            "预测研判",
            execution_trace.get("prediction", {}).get("status", prediction.get("status", "preview")),
            execution_trace.get("prediction", {}).get("attempt_log", []),
            execution_trace.get("prediction", {}).get("error_message", ""),
        ),
    ]
    prediction["prediction_process"] = _build_prediction_story(module_code, analysis_result, prediction, precedents, execution_trace)
    prediction["bilingual"] = build_bilingual_prediction_pack(prediction, analysis_result)
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
            "Use the extracted facts, disputed issues, requested relief, and supporting materials as the next research starting point."
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


def _build_local_reasoned_prediction(analysis_result: dict, precedents: list[dict]) -> dict:
    analysis = analysis_result.get("analysis", {}) or {}
    intake_outline = analysis_result.get("intake_outline", {}) or {}
    module_packet = analysis_result.get("module_packet", {}) or {}
    module_code = normalize_module(analysis_result.get("module_code"))

    law_titles = _compact_text_list([item.get("title") for item in module_packet.get("relevant_laws", [])], limit=3)
    issues = _compact_text_list(intake_outline.get("disputed_issues", []) or analysis.get("claims", []), limit=3)
    topics = _compact_text_list(analysis.get("legal_topics", []), limit=4)
    requested_relief = repair_text(intake_outline.get("requested_relief") or analysis.get("requested_relief") or "")
    facts = repair_text(intake_outline.get("facts") or analysis.get("facts") or analysis.get("summary") or "")
    support_titles = [item["title"] for item in precedents[:3]]

    base_confidence = 0.4
    base_confidence += 0.08 if law_titles else 0
    base_confidence += 0.06 if issues else 0
    base_confidence += 0.04 if requested_relief else 0
    base_confidence += 0.04 if support_titles else 0
    confidence = max(0.32, min(round(base_confidence, 2), 0.74))

    if module_code == "us_sanctions":
        predicted_outcome = (
            "从当前材料看，更可行的方向通常不是立即下结论，而是先围绕列名依据、控制关系、资金流向和整改措施做本地核查，"
            "再评估复议、除名或许可申请路径。"
        )
        likely_prevailing_party = "需优先走合规整改与行政救济路径"
        reasoning = (
            f"系统先从案情中提取了{('、'.join(issues) if issues else '争议点')}，"
            f"再结合{('、'.join(law_titles) if law_titles else '本地 OFAC 规则')}进行规则定位。"
            f"{' 当前请求事项是“' + requested_relief + '”。' if requested_relief else ''}"
            "在没有新增远程材料的前提下，现阶段更适合给出一份偏审慎的处理路径判断，而不是强行模拟最终裁决。"
        )
        caveats = [
            "若主体识别、受益所有权或控制关系尚未核清，判断会明显偏保守。",
            "如涉及冻结资金、限制交易或第三方协助规避制裁，通常需要并行评估许可路径。",
        ]
    else:
        predicted_outcome = (
            "从当前案情看，最终结论主要取决于关键事实能否被证据稳定支持，以及相关法院如何认定义务边界、处分效力或责任归属。"
        )
        likely_prevailing_party = "需结合证据进一步判断"
        reasoning = (
            f"系统先整理了案情事实：{facts[:120] + ('...' if len(facts) > 120 else '') if facts else '当前以输入原文为主'}。"
            f"{' 主要争议集中在：' + '；'.join(issues) + '。' if issues else ''}"
            f"{' 当前涉及：' + '、'.join(law_titles) + '。' if law_titles else ''}"
            f"{' 请求事项为：' + requested_relief + '。' if requested_relief else ''}"
            "基于这些已知信息，本次先给出一份面向人类阅读的本地推理结论，后续仍建议回到原始证据和法规全文继续核验。"
        )
        caveats = [
            "如果关键事实的时间线、主体关系或书面证据不足，结论可能快速反转。",
            "当前结果基于本地材料和规则化推理，不替代正式法律意见。",
        ]

    return {
        "status": "local_reasoning",
        "predicted_outcome": predicted_outcome,
        "likely_prevailing_party": likely_prevailing_party,
        "confidence": confidence,
        "reasoning": reasoning,
        "key_factors": topics or issues or law_titles,
        "supporting_case_titles": support_titles,
        "caveats": caveats,
        "model_status": "local_reasoning",
        "model_error": "",
        "model_name": "local",
        "response_id": "",
    }


def _law_reference_rows(module_packet: dict, limit: int = 6) -> list[dict]:
    rows = []
    seen = set()
    for item in (module_packet or {}).get("relevant_laws", []) or []:
        title = repair_text(item.get("title"))
        key = _safe_int(item.get("rule_id")) or title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "rule_id": _safe_int(item.get("rule_id")),
                "title": title,
                "article_no": repair_text(item.get("article_no")),
                "article_summary": repair_text(item.get("article_summary")),
                "detail_url": repair_text(item.get("detail_url")),
                "source_url": repair_text(item.get("source_url")),
                "legal_type": repair_text(item.get("legal_type") or item.get("rule_level")),
                "country": repair_text(item.get("country")),
                "linked_case_count": _safe_int(item.get("linked_case_count")),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _build_supporting_case_groups(
    module_packet: dict,
    limit_laws: int = 4,
    cases_per_law: int = 3,
    reference_terms: list[str] | None = None,
) -> list[dict]:
    packet = module_packet or {}
    case_lookup = {}
    for case in packet.get("case_law_rows", []) or []:
        case_id = _safe_int(case.get("case_id"))
        if case_id:
            case_lookup[case_id] = dict(case)

    normalized_terms = _compact_text_list(reference_terms or [], limit=12)
    weak_tokens = {
        "canada",
        "ontario",
        "court",
        "courts",
        "federal",
        "provincial",
        "local",
        "case",
        "cases",
        "matter",
        "dispute",
        "says",
    }
    groups = []
    seen_laws = set()
    for law in packet.get("relevant_laws", []) or []:
        law_id = _safe_int(law.get("rule_id"))
        law_key = law_id or repair_text(law.get("title")).lower()
        if not law_key or law_key in seen_laws:
            continue
        seen_laws.add(law_key)

        cases = []
        seen_cases = set()
        for raw_case in _flatten_related_cases(dict(law)):
            case_id = _safe_int(raw_case.get("case_id"))
            full_case = dict(case_lookup.get(case_id) or {})
            merged_case = {**raw_case, **full_case}
            case_title = repair_text(merged_case.get("title"))
            case_key = case_id or case_title.lower()
            if not case_key or case_key in seen_cases:
                continue
            seen_cases.add(case_key)
            linked_law_titles = _compact_text_list(
                [rule.get("title") for rule in (merged_case.get("rules") or [])],
                limit=4,
            )
            if not linked_law_titles:
                linked_law_titles = _compact_text_list([law.get("title")], limit=1)
            cases.append(
                {
                    "case_id": case_id,
                    "title": case_title,
                    "court_level": repair_text(merged_case.get("court_level")),
                    "court_rank": _safe_int(merged_case.get("court_rank")),
                    "case_type": repair_text(merged_case.get("case_type")),
                    "judgment_date": str(merged_case.get("judgment_date") or "")[:10],
                    "summary": _safe_excerpt(merged_case.get("summary") or merged_case.get("facts") or "", limit=150),
                    "source_url": repair_text(merged_case.get("source_url")),
                    "scope": repair_text(merged_case.get("scope")),
                    "scope_label": _support_scope_label(merged_case.get("scope")),
                    "match_score": _safe_float(merged_case.get("match_score")),
                    "match_reason": repair_text(merged_case.get("match_reason")),
                    "linked_law_titles": linked_law_titles,
                }
            )

        if not cases:
            continue

        cases = sorted(
            cases,
            key=lambda item: (
                _safe_float(item.get("match_score")),
                _safe_int(item.get("court_rank")),
                item.get("judgment_date") or "",
            ),
            reverse=True,
        )[:cases_per_law]
        case_titles = _compact_text_list([case.get("title") for case in cases], limit=3)
        law_tokens = _tokenize_text(
            " ".join(
                [
                    repair_text(law.get("title")),
                    repair_text(law.get("article_summary")),
                    repair_text(law.get("article_no")),
                    " ".join(case.get("title") or "" for case in cases),
                    " ".join(case.get("summary") or "" for case in cases),
                ]
            )
        )
        relevance_score = 0.0
        for term in normalized_terms:
            term_tokens = {token for token in _tokenize_text(term) if token.lower() not in weak_tokens}
            if term_tokens and term_tokens & law_tokens:
                relevance_score += 1.0

        groups.append(
            {
                "rule_id": law_id,
                "title": repair_text(law.get("title")),
                "article_no": repair_text(law.get("article_no")),
                "article_summary": repair_text(law.get("article_summary")),
                "country": repair_text(law.get("country")),
                "legal_type": repair_text(law.get("legal_type") or law.get("rule_level")),
                "detail_url": repair_text(law.get("detail_url")),
                "source_url": repair_text(law.get("source_url")),
                "linked_case_count": _safe_int(law.get("linked_case_count")) or len(cases),
                "relevance_score": relevance_score,
                "case_scope_summary": _compact_text_list([case.get("scope_label") for case in cases], limit=2),
                "support_summary": f"{repair_text(law.get('title'))}：{'、'.join(case_titles)}" if case_titles else "",
                "cases": cases,
            }
        )

    positive_scores = [_safe_float(group.get("relevance_score")) for group in groups if _safe_float(group.get("relevance_score")) > 0]
    if normalized_terms and positive_scores:
        max_score = max(positive_scores)
        threshold = max(1.0, max_score - 1.0)
        groups = [group for group in groups if _safe_float(group.get("relevance_score")) >= threshold]

    groups = sorted(
        groups,
        key=lambda item: (
            _safe_float(item.get("relevance_score")),
            _safe_int(item.get("linked_case_count")),
            max((_safe_float(case.get("match_score")) for case in (item.get("cases") or [])), default=0.0),
        ),
        reverse=True,
    )
    return groups[:limit_laws]


def _support_group_lines(groups: list[dict], limit_groups: int = 2, limit_cases: int = 2) -> list[str]:
    lines = []
    for group in groups[:limit_groups]:
        case_titles = _compact_text_list([case.get("title") for case in (group.get("cases") or [])], limit=limit_cases)
        if case_titles:
            lines.append(f"《{repair_text(group.get('title'))}》下参考 { '、'.join(case_titles) }")
        elif repair_text(group.get("title")):
            lines.append(f"《{repair_text(group.get('title'))}》可作为优先法规锚点")
    return lines


def _derive_supporting_case_titles(precedents: list[dict], groups: list[dict], limit: int = 8) -> list[str]:
    titles = []
    for group in groups or []:
        for case in group.get("cases", []) or []:
            titles.append(case.get("title"))
    for precedent in precedents or []:
        titles.append(precedent.get("title"))
    return _compact_text_list(titles, limit=limit)


def _count_supporting_cases(precedents: list[dict], groups: list[dict]) -> int:
    seen = set()
    for group in groups or []:
        for case in group.get("cases", []) or []:
            key = _safe_int(case.get("case_id")) or repair_text(case.get("title")).lower()
            if key:
                seen.add(key)
    for precedent in precedents or []:
        key = repair_text(precedent.get("title")).lower()
        if key:
            seen.add(key)
    return len(seen)


def _build_heuristic_case_comparisons(intake_outline: dict, precedents: list[dict]) -> list[dict]:
    issue_terms = intake_outline.get("disputed_issues", []) or []
    keyword_terms = intake_outline.get("keywords", []) or []
    requested_relief = repair_text(intake_outline.get("requested_relief") or "")
    reference_terms = _compact_text_list(issue_terms + keyword_terms, limit=6)

    comparisons = []
    for precedent in precedents:
        precedent_text = " ".join(
            [
                repair_text(precedent.get("title") or ""),
                repair_text(precedent.get("summary") or ""),
                " ".join(_compact_text_list(precedent.get("law_titles"), limit=3)),
            ]
        ).strip()
        precedent_tokens = _tokenize_text(precedent_text)

        matched_terms = []
        unmatched_terms = []
        for term in reference_terms:
            term_tokens = _tokenize_text(term)
            if term_tokens and term_tokens & precedent_tokens:
                matched_terms.append(term)
            else:
                unmatched_terms.append(term)

        law_titles = _compact_text_list(precedent.get("law_titles"), limit=2)
        similarities = _compact_text_list(
            [f"共享争议或关键词：{term}" for term in matched_terms[:2]]
            + ([f"关联法规：{'、'.join(law_titles)}"] if law_titles else [])
            + (
                [f"请求事项存在对应：{requested_relief}"]
                if requested_relief and _tokenize_text(requested_relief) & precedent_tokens
                else []
            )
            + ([f"本地关联依据：{repair_text(precedent.get('match_reason'))}"] if repair_text(precedent.get("match_reason")) else []),
            limit=3,
        )
        differences = _compact_text_list(
            [f"当前案情强调“{term}”，但该案例摘要里没有直接体现。" for term in unmatched_terms[:2]]
            + (
                ["当前案情的请求事项更明确，但该案例摘要不足以直接确认救济路径。"]
                if requested_relief and not (_tokenize_text(requested_relief) & precedent_tokens)
                else []
            )
            + (
                ["该案例与当前案情的法规锚点接近，但仍需逐项核对事实差异。"]
                if law_titles and not matched_terms
                else []
            ),
            limit=3,
        )
        similarities, differences = _normalize_case_comparison_lists(similarities, differences)

        annotated = dict(precedent)
        annotated["similarities"] = similarities
        annotated["differences"] = differences
        comparisons.append(annotated)

    return comparisons


def _build_prediction_story(module_code: str, analysis_result: dict, prediction: dict, precedents: list[dict], execution_trace: dict) -> list[dict]:
    normalized_module = normalize_module(module_code)
    module_profile = get_module_definition(normalized_module)
    intake = analysis_result.get("intake_outline", {}) or {}
    module_packet = analysis_result.get("module_packet") or {}
    reference_terms = _merge_unique_strings(intake.get("disputed_issues", []), intake.get("keywords", []))[:10]
    support_groups = prediction.get("supporting_case_groups") or _build_supporting_case_groups(
        module_packet,
        limit_laws=3,
        cases_per_law=2,
        reference_terms=reference_terms,
    )
    law_titles = _compact_text_list(
        [item.get("title") for item in support_groups] if support_groups else [item.get("title") for item in module_packet.get("relevant_laws", [])],
        limit=3,
    )
    support_lines = _support_group_lines(support_groups, limit_groups=2, limit_cases=2)
    precedent_lines = []
    for precedent in precedents[:3]:
        law_text = "、".join(_compact_text_list(precedent.get("law_titles"), limit=2))
        similar_text = "；".join(_compact_text_list(precedent.get("similarities"), limit=2))
        parts = [repair_text(precedent.get("title"))]
        if law_text:
            parts.append(f"对应 {law_text}")
        if similar_text:
            parts.append(similar_text)
        precedent_lines.append("，".join([part for part in parts if part]))
    risk_points = _merge_unique_strings(
        analysis_result.get("analysis", {}).get("risk_flags", []),
        prediction.get("caveats", []),
    )[:3]

    if normalized_module == "us_sanctions":
        material_focus = "本次重点参考 OFAC 规则、程序路径和本地已归档的相关材料，不把普通美国判例硬塞进来。"
    else:
        material_focus = "本轮先按拆分关键词在本地案例-法规关系库里定位法规，再把已归到这些法规下的相关案例拿来做比照。"

    material_detail = "；".join(support_lines) if support_lines else ("、".join(law_titles) if law_titles else material_focus)
    comparison_detail = "；".join(precedent_lines) if precedent_lines else "当前支持案例较少，系统只能基于已命中的法规和有限案例做谨慎比对。"
    outcome_detail = repair_text(prediction.get("reasoning") or prediction.get("predicted_outcome") or "当前只能给出预览级判断。")
    if support_lines:
        outcome_detail = f"{outcome_detail} 这一步优先参考了 {'；'.join(support_lines[:2])}。"

    return [
        {
            "slug": "facts",
            "kicker": "Facts",
            "title": "先把当前案情拆成事实、争议和请求事项",
            "detail": repair_text(intake.get("facts") or analysis_result.get("analysis", {}).get("facts") or "系统没有提炼出足够完整的事实段落。"),
            "status": execution_trace.get("analysis", {}).get("status", "completed"),
        },
        {
            "slug": "materials",
            "kicker": module_profile["label_en"],
            "title": "再按拆分关键词去本地检索法规和相关案例",
            "detail": material_detail,
            "status": "completed",
        },
        {
            "slug": "comparison",
            "kicker": "Comparison",
            "title": "把当前案情与已归到对应法规下的案例逐条比照",
            "detail": comparison_detail,
            "status": execution_trace.get("case_comparison", {}).get("status", "completed"),
        },
        {
            "slug": "uncertainty",
            "kicker": "Risk",
            "title": "最后看哪些不确定因素会左右结论",
            "detail": "；".join(risk_points) if risk_points else "当前没有额外提炼出显著的不确定因素，但仍应回到原始证据和法规全文继续核对。",
            "status": execution_trace.get("prediction", {}).get("status", prediction.get("status", "preview")),
        },
        {
            "slug": "outcome",
            "kicker": "Preliminary View",
            "title": "据此给出一个面向律师阅读顺序的初步判断",
            "detail": outcome_detail,
            "status": execution_trace.get("prediction", {}).get("status", prediction.get("status", "preview")),
        },
    ]


def _preview_prediction(analysis_result: dict, status: str = "preview", model_status: str = "preview_only", model_error: str = "") -> dict:
    module_packet = analysis_result.get("module_packet") or {}
    if normalize_module(analysis_result.get("module_code")) == "canada" and module_packet.get("case_law_rows"):
        support_groups = _build_supporting_case_groups(
            module_packet,
            limit_laws=3,
            cases_per_law=2,
            reference_terms=(analysis_result.get("retrieval_summary", {}) or {}).get("keywords")
            or (analysis_result.get("intake_outline", {}) or {}).get("keywords", []),
        )
        supporting_titles = _derive_supporting_case_titles(
            _build_precedent_packets_from_module_packet(module_packet, limit=3),
            support_groups,
            limit=4,
        )
    else:
        support_groups = []
        supporting_titles = [item["title"] for item in _build_precedent_packets(analysis_result["results"], limit=3)]

    provider = get_llm_provider()
    prediction = {
        "status": status,
        "predicted_outcome": "A full prediction was not produced.",
        "likely_prevailing_party": "Undetermined",
        "confidence": 0,
        "reasoning": (
            "The pipeline completed fact extraction and local retrieval. "
            "Use the matched laws, grouped supporting cases, disputed issues, and requested relief as the next research baseline."
        ),
        "key_factors": analysis_result.get("analysis", {}).get("legal_topics", [])[:4],
        "supporting_case_titles": supporting_titles,
        "supporting_case_groups": support_groups,
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


def _build_local_reasoned_prediction(analysis_result: dict, precedents: list[dict]) -> dict:
    analysis = analysis_result.get("analysis", {}) or {}
    intake_outline = analysis_result.get("intake_outline", {}) or {}
    module_packet = analysis_result.get("module_packet", {}) or {}
    module_code = normalize_module(analysis_result.get("module_code"))
    reference_terms = _merge_unique_strings(
        intake_outline.get("disputed_issues", []),
        intake_outline.get("keywords", []),
        analysis.get("legal_topics", []),
    )[:10]
    support_groups = _build_supporting_case_groups(
        module_packet,
        limit_laws=4,
        cases_per_law=3,
        reference_terms=reference_terms,
    )

    law_titles = _compact_text_list(
        [item.get("title") for item in support_groups] if support_groups else [item.get("title") for item in module_packet.get("relevant_laws", [])],
        limit=3,
    )
    issues = _compact_text_list(intake_outline.get("disputed_issues", []) or analysis.get("claims", []), limit=3)
    topics = _compact_text_list(analysis.get("legal_topics", []), limit=4)
    requested_relief = repair_text(intake_outline.get("requested_relief") or analysis.get("requested_relief") or "")
    facts = repair_text(intake_outline.get("facts") or analysis.get("facts") or analysis.get("summary") or "")
    support_titles = _derive_supporting_case_titles(precedents, support_groups, limit=5)
    support_lines = _support_group_lines(support_groups, limit_groups=3, limit_cases=2)

    base_confidence = 0.4
    base_confidence += 0.08 if law_titles else 0
    base_confidence += 0.08 if support_groups else 0
    base_confidence += 0.06 if issues else 0
    base_confidence += 0.04 if requested_relief else 0
    base_confidence += 0.04 if support_titles else 0
    confidence = max(0.32, min(round(base_confidence, 2), 0.78))

    if module_code == "us_sanctions":
        predicted_outcome = (
            "从当前材料看，更可行的方向通常不是立刻下结论，而是先围绕列名依据、控制关系、资金流向和整改措施完成本地核查，"
            "再评估复议、除名或许可申请路径。"
        )
        likely_prevailing_party = "需优先走合规整改与行政救济路径"
        reasoning = (
            f"系统先提取了{('、'.join(issues) if issues else '当前争议焦点')}，"
            f"再结合{('、'.join(law_titles) if law_titles else '本地 OFAC 规则')}定位程序路径。"
            f"{' 当前请求事项为：' + requested_relief + '。' if requested_relief else ''}"
            f"{' 本地比照材料方面：' + '；'.join(support_lines[:2]) + '。' if support_lines else ''}"
            "因此现阶段更适合给出一份谨慎的处理路径判断，而不是强行模拟最终裁断。"
        )
        caveats = [
            "若主体识别、受益所有权或控制关系尚未核清，判断会明显偏保守。",
            "如涉及冻结资金、受限交易或第三方协助规避制裁，通常需要并行评估许可路径。",
        ]
    else:
        predicted_outcome = (
            "从当前案情看，最终结论主要取决于关键事实能否被证据稳定支撑，以及相关法院如何认定义务边界、处分效力或责任归属。"
        )
        likely_prevailing_party = "需结合证据与案例进一步判断"
        reasoning = (
            f"系统先把案情拆成事实、争议焦点和请求事项，再按拆分后的关键词在本地法规-案例关系库里检索。"
            f"{' 当前涉及：' + '、'.join(law_titles) + '。' if law_titles else ''}"
            f"{' 本地已归到这些法规下的相关案例包括：' + '；'.join(support_lines) + '。' if support_lines else ''}"
            f"{' 争议焦点主要集中在：' + '、'.join(issues) + '。' if issues else ''}"
            f"{' 当前请求事项为：' + requested_relief + '。' if requested_relief else ''}"
            f"{' 核心事实摘要：' + facts[:120] + ('...' if len(facts) > 120 else '') + '。' if facts else ''}"
            "基于这些已命中的法规和案例，当前先给出一份面向律师阅读顺序的本地推理结论，后续仍应回到原始证据和法规全文继续核验。"
        )
        caveats = [
            "如果关键事实的时间线、主体关系或书面证据不足，结论可能快速反转。",
            "当前结果基于本地案例库与规则化推理，不替代正式法律意见。",
        ]
        if not support_groups:
            caveats.insert(0, "本地已归到法规下的对应案例较少，结论更多依赖法规锚点而非密集案例比照。")

    return {
        "status": "local_reasoning",
        "predicted_outcome": predicted_outcome,
        "likely_prevailing_party": likely_prevailing_party,
        "confidence": confidence,
        "reasoning": reasoning,
        "key_factors": _merge_unique_strings(
            topics or issues or law_titles,
            [f"法规锚点：{title}" for title in law_titles[:2]],
            [f"案例对照：{title}" for title in support_titles[:2]],
        )[:6],
        "supporting_case_titles": support_titles,
        "supporting_case_groups": support_groups,
        "caveats": caveats,
        "model_status": "local_reasoning",
        "model_error": "",
        "model_name": "local",
        "response_id": "",
    }


def _decorate_prediction(
    analysis_result: dict,
    prediction: dict,
    precedents: list[dict],
    execution_trace: dict,
) -> dict:
    analysis = analysis_result.get("analysis", {})
    intake_outline = analysis_result.get("intake_outline", {})
    module_code = normalize_module(analysis_result.get("module_code"))
    module_packet = analysis_result.get("module_packet") or {}
    supporting_case_groups = prediction.get("supporting_case_groups") or _build_supporting_case_groups(
        module_packet,
        limit_laws=4,
        cases_per_law=3,
        reference_terms=_merge_unique_strings(
            intake_outline.get("disputed_issues", []),
            intake_outline.get("keywords", []),
            analysis.get("legal_topics", []),
        )[:10],
    )
    if supporting_case_groups:
        linked_laws = [
            {
                "rule_id": _safe_int(item.get("rule_id")),
                "title": repair_text(item.get("title")),
                "article_no": repair_text(item.get("article_no")),
                "article_summary": repair_text(item.get("article_summary")),
                "detail_url": repair_text(item.get("detail_url")),
                "source_url": repair_text(item.get("source_url")),
                "legal_type": repair_text(item.get("legal_type")),
                "country": repair_text(item.get("country")),
                "linked_case_count": _safe_int(item.get("linked_case_count")),
            }
            for item in supporting_case_groups[:6]
        ]
    else:
        linked_laws = _law_reference_rows(module_packet, limit=6)
    supporting_case_titles = _derive_supporting_case_titles(precedents, supporting_case_groups, limit=8)

    prediction["confidence"] = _normalize_confidence(prediction.get("confidence"))
    prediction["confidence_percent"] = int(round(prediction["confidence"] * 100))
    prediction["linked_laws"] = linked_laws
    prediction["supporting_case_groups"] = supporting_case_groups
    prediction["supporting_case_titles"] = supporting_case_titles
    prediction["reason_points"] = _merge_unique_strings(
        prediction.get("key_factors", []),
        intake_outline.get("disputed_issues", []),
        analysis.get("claims", []),
        analysis.get("legal_topics", []),
        [f"法规锚点：{item['title']}" for item in linked_laws[:2]],
        [f"案例对照：{title}" for title in supporting_case_titles[:2]],
    )[:8]
    prediction["risk_points"] = _merge_unique_strings(
        analysis.get("risk_flags", []),
        prediction.get("caveats", []),
    )[:6]
    prediction["requested_relief"] = repair_text(analysis.get("requested_relief") or "")
    prediction["jurisdiction"] = repair_text(analysis.get("jurisdiction") or "")
    prediction["facts"] = repair_text(intake_outline.get("facts") or analysis.get("facts") or "")
    prediction["disputed_issues"] = intake_outline.get("disputed_issues", [])
    prediction["keywords"] = intake_outline.get("keywords", [])
    prediction["support_case_count"] = _count_supporting_cases(precedents, supporting_case_groups)
    prediction["recommendation_basis"] = _merge_unique_strings(
        _support_group_lines(supporting_case_groups, limit_groups=3, limit_cases=2),
        [f"优先法规：{item['title']}" for item in linked_laws[:2]],
    )[:6]
    prediction["prediction_status"] = prediction.get("status", "preview")
    prediction["module_code"] = module_code
    prediction["module_profile"] = get_module_definition(module_code)
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
            "预测研判",
            execution_trace.get("prediction", {}).get("status", prediction.get("status", "preview")),
            execution_trace.get("prediction", {}).get("attempt_log", []),
            execution_trace.get("prediction", {}).get("error_message", ""),
        ),
    ]
    prediction["prediction_process"] = _build_prediction_story(module_code, analysis_result, prediction, precedents, execution_trace)
    prediction["bilingual"] = build_bilingual_prediction_pack(prediction, analysis_result)
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
        "module_packet": payload.get("module_packet", {}),
    }

    try:
        with engine.begin() as conn:
            run_row = conn.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                        input_text,
                        module_code,
                        source_filter,
                        sort_mode,
                        extracted_keywords,
                        structured_analysis,
                        result_count
                    )
                    VALUES (
                        :input_text,
                        :module_code,
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
                    "module_code": normalize_module(payload.get("module_code")),
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
                        module_code,
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
                        :module_code,
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
                    "module_code": normalize_module(payload.get("module_code")),
                    "model_provider": get_llm_provider() if is_llm_configured() else "local",
                    "model_name": prediction.get("model_name") or (get_llm_model_name() if is_llm_configured() else "preview"),
                    "status": prediction.get("status", ""),
                    "predicted_outcome": repair_text(prediction.get("predicted_outcome", "")),
                    "likely_prevailing_party": repair_text(prediction.get("likely_prevailing_party", "")),
                    "confidence": prediction.get("confidence", 0),
                    "reasoning": repair_text(prediction.get("reasoning", "")),
                    "key_factors": json.dumps(prediction.get("key_factors", []), ensure_ascii=False),
                    "supporting_items": json.dumps(prediction.get("supporting_case_titles", []), ensure_ascii=False),
                    "raw_json": json.dumps(prediction, ensure_ascii=False),
                },
            )
    except Exception:
        return


def _find_cached_prediction(text_input: str, source: str, sort: str, module_code: str) -> dict | None:
    cleaned = repair_text(text_input)
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
              AND ar.module_code = :module_code
              AND ar.source_filter = :source_filter
              AND ar.sort_mode = :sort_mode
            ORDER BY ap.created_at DESC
            LIMIT 1
            """,
            {
                "input_text": cleaned,
                "module_code": normalize_module(module_code),
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
    module: str = "canada",
    refresh: bool = False,
):
    normalized_module = normalize_module(module)
    cleaned_text = repair_text(text)
    cache_key = (
        "predict",
        normalized_module,
        cleaned_text,
        int(limit),
        int(offset),
        str(source or "all"),
        str(sort or "relevance"),
    )

    if not refresh:
        cached_payload = _get_cached_prediction(cache_key)
        if cached_payload is not None:
            return cached_payload

    analysis_result = analyze_sentence_search(
        text=cleaned_text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=normalized_module,
        refresh=refresh,
        origin_page="predict",
        local_only=True,
    )

    if normalized_module == "canada" and (analysis_result.get("module_packet") or {}).get("case_law_rows"):
        precedents = _build_precedent_packets_from_module_packet(analysis_result.get("module_packet") or {}, limit=3)
    else:
        precedents = _build_precedent_packets(analysis_result["results"], limit=3)
    supporting_case_groups = _build_supporting_case_groups(
        analysis_result.get("module_packet") or {},
        limit_laws=4,
        cases_per_law=3,
        reference_terms=(analysis_result.get("retrieval_summary", {}) or {}).get("keywords")
        or (analysis_result.get("intake_outline", {}) or {}).get("keywords", []),
    )
    local_fast_mode = bool(getattr(settings, "prediction_local_fast_mode", True))
    if local_fast_mode:
        annotated_precedents = _build_heuristic_case_comparisons(
            analysis_result.get("intake_outline", {}),
            precedents,
        )
        case_comparison_trace = {
            "stage": "case_comparison",
            "status": "heuristic_fast_path",
            "model_status": "disabled_for_latency",
            "error_message": "",
            "error_category": "",
            "attempt_log": [],
        }
    else:
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

    if local_fast_mode:
        prediction = _build_local_reasoned_prediction(analysis_result, annotated_precedents)
        execution_trace["prediction"] = {
            "status": "local_reasoning",
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
        _persist_agent_run(analysis_result, prediction)
        _set_cached_prediction(cache_key, response_payload)
        return response_payload

    if not annotated_precedents:
        prediction = _preview_prediction(
            analysis_result,
            status="preview",
            model_status="skipped_no_precedents",
            model_error="当前没有检索到足以支撑预测的相关案例，本次仅返回结构化分析结果。",
        )
        execution_trace["prediction"] = {
            "status": "skipped_no_precedents",
            "error_message": "当前没有检索到足以支撑预测的相关案例，本次仅返回结构化分析结果。",
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

    cached_prediction_row = None if refresh else _find_cached_prediction(cleaned_text, source, sort, normalized_module)
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

    if normalize_module(analysis_result.get("module_code")) == "us_sanctions":
        instructions = (
            "You are a U.S. sanctions research assistant producing a cautious demo prediction. "
            "Use only the supplied event analysis, OFAC-related materials, the linked rules, and the locally grouped supporting materials under those rules. "
            "Focus on whether the company has a plausible path toward reconsideration, delisting, or licensing relief. "
            "If the materials are thin or noisy, lower confidence and say so. Do not invent cases, statutes, or outcomes."
        )
    else:
        instructions = (
            "You are a legal research assistant producing a cautious demo prediction. "
            "Use only the supplied event analysis, the linked laws, and the locally retrieved cases that have already been grouped under those laws. "
            "Explain how the laws and grouped cases support the preliminary view. "
            "If the precedents are thin or noisy, lower confidence and say so. "
            "Do not invent courts, statutes, or cases."
        )
    user_payload = json.dumps(
        {
            "event_text": analysis_result["input_text"],
            "intake_outline": analysis_result.get("intake_outline", {}),
            "analysis": analysis_result["analysis"],
            "retrieval_summary": analysis_result.get("retrieval_summary", {}),
            "linked_laws": _law_reference_rows(analysis_result.get("module_packet") or {}, limit=4),
            "law_case_groups": supporting_case_groups,
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
    _set_cached_prediction(cache_key, response_payload)
    return response_payload
