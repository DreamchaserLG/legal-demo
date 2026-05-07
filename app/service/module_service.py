import copy
import json
import re
import time
from collections import Counter

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.service.bilingual_service import build_display_pair
from app.service.canada_case_law_service import ensure_canada_law_tables
from app.service.common_service import plain_text_preview, repair_text
from app.service.legal_data_service import build_canada_case_rule_packet, get_canada_rule_detail_packet, sync_canada_legal_data
from app.service.llm_service import (
    LLMServiceError,
    create_structured_response,
    get_llm_model_name,
    is_llm_configured,
)

VALID_MODULES = {"canada", "us_sanctions"}
LEGISLATION_SOURCE_CODES = {"ca_federal_act", "ca_federal_regulation", "on_statute", "on_regulation"}
NATIONAL_FEDERAL_CASE_CODES = {"scc", "fc", "fca"}
SUPREME_COURT_CODES = {"scc", "uksc"}
APPEAL_COURT_CODES = {"fca", "onca", "abca", "bcca", "mbca", "nbca", "nlca", "nsca", "ntca", "nuca", "qcca", "skca", "ykca", "pescad"}
SUPERIOR_COURT_CODES = {"fc", "onsc", "abkb", "abqb", "bcsc", "mbkb", "mbqb", "nbkb", "nbqb", "nlsc", "nssc", "ntsc", "qccs", "skkb", "skqb", "yksc", "pecsc"}
PROVINCIAL_COURT_CODES = {"oncj", "ocj", "qccq", "skpc", "yktc", "nstc", "nspc", "pecp", "ntpc", "nupc"}

MODULE_DEFINITIONS = {
    "canada": {
        "label": "加拿大法规与案例模块",
        "label_en": "Canada Law and Cases Module",
        "picker_label": "加拿大法规与案例",
        "picker_label_en": "Canada Law and Cases",
        "subtitle": "先定位对应法律法规，再把加拿大案例按国家/联邦与省级/地方两层展开。",
        "subtitle_en": "Map governing legislation first, then expand Canadian cases into national-federal and provincial-local tracks.",
        "default_source": "canada",
        "source_options": [
            {
                "value": "canada",
                "label": "官方法规 + CanLII 案例",
                "label_en": "Official laws + CanLII cases",
                "picker_label": "法规 + 案例",
                "picker_label_en": "Laws + Cases",
            },
            {
                "value": "canlii",
                "label": "仅 CanLII 案例",
                "label_en": "CanLII cases only",
                "picker_label": "仅案例",
                "picker_label_en": "Cases Only",
            },
        ],
        "agent_title": "关联深度分析",
        "agent_title_en": "Linked Deep Analysis",
        "agent_placeholder": "例如：哪一部法规最关键？哪些省级案例最接近当前事实？",
        "question_prompts": [
            "哪一部法律法规最关键？",
            "国家/联邦层面的案例和地方层面的案例，哪边更关键？",
            "现有事实里最可能改变结果的点是什么？",
        ],
        "question_prompts_en": [
            "Which statute or regulation should I read first?",
            "Should I rely more on national-federal cases or provincial-local cases here?",
            "Which fact is most likely to change the outcome?",
        ],
    },
    "us_sanctions": {
        "label": "美国 OFAC 制裁模块",
        "label_en": "U.S. OFAC Sanctions Module",
        "picker_label": "美国 OFAC 制裁",
        "picker_label_en": "U.S. OFAC",
        "subtitle": "只围绕 OFAC 规则、名单记录、除名路径和合规整改，不扩展到普通美国判例。",
        "subtitle_en": "Stay inside OFAC rules, OFAC records, delisting paths, and remediation; do not expand into general U.S. case law.",
        "default_source": "ofac",
        "source_options": [
            {
                "value": "ofac",
                "label": "OFAC 规则与记录",
                "label_en": "OFAC rules and records",
                "picker_label": "OFAC 规则与记录",
                "picker_label_en": "OFAC Rules",
            },
        ],
        "agent_title": "OFAC 研判助手",
        "agent_title_en": "OFAC Reasoning Assistant",
        "agent_placeholder": "例如：如果公司想申请除名，第一步应该准备哪些材料？",
        "question_prompts": [
            "如果公司想申请除名，第一步最关键的动作是什么？",
            "当前更应该先走复议、除名还是许可证路径？",
            "哪些 OFAC 规则和材料要优先核对？",
        ],
        "question_prompts_en": [
            "If the company wants delisting, what is the first critical move?",
            "Should the team start with reconsideration, delisting, or licensing?",
            "Which OFAC rules and materials should be checked first?",
        ],
    },
}

MODULE_DEFINITIONS["canada"].update(
    {
        "label": "加拿大法律法规与案例模块",
        "picker_label": "加拿大法律法规与案例",
        "subtitle": "围绕当前案情整理加拿大法律法规、对应案例以及国家 / 地方层级的判例材料。",
        "agent_title": "关联深度分析",
        "agent_placeholder": "例如：当前最关键的法规是哪一条？哪些省级案例最接近当前事实？",
        "question_prompts": [
            "当前最关键的法律法规是哪一条？",
            "国家 / 联邦层面的案例和地方层面的案例，哪一边更关键？",
            "现有事实里最可能改变结果的点是什么？",
        ],
    }
)
MODULE_DEFINITIONS["us_sanctions"].update(
    {
        "label": "美国 OFAC 制裁模块",
        "picker_label": "美国 OFAC 制裁",
        "subtitle": "围绕 OFAC 规则、名单记录、除名路径和合规整改材料进行分析，不扩展到泛化美国案例。",
        "agent_title": "OFAC 研判助手",
        "agent_placeholder": "例如：如果公司要申请除名，第一步需要准备哪些材料？",
        "question_prompts": [
            "如果公司要申请除名，第一步最关键的动作是什么？",
            "当前更适合走复审、除名还是许可路径？",
            "哪些 OFAC 规则和材料需要优先核对？",
        ],
    }
)

