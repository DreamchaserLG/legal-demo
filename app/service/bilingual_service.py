import copy
import json
import re
import time
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.service.archive_service import safe_archive_source_item_by_id
from app.service.common_service import looks_mojibake, plain_text_preview, repair_text, split_keywords
from app.service.llm_service import LLMServiceError, create_structured_response, is_llm_configured

_BILINGUAL_CACHE: dict[tuple, tuple[float, dict]] = {}

KEYWORD_TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "query_zh": {"type": "string"},
        "query_en": {"type": "string"},
        "keywords_zh": {"type": "array", "items": {"type": "string"}},
        "keywords_en": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["query_zh", "query_en", "keywords_zh", "keywords_en"],
    "additionalProperties": False,
}

ANALYSIS_BILINGUAL_SCHEMA = {
    "type": "object",
    "properties": {
        "input_zh": {"type": "string"},
        "input_en": {"type": "string"},
        "facts_zh": {"type": "string"},
        "facts_en": {"type": "string"},
        "summary_zh": {"type": "string"},
        "summary_en": {"type": "string"},
        "requested_relief_zh": {"type": "string"},
        "requested_relief_en": {"type": "string"},
        "disputed_issues_zh": {"type": "array", "items": {"type": "string"}},
        "disputed_issues_en": {"type": "array", "items": {"type": "string"}},
        "keywords_zh": {"type": "array", "items": {"type": "string"}},
        "keywords_en": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "input_zh",
        "input_en",
        "facts_zh",
        "facts_en",
        "summary_zh",
        "summary_en",
        "requested_relief_zh",
        "requested_relief_en",
        "disputed_issues_zh",
        "disputed_issues_en",
        "keywords_zh",
        "keywords_en",
    ],
    "additionalProperties": False,
}