LAW_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "laws": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "level": {"type": "string"},
                    "reason": {"type": "string"},
                    "reason_en": {"type": "string"},
                },
                "required": ["title", "level", "reason", "reason_en"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["laws"],
    "additionalProperties": False,
}

CHAT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer_zh": {"type": "string"},
        "answer_en": {"type": "string"},
        "support_points_zh": {"type": "array", "items": {"type": "string"}},
        "support_points_en": {"type": "array", "items": {"type": "string"}},
        "caution_points_zh": {"type": "array", "items": {"type": "string"}},
        "caution_points_en": {"type": "array", "items": {"type": "string"}},
        "suggested_followups_zh": {"type": "array", "items": {"type": "string"}},
        "suggested_followups_en": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "answer_zh",
        "answer_en",
        "support_points_zh",
        "support_points_en",
        "caution_points_zh",
        "caution_points_en",
        "suggested_followups_zh",
        "suggested_followups_en",
    ],
    "additionalProperties": False,
}

_MODULE_PACKET_CACHE: dict[tuple, tuple[float, dict]] = {}
_CANADA_LAW_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z0-9'’().,\-/ ]{3,120}?(?:Act|Code|Rules|Regulation(?:s)?|Charter|Convention|Order))\b"
)


def normalize_module(value: str | None) -> str:
    module = str(value or "canada").strip().lower()
    return module if module in VALID_MODULES else "canada"


def get_module_definition(module: str) -> dict:
    return copy.deepcopy(MODULE_DEFINITIONS[normalize_module(module)])


def resolve_source_for_module(module: str, source: str | None, strict: bool = True) -> str:
    module_code = normalize_module(module)
    default_source = get_module_definition(module_code)["default_source"]
    normalized_source = str(source or "").strip().lower()
    if strict:
        if module_code == "canada" and normalized_source == "canlii":
            return "canlii"
        return default_source
    if normalized_source in {default_source, "all"}:
        return default_source
    if module_code == "canada" and normalized_source == "canlii":
        return "canlii"
    return default_source


def get_source_options_for_module(module: str) -> list[dict]:
    return copy.deepcopy(get_module_definition(module).get("source_options", []))


def ensure_agent_support_tables():
    statements = [
        """
        ALTER TABLE agent_runs
        ADD COLUMN IF NOT EXISTS module_code VARCHAR(30) NOT NULL DEFAULT 'canada'
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_agent_runs_module_created
        ON agent_runs(module_code, created_at DESC)
        """,
        """
        ALTER TABLE agent_predictions
        ADD COLUMN IF NOT EXISTS module_code VARCHAR(30) NOT NULL DEFAULT 'canada'
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_agent_predictions_module_created
        ON agent_predictions(module_code, created_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_chat_logs (
            id BIGSERIAL PRIMARY KEY,
            module_code VARCHAR(30) NOT NULL DEFAULT 'canada',
            input_text TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_agent_chat_logs_module_created
        ON agent_chat_logs(module_code, created_at DESC)
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def ensure_module_support_tables():
    ensure_agent_support_tables()
    ensure_canada_law_tables()


def _cache_ttl() -> int:
    return max(0, int(getattr(settings, "cache_ttl_seconds", 300)))


def _cache_get(key: tuple):
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    entry = _MODULE_PACKET_CACHE.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at <= time.time():
        _MODULE_PACKET_CACHE.pop(key, None)
        return None
    return copy.deepcopy(payload)


def _cache_set(key: tuple, payload: dict):
    ttl = _cache_ttl()
    if ttl <= 0:
        return
    _MODULE_PACKET_CACHE[key] = (time.time() + ttl, copy.deepcopy(payload))


def clear_module_cache():
    _MODULE_PACKET_CACHE.clear()


def _compact_strings(values, limit: int | None = None) -> list[str]:
    items = []
    seen = set()
    for value in values or []:
        text_value = repair_text(value)
        lowered = text_value.lower()
        if not text_value or lowered in seen:
            continue
        seen.add(lowered)
        items.append(text_value)
        if limit and len(items) >= limit:
            break
    return items


def _source_title_pair(original_title: str, preview: dict | None) -> dict:
    preview = preview or {}
    original = repair_text(original_title)
    english = repair_text(preview.get("title_en")) or original
    chinese = repair_text(preview.get("title_zh"))
    secondary = chinese if chinese and chinese not in {original, english} else ""
    return {"primary": english or original, "secondary": secondary}


def _row_preview(row: dict) -> dict:
    meta = row.get("raw_json") or {}
    preview = ((meta.get("translations") or {}).get("preview") or {})
    query_language = str(row.get("query_language") or "zh")
    title_pair = _source_title_pair(row.get("title", ""), preview)
    summary_pair = build_display_pair(
        str(row.get("summary") or row.get("raw_text") or ""),
        {"summary_zh": preview.get("summary_zh"), "summary_en": preview.get("summary_en")},
        query_language,
    )
    return {
        "title_primary": title_pair["primary"],
        "title_secondary": title_pair["secondary"],
        "summary_primary": summary_pair["primary"],
        "summary_secondary": summary_pair["secondary"],
    }


def _derive_court_code(row: dict) -> str:
    existing = str(row.get("court_code") or "").strip().lower()
    if existing:
        return existing
    meta = row.get("raw_json") or {}
    candidates = [repair_text(meta.get("database_page")), repair_text(row.get("item_url"))]
    for candidate in candidates:
        if not candidate:
            continue
        match = re.search(r"/([a-z0-9]+)/?$", candidate.lower())
        if match:
            return match.group(1).lower()
        match = re.search(r"/([a-z0-9]+)/doc/", candidate.lower())
        if match:
            return match.group(1).lower()
    return ""


def _derive_court_level(court_code: str) -> int:
    code = str(court_code or "").strip().lower()
    if code in SUPREME_COURT_CODES:
        return 5
    if code in APPEAL_COURT_CODES:
        return 4
    if code in SUPERIOR_COURT_CODES:
        return 3
    if code in PROVINCIAL_COURT_CODES:
        return 2
    if code:
        return 1
    return 0


def _court_level_label(level_value: int, court_code: str = "") -> str:
    code = str(court_code or "").strip().upper()
    if level_value >= 5:
        return "最高法院"
    if level_value == 4:
        return "上诉法院"
    if level_value == 3:
        return "高等法院 / 联邦法院"
    if level_value == 2:
        return "省级 / 地方法院"
    if level_value == 1:
        return "Tribunal / Other"
    return code or "-"


def _case_scope(case_row: dict) -> str:
    court_level = int(case_row.get("court_level") or 0)
    court_code = str(case_row.get("court_code") or "").strip().lower()
    if court_level >= 5 or court_code in NATIONAL_FEDERAL_CASE_CODES:
        return "national_federal"
    if court_level >= 2:
        return "provincial_local"
    return "other"


def _case_entry_from_row(row: dict) -> dict:
    meta = row.get("raw_json") or {}
    preview = _row_preview(row)
    court_code = _derive_court_code(row)
    court_level = int(row.get("court_level") or 0) or _derive_court_level(court_code)
    court_level_label = repair_text(row.get("court_level_label") or "") or _court_level_label(court_level, court_code)
    entry = {
        "id": row.get("id"),
        "title": repair_text(row.get("title")),
        "title_primary": preview["title_primary"],
        "title_secondary": preview["title_secondary"],
        "summary": plain_text_preview(row.get("summary"))[:360],
        "summary_primary": preview["summary_primary"],
        "summary_secondary": preview["summary_secondary"],
        "excerpt": plain_text_preview(row.get("raw_text"))[:900],
        "url": row.get("item_url", ""),
        "source_url": meta.get("database_page") or meta.get("source_csv_url") or "",
        "published_at": str(row.get("published_at") or "")[:10],
        "score": float(row.get("score") or 0),
        "court_level": court_level,
        "court_level_label": court_level_label,
        "court_code": court_code.upper(),
        "source_code": row.get("source_code", ""),
    }
    entry["scope"] = _case_scope(entry)
    return entry


def _case_sort_key(item: dict):
    return (
        int(item.get("court_level") or 0),
        float(item.get("score") or 0),
        str(item.get("published_at") or ""),
    )


def _law_case_columns(related_cases: list[dict], limit: int | None = 4) -> list[dict]:
    national = sorted(
        [item for item in related_cases if item.get("scope") == "national_federal"],
        key=_case_sort_key,
        reverse=True,
    )
    local = sorted(
        [item for item in related_cases if item.get("scope") == "provincial_local"],
        key=_case_sort_key,
        reverse=True,
    )
    if limit and limit > 0:
        national = national[:limit]
        local = local[:limit]
    return [
        {
            "key": "national_federal",
            "label": "国家 / 联邦法院",
            "label_en": "National / Federal Courts",
            "description": "在加拿大语境下，这里合并最高法院与联邦法院体系，代表更高或全国性权威。",
            "items": national,
            "empty_copy": "当前没有命中更高位阶或联邦体系案例。",
        },
        {
            "key": "provincial_local",
            "label": "省级 / 地方法院",
            "label_en": "Provincial / Local Courts",
            "description": "这里包含省上诉法院、高等法院、省级法院及更贴近地方实践的裁判。",
            "items": local,
            "empty_copy": "当前没有命中省级或地方层面的直接案例。",
        },
    ]


def _canada_authority_groups(results: list[dict]) -> list[dict]:
    case_entries = [_case_entry_from_row(row) for row in results if row.get("source_code") == "canlii"]
    mapping = [
        (
            "national_federal",
            "国家 / 联邦法院",
            "National / Federal Courts",
            "优先参考全国性或联邦体系判例，确认更高位阶的解释方向。",
        ),
        (
            "provincial_local",
            "省级 / 地方法院",
            "Provincial / Local Courts",
            "再看省级和地方层面的裁判，判断事实适配度和地方实践。",
        ),
        (
            "other",
            "其他机构 / Tribunal",
            "Other Tribunals",
            "这部分可作为补充材料，但通常不应先于更高法院裁判。",
        ),
    ]
    groups = []
    for key, label, label_en, description in mapping:
        items = sorted([item for item in case_entries if item.get("scope") == key], key=_case_sort_key, reverse=True)[:8]
        if not items:
            continue
        groups.append(
            {
                "key": key,
                "label": label,
                "label_en": label_en,
                "description": description,
                "description_en": "",
                "kind": "case",
                "items": items,
            }
        )
    return groups


def _law_lookup_text(row: dict) -> str:
    return " ".join(
        [
            repair_text(row.get("title")),
            plain_text_preview(row.get("summary")),
            plain_text_preview(row.get("raw_text"))[:1800],
        ]
    ).lower()


def _normalize_law_title(raw_title: str) -> str:
    title = re.sub(r"\s+", " ", repair_text(raw_title).strip(" ,.;:()[]{}"))
    lowered = title.lower()
    for marker in [" under the ", " under an ", " under a ", " under ", " pursuant to "]:
        if marker in lowered:
            title = title[lowered.rfind(marker) + len(marker):].strip()
            lowered = title.lower()
    segments = re.split(r"\s[-–—:]\s", title)
    if segments:
        tail = segments[-1].strip()
        if re.search(r"(Act|Code|Rules|Regulation(?:s)?|Charter|Convention|Order)$", tail):
            title = tail
    return title


def _related_case_entries_for_law(law_title: str, results: list[dict]) -> list[dict]:
    normalized_title = repair_text(law_title).lower()
    related = []
    seen = set()
    for row in results:
        if row.get("source_code") != "canlii":
            continue
        lookup_text = _law_lookup_text(row)
        if normalized_title not in lookup_text:
            continue
        entry = _case_entry_from_row(row)
        key = entry["title"].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        related.append(entry)
    return sorted(related, key=_case_sort_key, reverse=True)


def _law_record_from_legislation_row(row: dict, results: list[dict]) -> dict:
    meta = row.get("raw_json") or {}
    title = repair_text(row.get("title"))
    citation = repair_text(meta.get("citation") or meta.get("code") or "")
    related_cases = _related_case_entries_for_law(title, results)
    return {
        "title": title,
        "level": "Official Law",
        "level_en": "Official Law",
        "reason": "这是当前检索结果中直接命中的正式法源，适合作为当前判断的主要法律入口。",
        "reason_en": "This is a directly matched official legal source and should be reviewed first.",
        "citation": citation,
        "source_url": row.get("item_url") or meta.get("xml_url") or meta.get("source_csv_url") or "",
        "related_cases": related_cases[:8],
        "case_columns": _law_case_columns(related_cases),
    }


def _extract_canada_laws_from_results(results: list[dict]) -> list[dict]:
    mentions = Counter()
    support_map: dict[str, list[dict]] = {}
    title_map: dict[str, str] = {}

    for row in results:
        if row.get("source_code") != "canlii":
            continue
        combined_text = " ".join(
            [
                repair_text(row.get("title")),
                plain_text_preview(row.get("summary")),
                plain_text_preview(row.get("raw_text"))[:1800],
            ]
        )
        for match in _CANADA_LAW_PATTERN.findall(combined_text):
            title = _normalize_law_title(match)
            if len(title) < 8 or len(title) > 140:
                continue
            first_word = title.split(" ", 1)[0].lower()
            if first_word in {"whether", "should", "must", "could", "would", "did", "does", "is", "are", "was", "were", "not"}:
                continue
            if title.lower().endswith("charter") and "canadian" not in title.lower():
                continue
            lowered = title.lower()
            mentions[lowered] += 1
            title_map[lowered] = title
            support_map.setdefault(lowered, []).append(_case_entry_from_row(row))

    laws = []
    for lowered, count in mentions.most_common(6):
        related_cases = []
        seen = set()
        for case in sorted(support_map[lowered], key=_case_sort_key, reverse=True):
            key = case["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            related_cases.append(case)
            if len(related_cases) >= 8:
                break
        laws.append(
            {
                "title": title_map.get(lowered, lowered.title()),
                "level": "Inferred Legal Anchor",
                "level_en": "Inferred Legal Anchor",
                "reason": f"该法规名称在当前命中的案例材料中出现了 {count} 次，可作为先查法源的入口。",
                "reason_en": f"This law title appears {count} times in the matched cases and is a useful first-pass legal anchor.",
                "citation": "",
                "source_url": related_cases[0].get("source_url", "") if related_cases else "",
                "related_cases": related_cases,
                "case_columns": _law_case_columns(related_cases),
            }
        )
    return laws


def _llm_canada_law_fallback(analysis: dict) -> list[dict]:
    if not is_llm_configured():
        return []
    try:
        response = create_structured_response(
            schema_name="canada_relevant_laws",
            schema=LAW_EXTRACTION_SCHEMA,
            instructions=(
                "You are assisting with Canadian legal research triage. Based on the supplied facts, issues, relief, "
                "and keywords, list up to 5 Canadian statutes, regulations, codes, or procedural rules that should be checked first. "
                "Keep the reasons cautious and concise."
            ),
            user_input=json.dumps(analysis, ensure_ascii=False),
        )
    except LLMServiceError:
        return []

    items = []
    for item in response["data"].get("laws", []):
        title = repair_text(item.get("title"))
        if not title:
            continue
        items.append(
            {
                "title": title,
                "level": repair_text(item.get("level") or "To confirm"),
                "level_en": repair_text(item.get("level") or "To confirm"),
                "reason": repair_text(item.get("reason") or "模型建议优先核对该法律法规。"),
                "reason_en": repair_text(item.get("reason_en") or "The model suggests checking this law early."),
                "citation": "",
                "source_url": "",
                "related_cases": [],
                "case_columns": _law_case_columns([]),
            }
        )
    return items[:5]


def _build_canada_packet(analysis_result: dict, refresh: bool = False) -> dict:
    results = analysis_result.get("results", [])
    definition = MODULE_DEFINITIONS["canada"]
    graph = build_canada_result_graph(results, refresh=refresh)

    case_rows = []
    law_scope_counts: dict[int, dict[str, int]] = {}
    for row in results:
        if row.get("source_code") != "canlii":
            continue
        entry = _case_entry_from_row(row)
        linked_laws = []
        for link in graph.get("links_by_case", {}).get(int(entry["id"]), []):
            law_id = int(link["law_id"])
            scope_counts = law_scope_counts.setdefault(law_id, {"national_federal": 0, "provincial_local": 0, "other": 0})
            scope_counts[entry["scope"]] = scope_counts.get(entry["scope"], 0) + 1
            linked_laws.append(
                {
                    "law_id": law_id,
                    "title": repair_text(link.get("title")),
                    "citation": repair_text(link.get("citation")),
                    "origin": repair_text(link.get("origin") or "inferred"),
                    "source_url": repair_text(link.get("source_url")),
                    "detail_url": f"/law/canada/{repair_text(link.get('slug'))}",
                    "matched_alias": repair_text(link.get("matched_alias")),
                    "match_source": repair_text(link.get("match_source")),
                    "match_score": float(link.get("match_score") or 0),
                    "evidence_excerpt": repair_text(link.get("evidence_excerpt") or ""),
                }
            )
        entry["linked_laws"] = linked_laws
        entry["law_count"] = len(linked_laws)
        case_rows.append(entry)

    laws = []
    for law in graph.get("laws", []):
        scope_counts = law_scope_counts.get(int(law["law_id"]), {})
        laws.append(
            {
                "law_id": int(law["law_id"]),
                "title": repair_text(law.get("title")),
                "level": "Official Law" if repair_text(law.get("origin")) == "official" else "Inferred From Cases",
                "level_en": "Official Law" if repair_text(law.get("origin")) == "official" else "Inferred From Cases",
                "reason": repair_text(law.get("reason")),
                "reason_en": "",
                "citation": repair_text(law.get("citation")),
                "source_url": repair_text(law.get("source_url")),
                "detail_url": f"/law/canada/{repair_text(law.get('slug'))}",
                "linked_case_count": int(law.get("linked_case_count") or 0),
                "national_case_count": int(scope_counts.get("national_federal") or 0),
                "local_case_count": int(scope_counts.get("provincial_local") or 0),
                "origin": repair_text(law.get("origin")),
            }
        )

    if not laws:
        fallback_laws = _llm_canada_law_fallback(
            {
                "analysis": analysis_result.get("analysis", {}),
                "intake_outline": analysis_result.get("intake_outline", {}),
                "keywords": analysis_result.get("keywords", []),
            }
        )
        for law in fallback_laws:
            law["detail_url"] = ""
            law["linked_case_count"] = 0
            law["national_case_count"] = 0
            law["local_case_count"] = 0
            law["origin"] = "fallback"
        laws = fallback_laws

    return {
        "module_code": "canada",
        "module_label": definition["label"],
        "module_label_en": definition["label_en"],
        "focus_title": "相关法规与案例映射",
        "focus_title_en": "List Likely Laws First, Then Show Case-to-Law Mapping",
        "focus_copy": "页面会先收拢当前案情对应的法律法规，再逐条展示案例，并把每个案例右侧对应的法规明确列出来。点击法规后，会进入该法规的案例详情页。",
        "focus_copy_en": "The page starts with the laws most likely implicated by the input, then shows each case once with its linked laws on the right.",
        "notice": "这里不再用“关键词相近”就强行挂接法规，而是优先依据案例正文里直接出现的法规名称来建立关系；法规详情页再按国家/联邦与省级/地方拆开案例。",
        "notice_en": "",
        "relevant_laws": laws[:8],
        "case_law_rows": case_rows,
        "authority_groups": _canada_authority_groups(results),
        "transition_playbook": [],
        "suggested_questions": definition["question_prompts"],
        "suggested_questions_en": definition["question_prompts_en"],
        "agent_placeholder": definition["agent_placeholder"],
    }


def _us_sanctions_laws() -> list[dict]:
    return [
        {
            "title": "31 C.F.R. § 501.807",
            "level": "除名 / 复议程序",
            "level_en": "Delisting / Reconsideration",
            "reason": "公司如果主张列名基础已经失效、事实已经变化，通常要先核对这一条对应的 petition / reconsideration 路径。",
            "reason_en": "Use this rule when the company argues that the designation basis no longer stands or material facts have changed.",
            "citation": "31 C.F.R. § 501.807",
            "source_url": "https://ofac.treasury.gov/specially-designated-nationals-list-sdn-list/filing-a-petition-for-removal-from-an-ofac-list",
            "related_cases": [],
            "case_columns": [],
        },
        {
            "title": "31 C.F.R. § 501.801",
            "level": "许可证 / 指导意见",
            "level_en": "Licensing / Guidance",
            "reason": "如果除名前仍有交易、资金或合同履行问题，需要并行评估 specific license 或解释性指引。",
            "reason_en": "If transactions, funds, or contract-performance issues remain before delisting, assess licensing and interpretive guidance in parallel.",
            "citation": "31 C.F.R. § 501.801",
            "source_url": "https://ofac.treasury.gov/ofac-license-application-page",
            "related_cases": [],
            "case_columns": [],
        },
        {
            "title": "General Licenses and Program Authorizations",
            "level": "一般授权",
            "level_en": "General Authorization",
            "reason": "不要默认所有问题都要单独申请，应先确认现有的一般授权是否已经覆盖当前场景。",
            "reason_en": "Do not assume a bespoke application is required before checking whether a general authorization already covers the scenario.",
            "citation": "",
            "source_url": "https://ofac.treasury.gov/faqs/4",
            "related_cases": [],
            "case_columns": [],
        },
        {
            "title": "Voluntary Self-Disclosure and Remediation",
            "level": "整改与披露",
            "level_en": "Disclosure and Remediation",
            "reason": "如果存在潜在违规或协助规避制裁的风险，主动披露、控制权整改和第三方审计会直接影响后续姿态。",
            "reason_en": "If there is potential sanctions-evasion risk, voluntary disclosure, ownership remediation, and audit support can materially affect the later posture.",
            "citation": "",
            "source_url": "https://ofac.treasury.gov/disclosure",
            "related_cases": [],
            "case_columns": [],
        },
    ]


def _ofac_record_entry(row: dict) -> dict:
    meta = row.get("raw_json") or {}
    preview = _row_preview(row)
    return {
        "id": row.get("id"),
        "title": repair_text(meta.get("sdn_name") or row.get("title")),
        "title_primary": preview["title_primary"],
        "title_secondary": preview["title_secondary"],
        "summary": plain_text_preview(meta.get("remarks") or row.get("summary"))[:360],
        "summary_primary": preview["summary_primary"],
        "summary_secondary": preview["summary_secondary"],
        "subtitle": " / ".join(
            [repair_text(part) for part in [meta.get("sdn_type"), meta.get("program")] if repair_text(part)]
        ),
        "url": meta.get("official_search_url") or row.get("item_url", ""),
        "source_url": meta.get("source_csv_url") or "",
        "published_at": str(row.get("published_at") or "")[:10],
        "score": float(row.get("score") or 0),
        "kind": "ofac_record",
    }


def _build_us_sanctions_packet(analysis_result: dict) -> dict:
    results = analysis_result.get("results", [])
    ofac_rows = [row for row in results if row.get("source_code") == "ofac"]
    current_matches = [_ofac_record_entry(row) for row in ofac_rows[:8]]

    authority_groups = []
    if current_matches:
        authority_groups.append(
            {
                "key": "current_records",
                "label": "当前 OFAC 相关记录",
                "label_en": "Current OFAC Records",
                "description": "先确认是否确实命中了 OFAC 名单或相关记录，再进入除名或许可证路径分析。",
                "description_en": "Confirm the OFAC hit first before moving into delisting or licensing analysis.",
                "kind": "record",
                "items": current_matches,
            }
        )

    authority_groups.extend(
        [
            {
                "key": "delisting_path",
                "label": "除名 / 复议路径",
                "label_en": "Delisting / Reconsideration Path",
                "description": "围绕列名基础、所有权变化、控制权变化、整改证据和与 OFAC 的后续沟通来组织材料。",
                "description_en": "",
                "kind": "guide",
                "items": [
                    {
                        "title": "Verify the designation basis",
                        "title_primary": "Verify the designation basis",
                        "title_secondary": "先核实名单命中的依据",
                        "summary": "先判断是误认主体、控制权问题、交易链问题，还是协助规避制裁的事实未被充分切断。",
                        "summary_primary": "先判断是误认主体、控制权问题、交易链问题，还是协助规避制裁的事实未被充分切断。",
                        "summary_secondary": "",
                        "subtitle": "",
                        "url": "",
                        "source_url": "",
                        "published_at": "",
                    },
                    {
                        "title": "Build the remediation file",
                        "title_primary": "Build the remediation file",
                        "title_secondary": "准备整改与除名材料包",
                        "summary": "围绕股权、控制权、受益所有人、交易切断、合规重建和第三方审计证据形成完整材料。",
                        "summary_primary": "围绕股权、控制权、受益所有人、交易切断、合规重建和第三方审计证据形成完整材料。",
                        "summary_secondary": "",
                        "subtitle": "",
                        "url": "",
                        "source_url": "",
                        "published_at": "",
                    },
                ],
            },
            {
                "key": "license_path",
                "label": "许可证与指引路径",
                "label_en": "Licensing and Guidance Path",
                "description": "在除名前，如果还有受限交易、冻结资金或合同履约压力，就要并行评估许可证路径。",
                "description_en": "",
                "kind": "guide",
                "items": [
                    {
                        "title": "Check general licenses first",
                        "title_primary": "Check general licenses first",
                        "title_secondary": "先排查现成的一般授权",
                        "summary": "不要一上来就单独申请许可证，先确认 program-specific general license 是否已覆盖场景。",
                        "summary_primary": "不要一上来就单独申请许可证，先确认 program-specific general license 是否已覆盖场景。",
                        "summary_secondary": "",
                        "subtitle": "",
                        "url": "",
                        "source_url": "",
                        "published_at": "",
                    },
                    {
                        "title": "Prepare a specific license request",
                        "title_primary": "Prepare a specific license request",
                        "title_secondary": "必要时准备 specific license",
                        "summary": "如果没有现成授权，就把交易、资金流、受益所有人和控制措施解释清楚。",
                        "summary_primary": "如果没有现成授权，就把交易、资金流、受益所有人和控制措施解释清楚。",
                        "summary_secondary": "",
                        "subtitle": "",
                        "url": "",
                        "source_url": "",
                        "published_at": "",
                    },
                ],
            },
        ]
    )

    transition_playbook = [
        {
            "step": 1,
            "title": "先确认列名基础",
            "title_en": "Confirm the designation basis",
            "detail": "判断问题到底来自误认主体、控制权、资金流，还是帮助规避制裁的事实链条。",
            "detail_en": "",
        },
        {
            "step": 2,
            "title": "再做整改与证据整理",
            "title_en": "Build remediation and evidence",
            "detail": "把所有权变化、控制权切断、受益所有人、合规流程和第三方审计证据整理完整。",
            "detail_en": "",
        },
        {
            "step": 3,
            "title": "决定主路径：除名还是许可证",
            "title_en": "Choose the main path: delisting or licensing",
            "detail": "如果目标是从名单移除，应优先核对 31 C.F.R. § 501.807；如果短期还有交易需求，就并行评估 § 501.801。",
            "detail_en": "",
        },
        {
            "step": 4,
            "title": "持续回应 OFAC 补充问题",
            "title_en": "Respond to OFAC follow-ups",
            "detail": "后续补件、解释和时间管理会直接影响推进速度。",
            "detail_en": "",
        },
    ]

    definition = MODULE_DEFINITIONS["us_sanctions"]
    return {
        "module_code": "us_sanctions",
        "module_label": definition["label"],
        "module_label_en": definition["label_en"],
        "focus_title": "OFAC 规则优先，不虚构美国判例",
        "focus_title_en": "OFAC Rules First, No Invented U.S. Case Law",
        "focus_copy": "美国模块只展示本地可用的 OFAC 规则和 OFAC 记录。如果当前本地没有审判型材料，页面会明确说明，而不会伪造案例。",
        "focus_copy_en": "The U.S. module only shows locally available OFAC rules and OFAC records. If no adjudicative materials exist locally, the page says so plainly.",
        "notice": "当前仓库提供的美国材料以 OFAC 记录和程序规则为主，并不等于完整的美国法院判例库。",
        "notice_en": "",
        "relevant_laws": _us_sanctions_laws(),
        "authority_groups": authority_groups,
        "transition_playbook": transition_playbook,
        "suggested_questions": definition["question_prompts"],
        "suggested_questions_en": definition["question_prompts_en"],
        "agent_placeholder": definition["agent_placeholder"],
    }


def build_module_packet(module: str, analysis_result: dict, refresh: bool = False) -> dict:
    normalized_module = normalize_module(module)
    cache_key = (
        "module_packet",
        normalized_module,
        str(analysis_result.get("input_text") or "").strip().lower(),
        int(analysis_result.get("total") or 0),
        str(analysis_result.get("query_language") or "zh"),
    )
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    packet = _build_us_sanctions_packet(analysis_result) if normalized_module == "us_sanctions" else _build_canada_packet(analysis_result, refresh=refresh)
    _cache_set(cache_key, packet)
    return packet


def get_canada_law_detail_packet(law_slug: str, refresh: bool = False) -> dict | None:
    detail = get_canada_law_detail_data(law_slug, refresh=refresh)
    if not detail:
        return None

    law_row = detail["law"]
    case_entries = []
    for row in detail.get("cases", []):
        entry = _case_entry_from_row(row)
        entry["matched_alias"] = repair_text(row.get("matched_alias"))
        entry["match_source"] = repair_text(row.get("match_source"))
        entry["match_score"] = float(row.get("match_score") or 0)
        entry["evidence_excerpt"] = repair_text(row.get("evidence_excerpt") or "")
        case_entries.append(entry)

    columns = _law_case_columns(case_entries, limit=None)
    national_count = len([item for item in case_entries if item.get("scope") == "national_federal"])
    local_count = len([item for item in case_entries if item.get("scope") == "provincial_local"])

    return {
        "law": {
            "title": repair_text(law_row.get("title")),
            "citation": repair_text(law_row.get("citation")),
            "origin": repair_text(law_row.get("origin") or "inferred"),
            "jurisdiction": repair_text(law_row.get("jurisdiction") or "Canada"),
            "level": repair_text(law_row.get("law_level") or ""),
            "kind": repair_text(law_row.get("law_kind") or ""),
            "source_url": repair_text(law_row.get("source_url")),
            "slug": repair_text(law_row.get("slug")),
        },
        "case_columns": columns,
        "total_cases": len(case_entries),
        "national_case_count": national_count,
        "local_case_count": local_count,
        "all_cases": case_entries,
    }


def _build_canada_packet(analysis_result: dict, refresh: bool = False) -> dict:
    results = analysis_result.get("results", [])
    definition = MODULE_DEFINITIONS["canada"]
    relation_packet = build_canada_case_rule_packet(
        results,
        refresh=refresh,
        keywords=analysis_result.get("retrieval_keywords") or analysis_result.get("extracted_keywords") or [],
    )
    laws = relation_packet.get("relevant_laws", [])
    case_rows = relation_packet.get("case_law_rows", [])

    if not laws and not case_rows:
        fallback_laws = _llm_canada_law_fallback(
            {
                "analysis": analysis_result.get("analysis", {}),
                "intake_outline": analysis_result.get("intake_outline", {}),
                "keywords": analysis_result.get("keywords", []),
            }
        )
        for law in fallback_laws:
            law["detail_url"] = ""
            law["linked_case_count"] = 0
            law["national_case_count"] = 0
            law["local_case_count"] = 0
            law["country"] = "Canada"
            law["legal_type"] = law.get("level", "")
            law["article_no"] = law.get("citation", "")
            law["article_summary"] = law.get("reason", "")
            law["rule_level"] = law.get("level", "")
        laws = fallback_laws

    return {
        "module_code": "canada",
        "module_label": definition["label"],
        "module_label_en": definition["label_en"],
        "focus_title": "相关法规与案例成组映射",
        "focus_title_en": "List Likely Laws First, Then Show Case-to-Law Mapping",
        "focus_copy": "页面顶部会先收拢当前案情对应的法律法规，下面每一条结果都以“左侧案例、右侧法律法规”的配对大卡片展示，只保留已经建立明确关联的内容。",
        "focus_copy_en": "The page starts with the laws most likely implicated by the input, then shows each case once with its linked laws on the right.",
        "notice": "当前结果以 case_rule_relations 为准。没有对应法规关系的案例不会展示；点击法规后会进入该法规的详情页，并按国家 / 联邦与省级 / 地方分栏展示相关案例。",
        "notice_en": "",
        "relevant_laws": laws[:8],
        "case_law_rows": case_rows,
        "authority_groups": [],
        "transition_playbook": [],
        "suggested_questions": definition["question_prompts"],
        "suggested_questions_en": definition["question_prompts_en"],
        "agent_placeholder": definition["agent_placeholder"],
    }


def get_canada_law_detail_packet(law_slug: str, refresh: bool = False) -> dict | None:
    if refresh:
        sync_canada_legal_data(force=True)
    return get_canada_rule_detail_packet(law_slug)


def _build_reference_pack(module_packet: dict) -> dict:
    laws = []
    for law in module_packet.get("relevant_laws", [])[:3]:
        laws.append({"title": repair_text(law.get("title")), "url": law.get("detail_url") or law.get("source_url", "")})

    cases = []
    if module_packet.get("case_law_rows"):
        for item in module_packet.get("case_law_rows", [])[:4]:
            cases.append(
                {
                    "title": repair_text(item.get("title") or item.get("title_primary")),
                    "url": item.get("url", ""),
                    "label": repair_text(item.get("court_level_label") or item.get("scope")),
                }
            )
    else:
        for group in module_packet.get("authority_groups", []):
            for item in group.get("items", [])[:2]:
                cases.append(
                    {
                        "title": repair_text(item.get("title") or item.get("title_primary")),
                        "url": item.get("url", ""),
                        "label": repair_text(group.get("label")),
                    }
                )
                if len(cases) >= 4:
                    break
            if len(cases) >= 4:
                break
    return {"laws": laws, "cases": cases}


def _fallback_chat_answer(module_packet: dict, question: str, analysis_result: dict) -> dict:
    top_laws = _compact_strings([law.get("title") for law in module_packet.get("relevant_laws", [])], limit=3)
    top_groups = _compact_strings([group.get("label") for group in module_packet.get("authority_groups", [])], limit=3)
    references = _build_reference_pack(module_packet)

    answer_zh = (
        f"当前先返回基于本地检索与结构化分析的研判答案。你的问题是：{repair_text(question)}。"
        "建议先把页面里已经匹配出的法律法规看清，再对照下面分组过的案例或 OFAC 记录。"
    )
    if top_laws:
        answer_zh += f" 当前优先法源包括：{'；'.join(top_laws)}。"
    if top_groups:
        answer_zh += f" 当前支持材料已经按这些组整理：{'；'.join(top_groups)}。"

    answer_en = (
        f"This is a cached/local research answer for: {repair_text(question)}. "
        "Start with the governing laws shown on the page, then compare the grouped supporting cases or OFAC records."
    )
    if top_laws:
        answer_en += f" Priority legal anchors: {'; '.join(top_laws)}."

    return {
        "answer_zh": answer_zh,
        "answer_en": answer_en,
        "support_points_zh": [
            "答案基于当前案情拆解、已命中的本地材料以及模块规则。",
            "页面上的法规与案例分组已经按后续核查顺序排好。",
        ],
        "support_points_en": [
            "The answer is grounded in the current intake breakdown, retrieved local materials, and module rules.",
            "The page order already reflects a practical verification sequence.",
        ],
        "caution_points_zh": [
            "这条回答没有额外新增远端数据，只复用了当前检索上下文。",
            "系统只提供研究辅助，不替代正式法律意见。",
        ],
        "caution_points_en": [
            "This answer reused the current retrieval context and did not add a new remote fetch.",
            "This is research support only, not legal advice.",
        ],
        "suggested_followups_zh": module_packet.get("suggested_questions", [])[:3],
        "suggested_followups_en": module_packet.get("suggested_questions_en", [])[:3],
        "references": references,
        "model_status": "fallback",
        "model_name": "fallback",
    }


def _persist_chat_log(module_code: str, input_text: str, question: str, payload: dict):
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent_chat_logs (
                        module_code,
                        input_text,
                        question,
                        answer,
                        raw_json
                    )
                    VALUES (
                        :module_code,
                        :input_text,
                        :question,
                        :answer,
                        CAST(:raw_json AS jsonb)
                    )
                    """
                ),
                {
                    "module_code": normalize_module(module_code),
                    "input_text": repair_text(input_text),
                    "question": repair_text(question),
                    "answer": repair_text(payload.get("answer_zh") or payload.get("answer_en") or ""),
                    "raw_json": json.dumps(payload, ensure_ascii=False),
                },
            )
    except Exception:
        return