RESULT_TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title_zh": {"type": "string"},
                    "title_en": {"type": "string"},
                    "summary_zh": {"type": "string"},
                    "summary_en": {"type": "string"},
                },
                "required": ["id", "title_zh", "title_en", "summary_zh", "summary_en"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

PREDICTION_BILINGUAL_SCHEMA = {
    "type": "object",
    "properties": {
        "predicted_outcome_zh": {"type": "string"},
        "predicted_outcome_en": {"type": "string"},
        "reasoning_zh": {"type": "string"},
        "reasoning_en": {"type": "string"},
        "likely_prevailing_party_zh": {"type": "string"},
        "likely_prevailing_party_en": {"type": "string"},
        "key_factors_zh": {"type": "array", "items": {"type": "string"}},
        "key_factors_en": {"type": "array", "items": {"type": "string"}},
        "caveats_zh": {"type": "array", "items": {"type": "string"}},
        "caveats_en": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "predicted_outcome_zh",
        "predicted_outcome_en",
        "reasoning_zh",
        "reasoning_en",
        "likely_prevailing_party_zh",
        "likely_prevailing_party_en",
        "key_factors_zh",
        "key_factors_en",
        "caveats_zh",
        "caveats_en",
    ],
    "additionalProperties": False,
}


def _cache_ttl() -> int:
    return max(0, int(getattr(settings, "cache_ttl_seconds", 300)))


def _cache_get(key: tuple):
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    entry = _BILINGUAL_CACHE.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at <= time.time():
        _BILINGUAL_CACHE.pop(key, None)
        return None
    return copy.deepcopy(payload)


def _cache_set(key: tuple, payload: dict):
    ttl = _cache_ttl()
    if ttl <= 0:
        return
    _BILINGUAL_CACHE[key] = (time.time() + ttl, copy.deepcopy(payload))


def clear_bilingual_cache():
    _BILINGUAL_CACHE.clear()


def detect_language(text: str) -> str:
    raw = str(text or "")
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", raw))
    en_count = len(re.findall(r"[A-Za-z]", raw))
    if zh_count and en_count:
        return "mixed"
    if zh_count:
        return "zh"
    if en_count:
        return "en"
    return "unknown"


def _dedupe_strings(values, limit: int | None = None) -> list[str]:
    items = []
    seen = set()
    for value in values or []:
        text_value = str(value or "").strip()
        key = text_value.lower()
        if not text_value or key in seen:
            continue
        seen.add(key)
        items.append(text_value)
        if limit and len(items) >= limit:
            break
    return items


def _compact_text(text: str, limit: int = 240) -> str:
    clean = plain_text_preview(text)
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "..."


def _call_structured(schema_name: str, schema: dict, instructions: str, user_payload: dict) -> dict:
    return create_structured_response(
        schema_name=schema_name,
        schema=schema,
        instructions=instructions,
        user_input=json.dumps(user_payload, ensure_ascii=False),
    )["data"]


def _fallback_pair(text: str, language: str) -> dict:
    clean = repair_text(text)
    if language == "zh":
        return {"zh": clean, "en": clean}
    if language == "en":
        return {"zh": clean, "en": clean}
    return {"zh": clean, "en": clean}


def build_bilingual_keyword_bundle(keywords_input: str | list[str], module: str = "canada") -> dict:
    raw_text = ", ".join(keywords_input) if isinstance(keywords_input, list) else str(keywords_input or "")
    query_language = detect_language(raw_text)
    original_keywords = _dedupe_strings(split_keywords(keywords_input), limit=12)
    cache_key = ("keyword-bilingual", module, raw_text.strip().lower())
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    payload = {
        "query_language": query_language,
        "query_zh": raw_text.strip() if query_language in {"zh", "mixed"} else "",
        "query_en": raw_text.strip() if query_language in {"en", "mixed"} else "",
        "keywords_zh": original_keywords if query_language in {"zh", "mixed"} else [],
        "keywords_en": original_keywords if query_language in {"en", "mixed"} else [],
        "original_keywords": original_keywords,
        "retrieval_keywords": original_keywords,
        "translation_status": "fallback",
    }

    if is_llm_configured() and raw_text.strip() and query_language in {"zh", "mixed"}:
        try:
            data = _call_structured(
                "bilingual_keyword_query",
                KEYWORD_TRANSLATION_SCHEMA,
                (
                    "You are preparing legal retrieval keywords for a bilingual Chinese-English workflow. "
                    "Translate the query faithfully between Chinese and English. Keep legal names, agencies, "
                    "and statutes accurate. Return 4 to 10 retrieval-friendly keywords in both languages."
                ),
                {"module": module, "query": raw_text, "keywords": original_keywords},
            )
            payload.update(
                {
                    "query_zh": str(data.get("query_zh") or payload["query_zh"]).strip(),
                    "query_en": str(data.get("query_en") or payload["query_en"]).strip(),
                    "keywords_zh": _dedupe_strings(data.get("keywords_zh"), limit=10) or payload["keywords_zh"],
                    "keywords_en": _dedupe_strings(data.get("keywords_en"), limit=10) or payload["keywords_en"],
                    "translation_status": "model",
                }
            )
        except LLMServiceError:
            pass

    retrieval_keywords = _dedupe_strings(payload.get("keywords_en") + payload.get("keywords_zh") + original_keywords, limit=14)
    payload["retrieval_keywords"] = retrieval_keywords or original_keywords
    _cache_set(cache_key, payload)
    return payload


def build_bilingual_analysis_pack(input_text: str, analysis: dict, module: str = "canada") -> dict:
    raw_text = str(input_text or "").strip()
    query_language = detect_language(raw_text)
    cache_key = ("analysis-bilingual", module, raw_text.lower(), json.dumps(analysis or {}, ensure_ascii=False, sort_keys=True))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    fallback = {
        "query_language": query_language,
        "input_texts": _fallback_pair(raw_text, query_language),
        "facts": _fallback_pair(str(analysis.get("facts") or analysis.get("summary") or "").strip(), query_language),
        "summary": _fallback_pair(str(analysis.get("summary") or analysis.get("facts") or "").strip(), query_language),
        "requested_relief": _fallback_pair(str(analysis.get("requested_relief") or "").strip(), query_language),
        "disputed_issues": {
            "zh": _dedupe_strings(analysis.get("disputed_issues"), limit=6),
            "en": _dedupe_strings(analysis.get("disputed_issues"), limit=6),
        },
        "keywords": {
            "zh": _dedupe_strings(analysis.get("search_keywords"), limit=8),
            "en": _dedupe_strings(analysis.get("search_keywords"), limit=8),
        },
        "translation_status": "fallback",
    }

    payload = copy.deepcopy(fallback)
    if is_llm_configured() and raw_text:
        try:
            data = _call_structured(
                "bilingual_case_analysis",
                ANALYSIS_BILINGUAL_SCHEMA,
                (
                    "You are preparing a bilingual Chinese-English legal analysis pack. "
                    "Translate the input and each structured field faithfully. "
                    "Keep party names, statute names, court names, and legal terms precise. "
                    "The English side should be retrieval-friendly."
                ),
                {"module": module, "input_text": raw_text, "analysis": analysis},
            )
            payload = {
                "query_language": query_language,
                "input_texts": {
                    "zh": str(data.get("input_zh") or fallback["input_texts"]["zh"]).strip(),
                    "en": str(data.get("input_en") or fallback["input_texts"]["en"]).strip(),
                },
                "facts": {
                    "zh": str(data.get("facts_zh") or fallback["facts"]["zh"]).strip(),
                    "en": str(data.get("facts_en") or fallback["facts"]["en"]).strip(),
                },
                "summary": {
                    "zh": str(data.get("summary_zh") or fallback["summary"]["zh"]).strip(),
                    "en": str(data.get("summary_en") or fallback["summary"]["en"]).strip(),
                },
                "requested_relief": {
                    "zh": str(data.get("requested_relief_zh") or fallback["requested_relief"]["zh"]).strip(),
                    "en": str(data.get("requested_relief_en") or fallback["requested_relief"]["en"]).strip(),
                },
                "disputed_issues": {
                    "zh": _dedupe_strings(data.get("disputed_issues_zh"), limit=6) or fallback["disputed_issues"]["zh"],
                    "en": _dedupe_strings(data.get("disputed_issues_en"), limit=6) or fallback["disputed_issues"]["en"],
                },
                "keywords": {
                    "zh": _dedupe_strings(data.get("keywords_zh"), limit=8) or fallback["keywords"]["zh"],
                    "en": _dedupe_strings(data.get("keywords_en"), limit=8) or fallback["keywords"]["en"],
                },
                "translation_status": "model",
            }
        except LLMServiceError:
            payload = fallback

    payload["retrieval_keywords"] = _dedupe_strings(
        payload.get("keywords", {}).get("en") + payload.get("keywords", {}).get("zh") + analysis.get("search_keywords", []),
        limit=14,
    )
    _cache_set(cache_key, payload)
    return payload


def _persist_item_translation(row: dict, translation_payload: dict):
    raw_json = copy.deepcopy(row.get("raw_json") or {})
    translations = copy.deepcopy(raw_json.get("translations") or {})
    preview = copy.deepcopy(translations.get("preview") or {})
    preview.update(translation_payload)
    translations["preview"] = preview
    raw_json["translations"] = translations

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE source_items
                SET raw_json = CAST(:raw_json AS jsonb),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :item_id
                """
            ),
            {"item_id": int(row.get("id") or 0), "raw_json": json.dumps(raw_json, ensure_ascii=False)},
        )
    row["raw_json"] = raw_json
    safe_archive_source_item_by_id(int(row.get("id") or 0), archive_event="translation_update")


def enrich_result_rows_bilingual(rows: list[dict], query_language: str = "zh") -> list[dict]:
    if not rows:
        return rows

    limit = max(0, int(getattr(settings, "bilingual_result_preview_limit", 6)))
    if limit <= 0:
        return rows

    candidates = []
    for row in rows[:limit]:
        meta = row.get("raw_json") or {}
        preview = ((meta.get("translations") or {}).get("preview") or {})
        if (
            preview.get("title_zh")
            and preview.get("summary_zh")
            and not looks_mojibake(preview.get("title_zh"))
            and not looks_mojibake(preview.get("summary_zh"))
        ):
            continue
        candidates.append(
            {
                "id": int(row.get("id") or 0),
                "title": repair_text(row.get("title")),
                "summary": _compact_text(row.get("summary") or row.get("raw_text"), 320),
                "source_code": str(row.get("source_code") or "").strip(),
            }
        )

    if not candidates:
        return rows

    translation_map: dict[int, dict[str, str]] = {}
    if is_llm_configured():
        try:
            data = _call_structured(
                "bilingual_result_preview",
                RESULT_TRANSLATION_SCHEMA,
                (
                    "You are preparing bilingual legal search result previews. Translate each title and summary "
                    "between Chinese and English. Keep case names, company names, program names, statute names, "
                    "and abbreviations accurate. Summaries should stay concise."
                ),
                {"query_language": query_language, "items": candidates},
            )
            for item in data.get("items", []):
                try:
                    item_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                translation_map[item_id] = {
                    "title_zh": str(item.get("title_zh") or "").strip(),
                    "title_en": str(item.get("title_en") or "").strip(),
                    "summary_zh": str(item.get("summary_zh") or "").strip(),
                    "summary_en": str(item.get("summary_en") or "").strip(),
                }
        except LLMServiceError:
            translation_map = {}

    for row in rows[:limit]:
        row_id = int(row.get("id") or 0)
        translation = translation_map.get(row_id)
        if not translation:
            title = repair_text(row.get("title"))
            summary = _compact_text(row.get("summary") or row.get("raw_text"), 320)
            translation = {
                "title_zh": title,
                "title_en": title,
                "summary_zh": summary,
                "summary_en": summary,
            }
        _persist_item_translation(row, translation)

    return rows


def build_display_pair(original_text: str, preview: dict | None, query_language: str) -> dict:
    preview = preview or {}
    original = repair_text(original_text)
    zh_text = repair_text(preview.get("title_zh") or preview.get("summary_zh") or "")
    en_text = repair_text(preview.get("title_en") or preview.get("summary_en") or "")

    if looks_mojibake(zh_text):
        zh_text = ""
    if looks_mojibake(en_text):
        en_text = ""

    if query_language == "en":
        primary = en_text or original
        secondary = zh_text if zh_text and zh_text != primary else ""
    else:
        primary = zh_text or original
        secondary = en_text if en_text and en_text != primary else ""

    return {"primary": primary, "secondary": secondary}


def build_bilingual_prediction_pack(prediction: dict, analysis_result: dict) -> dict:
    module = str(analysis_result.get("module_code") or "canada")
    query_language = str(analysis_result.get("query_language") or "zh")
    cache_key = (
        "prediction-bilingual",
        module,
        query_language,
        json.dumps(prediction or {}, ensure_ascii=False, sort_keys=True),
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    fallback = {
        "predicted_outcome_zh": str(prediction.get("predicted_outcome") or "").strip(),
        "predicted_outcome_en": str(prediction.get("predicted_outcome") or "").strip(),
        "reasoning_zh": str(prediction.get("reasoning") or "").strip(),
        "reasoning_en": str(prediction.get("reasoning") or "").strip(),
        "likely_prevailing_party_zh": str(prediction.get("likely_prevailing_party") or "").strip(),
        "likely_prevailing_party_en": str(prediction.get("likely_prevailing_party") or "").strip(),
        "key_factors_zh": _dedupe_strings(prediction.get("key_factors"), limit=6),
        "key_factors_en": _dedupe_strings(prediction.get("key_factors"), limit=6),
        "caveats_zh": _dedupe_strings(prediction.get("caveats"), limit=6),
        "caveats_en": _dedupe_strings(prediction.get("caveats"), limit=6),
        "translation_status": "fallback",
    }

    if is_llm_configured() and (prediction.get("predicted_outcome") or prediction.get("reasoning")):
        try:
            data = _call_structured(
                "bilingual_prediction_summary",
                PREDICTION_BILINGUAL_SCHEMA,
                (
                    "You are preparing a bilingual Chinese-English prediction summary for a legal research demo. "
                    "Translate the prediction faithfully. Keep the tone cautious, keep party names accurate, and do not add new facts."
                ),
                {"module": module, "query_language": query_language, "prediction": prediction},
            )
            fallback.update({
                "predicted_outcome_zh": str(data.get("predicted_outcome_zh") or fallback["predicted_outcome_zh"]).strip(),
                "predicted_outcome_en": str(data.get("predicted_outcome_en") or fallback["predicted_outcome_en"]).strip(),
                "reasoning_zh": str(data.get("reasoning_zh") or fallback["reasoning_zh"]).strip(),
                "reasoning_en": str(data.get("reasoning_en") or fallback["reasoning_en"]).strip(),
                "likely_prevailing_party_zh": str(data.get("likely_prevailing_party_zh") or fallback["likely_prevailing_party_zh"]).strip(),
                "likely_prevailing_party_en": str(data.get("likely_prevailing_party_en") or fallback["likely_prevailing_party_en"]).strip(),
                "key_factors_zh": _dedupe_strings(data.get("key_factors_zh"), limit=6) or fallback["key_factors_zh"],
                "key_factors_en": _dedupe_strings(data.get("key_factors_en"), limit=6) or fallback["key_factors_en"],
                "caveats_zh": _dedupe_strings(data.get("caveats_zh"), limit=6) or fallback["caveats_zh"],
                "caveats_en": _dedupe_strings(data.get("caveats_en"), limit=6) or fallback["caveats_en"],
                "translation_status": "model",
            })
        except LLMServiceError:
            pass

    _cache_set(cache_key, fallback)
    return fallback