def _find_cached_chat_answer(module_code: str, input_text: str, question: str) -> dict | None:
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT raw_json
                    FROM agent_chat_logs
                    WHERE module_code = :module_code
                      AND LOWER(input_text) = LOWER(:input_text)
                      AND LOWER(question) = LOWER(:question)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "module_code": normalize_module(module_code),
                    "input_text": repair_text(input_text),
                    "question": repair_text(question),
                },
            ).mappings().all()
    except Exception:
        return None
    if not rows:
        return None
    return dict(rows[0].get("raw_json") or {})


def answer_module_question(
    *,
    module: str,
    question: str,
    analysis_result: dict,
    refresh: bool = False,
) -> dict:
    normalized_module = normalize_module(module)
    clean_question = repair_text(question)
    if not clean_question:
        return {
            "answer_zh": "",
            "answer_en": "",
            "support_points_zh": [],
            "support_points_en": [],
            "caution_points_zh": [],
            "caution_points_en": [],
            "suggested_followups_zh": [],
            "suggested_followups_en": [],
            "references": {"laws": [], "cases": []},
            "model_status": "empty",
            "model_name": "",
        }

    cached = None if refresh else _find_cached_chat_answer(normalized_module, analysis_result.get("input_text", ""), clean_question)
    if cached:
        cached["model_status"] = "history_reuse"
        cached["model_name"] = cached.get("model_name") or "history_reuse"
        if "references" not in cached:
            cached["references"] = {"laws": [], "cases": []}
        return cached

    module_packet = build_module_packet(normalized_module, analysis_result, refresh=refresh)
    references = _build_reference_pack(module_packet)
    if not is_llm_configured():
        payload = _fallback_chat_answer(module_packet, clean_question, analysis_result)
        _persist_chat_log(normalized_module, analysis_result.get("input_text", ""), clean_question, payload)
        return payload

    user_payload = json.dumps(
        {
            "module": normalized_module,
            "module_packet": module_packet,
            "analysis": analysis_result.get("analysis", {}),
            "intake_outline": analysis_result.get("intake_outline", {}),
            "bilingual_context": analysis_result.get("bilingual_context", {}),
            "question": clean_question,
        },
        ensure_ascii=False,
    )

    try:
        response = create_structured_response(
            schema_name="legal_agent_chat_response_bilingual",
            schema=CHAT_RESPONSE_SCHEMA,
            instructions=(
                "You are a bilingual legal research assistant. Answer the user's question using only the supplied case analysis, "
                "retrieved materials, and module packet. Keep the answer practical, cautious, and concrete. Do not invent laws, cases, or outcomes."
            ),
            user_input=user_payload,
        )
        payload = dict(response["data"])
        payload["model_status"] = "configured"
        payload["model_name"] = response.get("model", get_llm_model_name())
        payload["response_id"] = response.get("response_id", "")
        payload["references"] = references
    except LLMServiceError:
        payload = _fallback_chat_answer(module_packet, clean_question, analysis_result)

    _persist_chat_log(normalized_module, analysis_result.get("input_text", ""), clean_question, payload)
    return payload
