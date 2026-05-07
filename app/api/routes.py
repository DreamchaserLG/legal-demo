import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.core.config import settings
from app.service.agent_service import get_dashboard_metrics, predict_legal_outcome
from app.service.analysis_service import analyze_sentence_search
from app.service.archive_service import (
    export_source_items_snapshot,
    get_archive_status,
    rebuild_local_archive_from_db,
)
from app.service.crawler_service import sync_all_sources
from app.service.ingestion_task_service import get_ingestion_task
from app.service.legal_data_service import (
    get_canada_rule_detail_packet,
    get_case,
    get_rule,
    import_from_url,
    import_manual_entry,
    list_case_rule_relations,
    list_cases,
    list_import_tasks,
    list_rules,
    run_canada_crawler_import,
)
from app.service.module_service import (
    answer_module_question,
    get_canada_law_detail_packet,
    get_module_definition,
    get_source_options_for_module,
    normalize_module,
    resolve_source_for_module,
)
from app.service.ofac_service import sync_ofac_demo
from app.service.canlii_service import sync_canlii_demo
from app.service.common_service import looks_mojibake, repair_text
from app.service.pdf_service import PDFRenderError, render_legal_memo_pdf
from app.service.search_service import search_and_optionally_sync
from app.service.user_service import (
    authenticate_user,
    build_case_history_payload,
    build_history_display_payload,
    build_history_snapshot,
    build_user_history_graph,
    clear_session_user,
    delete_history,
    get_current_user,
    get_history,
    get_last_query,
    list_all_histories,
    list_all_users,
    list_user_histories,
    record_search_history,
    register_user,
    remember_last_query,
    require_admin,
    require_user,
    set_session_user,
    normalize_case_text,
    upsert_case_history,
    update_user_profile,
    update_user_password,
    update_user_status,
)

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_MODULE_PROFILE_OVERRIDES = {
    "canada": {
        "label": "加拿大法规与案例模块",
        "picker_label": "加拿大法规与案例",
        "subtitle": "先定位法律法规，再匹配本地案例与对应关系，适合做法规锚定、案例比对和历史分析。",
        "agent_title": "关联深度分析",
        "agent_placeholder": "例如：哪一部法规最关键？哪些地方判例更接近当前事实？",
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
    },
    "us_sanctions": {
        "label": "美国 OFAC 制裁模块",
        "picker_label": "美国 OFAC 制裁",
        "subtitle": "聚焦 OFAC 规则、程序路径和合规整改材料，不扩展到普通美国判例。",
        "agent_title": "OFAC 研判助手",
        "agent_placeholder": "例如：如果公司需要准备除名申请，材料应如何排序？",
        "source_options": [
            {
                "value": "ofac",
                "label": "OFAC 规则与记录",
                "label_en": "OFAC rules and records",
                "picker_label": "OFAC 规则与记录",
                "picker_label_en": "OFAC Rules",
            },
        ],
    },
}


def _presentation_module_profile(module: str) -> dict:
    module_code = normalize_module(module)
    profile = get_module_definition(module_code)
    profile.update(_MODULE_PROFILE_OVERRIDES.get(module_code, {}))
    return profile


def _presentation_source_options(module: str) -> list[dict]:
    profile = _presentation_module_profile(module)
    return list(profile.get("source_options") or [])


def _trim_copy(value: str, limit: int = 110) -> str:
    text_value = repair_text(value)
    if not text_value:
        return ""
    if len(text_value) <= limit:
        return text_value
    return text_value[:limit].rstrip() + "..."


def _needs_regeneration(items: list[str]) -> bool:
    cleaned = [repair_text(item) for item in items if repair_text(item)]
    if not cleaned:
        return True
    for item in cleaned:
        stripped = item.replace(" ", "")
        if looks_mojibake(item):
            return True
        if "�" in item:
            return True
        if stripped and stripped.count("?") >= max(2, len(stripped) // 2):
            return True
    return False


def _is_broken_text(value: str) -> bool:
    text_value = repair_text(value)
    if not text_value:
        return True
    stripped = text_value.replace(" ", "")
    if looks_mojibake(text_value) or "�" in text_value:
        return True
    return stripped.count("?") >= max(2, len(stripped) // 2)


def _needs_structured_line_refresh(items: list[str]) -> bool:
    cleaned = [repair_text(item) for item in items if repair_text(item)]
    if _needs_regeneration(cleaned):
        return True
    noisy = 0
    for item in cleaned:
        lowered = item.lower().strip()
        alpha_only = re.sub(r"[^a-z ]", "", lowered).strip()
        if alpha_only and len(alpha_only) <= 18 and len(alpha_only.split()) <= 3 and "《" not in item:
            noisy += 1
    return noisy >= max(2, len(cleaned))


def _clean_risk_level(value: str) -> str:
    text_value = repair_text(value).lower()
    if any(token in text_value for token in ["high", "高"]):
        return "高风险"
    if any(token in text_value for token in ["low", "低"]):
        return "低风险"
    if any(token in text_value for token in ["medium", "mid", "中"]):
        return "中风险"
    return "未知风险"


def _split_text_fragments_clean(text: str, limit: int = 5) -> list[str]:
    source = repair_text(text)
    if not source:
        return []
    chunks = re.split(r"[。！？!?\n；;]+", source)
    items = []
    for chunk in chunks:
        value = repair_text(chunk).strip(" ,，；;")
        if not value:
            continue
        items.append(value)
        if len(items) >= limit:
            break
    return items


def _fallback_key_facts_clean(item: dict) -> list[str]:
    summary = repair_text(item.get("case_summary") or item.get("analysis_summary") or "")
    query_text = repair_text(item.get("query_text") or "")
    fragments = _split_text_fragments_clean(summary, limit=3) + _split_text_fragments_clean(query_text, limit=4)
    cleaned: list[str] = []
    seen = set()
    for fragment in fragments:
        key = fragment.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(fragment)
        if len(cleaned) >= 5:
            break
    return cleaned or ["当前需要回到原始案情文本重新提炼关键事实。"]


def _fallback_dispute_focus_clean(item: dict) -> list[str]:
    focus: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type and not _is_broken_text(case_type):
        focus.append(f"{case_type}中的责任边界如何认定")
    for relation in (item.get("legal_relations") or [])[:2]:
        value = repair_text(relation)
        if value and not _is_broken_text(value):
            focus.append(value)
    for risk in (item.get("risk_points") or [])[:2]:
        risk_name = repair_text(risk.get("name"))
        if risk_name and not _is_broken_text(risk_name):
            focus.append(f"{risk_name}是否会影响最终判断")
    rules = item.get("legal_rules") or []
    if rules:
        title = repair_text(rules[0].get("title"))
        if title:
            focus.append(f"《{title}》如何适用于当前案情")
    return focus[:5] or ["当前争议焦点需要结合案情原文进一步拆解。"]


def _fallback_legal_relations_clean(item: dict) -> list[str]:
    relations: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type and not _is_broken_text(case_type):
        relations.append(f"{case_type}核心法律关系")
    for law in (item.get("legal_rules") or [])[:3]:
        title = repair_text(law.get("title"))
        if title:
            relations.append(f"与《{title}》直接相关")
    prediction_label = repair_text((item.get("prediction") or {}).get("label"))
    if prediction_label and not _is_broken_text(prediction_label):
        relations.append(f"当前预测标签：{prediction_label}")
    return relations[:5]


def _fallback_evidence_focus_clean(item: dict) -> list[str]:
    focus: list[str] = []
    risk_points = item.get("risk_points") or []
    for risk in risk_points[:2]:
        description = repair_text(risk.get("description"))
        if description:
            focus.append(description[:34])
    dispute_focus = item.get("dispute_focus") or []
    for point in dispute_focus[:2]:
        text_value = repair_text(point)
        if text_value and not _is_broken_text(text_value):
            focus.append(f"围绕“{text_value[:20]}”补强证据")
    if not focus:
        focus = ["先补齐时间线、主体关系和关键书证", "优先核对能直接支持主张的证据链"]
    return focus[:5]


def _fallback_actions_clean(item: dict) -> list[str]:
    actions: list[str] = []
    risk_level = repair_text((item.get("prediction") or {}).get("risk_level") or item.get("risk_level"))
    if risk_level == "高风险":
        actions.append("优先补强关键证据，再重新评估主张强度")
    elif risk_level == "中风险":
        actions.append("围绕争议焦点补充书面证据与时间线")
    else:
        actions.append("保留现有论证结构，继续补充针对性佐证")
    laws = item.get("legal_rules") or []
    if laws:
        title = repair_text(laws[0].get("title"))
        if title:
            actions.append(f"围绕《{title}》准备对应论证与证据")
    case_type = repair_text(item.get("case_type"))
    if "劳动" in case_type:
        actions.append("整理劳动关系、工资和考勤材料")
    elif "合同" in case_type:
        actions.append("核对合同条款、补充协议和履约凭证")
    elif "侵权" in case_type:
        actions.append("补强因果关系和损失金额证据")
    return actions[:4]


def _fallback_legal_relations(item: dict) -> list[str]:
    relations: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type:
        relations.append(f"{case_type}核心法律关系")
    for law in (item.get("legal_rules") or [])[:3]:
        title = repair_text(law.get("title"))
        if title:
            relations.append(f"与《{title}》直接相关")
    prediction_label = repair_text((item.get("prediction") or {}).get("label"))
    if prediction_label:
        relations.append(f"当前预测标签：{prediction_label}")
    return relations[:5]


def _fallback_evidence_focus(item: dict) -> list[str]:
    focus: list[str] = []
    risk_points = item.get("risk_points") or []
    for risk in risk_points[:2]:
        description = repair_text(risk.get("description"))
        if description:
            focus.append(description[:34])
    dispute_focus = item.get("dispute_focus") or []
    for point in dispute_focus[:2]:
        text_value = repair_text(point)
        if text_value:
            focus.append(f"围绕“{text_value[:20]}”补强证据")
    if not focus:
        focus = ["先补齐时间线、主体关系和关键书证", "优先核对能直接支持主张的证据链"]
    return focus[:5]


def _fallback_actions(item: dict) -> list[str]:
    actions: list[str] = []
    risk_level = repair_text((item.get("prediction") or {}).get("risk_level") or item.get("risk_level"))
    if risk_level == "高风险":
        actions.append("优先补强关键证据，再重新评估主张强度")
    elif risk_level == "中风险":
        actions.append("围绕争议焦点补充书面证据与时间线")
    else:
        actions.append("保留现有论证结构，继续补充针对性佐证")
    laws = item.get("legal_rules") or []
    if laws:
        title = repair_text(laws[0].get("title"))
        if title:
            actions.append(f"围绕《{title}》准备对应论证与证据")
    case_type = repair_text(item.get("case_type"))
    if "劳动" in case_type:
        actions.append("整理劳动关系、工资和考勤材料")
    elif "合同" in case_type:
        actions.append("核对合同条款、补充协议和履约凭证")
    elif "侵权" in case_type:
        actions.append("补强因果关系和损失金额证据")
    return actions[:4]

def _fallback_key_facts_clean(item: dict) -> list[str]:
    summary = repair_text(item.get("case_summary") or item.get("analysis_summary") or "")
    query_text = repair_text(item.get("query_text") or "")
    fragments = _split_text_fragments_clean(summary, limit=3) + _split_text_fragments_clean(query_text, limit=4)
    cleaned: list[str] = []
    seen = set()
    for fragment in fragments:
        key = fragment.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(fragment)
        if len(cleaned) >= 5:
            break
    return cleaned or ["当前需要回到原始案情文本，重新提炼关键事实。"]


def _fallback_dispute_focus_clean(item: dict) -> list[str]:
    focus: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type and not _is_broken_text(case_type):
        focus.append(f"{case_type}中的责任边界如何认定")
    for relation in (item.get("legal_relations") or [])[:2]:
        value = repair_text(relation)
        if value and not _is_broken_text(value):
            focus.append(value)
    for risk in (item.get("risk_points") or [])[:2]:
        risk_name = repair_text(risk.get("name"))
        if risk_name and not _is_broken_text(risk_name):
            focus.append(f"{risk_name}是否会影响最终判断")
    rules = item.get("legal_rules") or []
    if rules:
        title = repair_text(rules[0].get("title"))
        if title:
            focus.append(f"《{title}》如何适用于当前案情")
    return focus[:5] or ["当前争议焦点需要结合案情原文进一步拆解。"]


def _fallback_legal_relations_clean(item: dict) -> list[str]:
    relations: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type and not _is_broken_text(case_type):
        relations.append(f"{case_type}案件的核心法律关系")
    for law in (item.get("legal_rules") or [])[:3]:
        title = repair_text(law.get("title"))
        if title:
            relations.append(f"与《{title}》直接相关")
    prediction_label = repair_text((item.get("prediction") or {}).get("label"))
    if prediction_label and not _is_broken_text(prediction_label):
        relations.append(f"当前预测标签：{prediction_label}")
    return relations[:5]


def _fallback_evidence_focus_clean(item: dict) -> list[str]:
    focus: list[str] = []
    risk_points = item.get("risk_points") or []
    for risk in risk_points[:2]:
        description = repair_text(risk.get("description"))
        if description:
            focus.append(description[:34])
    dispute_focus = item.get("dispute_focus") or []
    for point in dispute_focus[:2]:
        text_value = repair_text(point)
        if text_value and not _is_broken_text(text_value):
            focus.append(f"围绕“{text_value[:20]}”补强证据")
    if not focus:
        focus = ["先补齐时间线、主体关系和关键书证", "优先核对能够直接支持主张的证据链"]
    return focus[:5]


def _fallback_actions_clean(item: dict) -> list[str]:
    actions: list[str] = []
    risk_level = repair_text((item.get("prediction") or {}).get("risk_level") or item.get("risk_level"))
    if risk_level == "高风险":
        actions.append("优先补强关键证据，再重新评估主张强度")
    elif risk_level == "中风险":
        actions.append("围绕争议焦点补充书面证据和时间线")
    else:
        actions.append("保留现有论证结构，继续补充针对性佐证")
    laws = item.get("legal_rules") or []
    if laws:
        title = repair_text(laws[0].get("title"))
        if title:
            actions.append(f"围绕《{title}》准备对应论证与证据")
    case_type = repair_text(item.get("case_type"))
    if "劳动" in case_type:
        actions.append("整理劳动关系、工资和考勤材料")
    elif "合同" in case_type:
        actions.append("核对合同条款、补充协议和履约凭证")
    elif "侵权" in case_type:
        actions.append("补强因果关系和损失金额证据")
    return actions[:4]


def _sanitize_history_display(item: dict) -> dict:
    payload = dict(item or {})
    payload["analysis_summary"] = repair_text(payload.get("analysis_summary"))
    payload["case_summary"] = repair_text(payload.get("case_summary"))
    payload["query_text"] = repair_text(payload.get("query_text"))
    key_facts = [repair_text(entry) for entry in (payload.get("key_facts") or []) if repair_text(entry)]
    dispute_focus = [repair_text(entry) for entry in (payload.get("dispute_focus") or []) if repair_text(entry)]
    legal_relations = [repair_text(entry) for entry in (payload.get("legal_relations") or []) if repair_text(entry)]
    evidence_focus = [repair_text(entry) for entry in (payload.get("evidence_focus") or []) if repair_text(entry)]
    suggested_actions = [repair_text(entry) for entry in ((payload.get("prediction") or {}).get("suggested_actions") or []) if repair_text(entry)]
    if _needs_regeneration(key_facts):
        key_facts = _fallback_key_facts_clean(payload)
    if _needs_structured_line_refresh(dispute_focus):
        dispute_focus = _fallback_dispute_focus_clean(payload)
    payload["key_facts"] = key_facts
    payload["dispute_focus"] = dispute_focus
    if _needs_structured_line_refresh(legal_relations):
        legal_relations = _fallback_legal_relations_clean(payload)
    if _needs_structured_line_refresh(evidence_focus):
        evidence_focus = _fallback_evidence_focus_clean(payload)
    if _needs_regeneration(suggested_actions):
        suggested_actions = _fallback_actions_clean(payload)
    payload["legal_relations"] = legal_relations
    payload["evidence_focus"] = evidence_focus
    payload.setdefault("prediction", {})
    payload["prediction"]["suggested_actions"] = suggested_actions
    payload["prediction"]["label"] = repair_text(payload["prediction"].get("label"))
    payload["prediction"]["conclusion"] = repair_text(payload["prediction"].get("conclusion"))
    payload["prediction"]["explanation"] = repair_text(payload["prediction"].get("explanation"))
    payload["prediction"]["risk_level"] = _clean_risk_level(payload["prediction"].get("risk_level") or payload.get("risk_level"))
    payload["risk_level"] = _clean_risk_level(payload.get("risk_level") or payload["prediction"].get("risk_level"))
    payload["module_code"] = repair_text(payload.get("module_code"))
    payload["supporting_case_groups"] = payload.get("supporting_case_groups") or []
    payload["supporting_case_rows"] = payload.get("supporting_case_rows") or []
    return payload


def _sanitize_history_graph(graph: dict) -> dict:
    payload = dict(graph or {})
    nodes = []
    for node in payload.get("nodes", []) or []:
        node_entry = dict(node)
        case_type = repair_text(node_entry.get("caseType"))
        country = repair_text(node_entry.get("country"))
        if country.lower() in {"canada", "ca"}:
            country = "加拿大"
        elif country.lower() in {"united states", "usa", "us", "u.s.", "u.s"}:
            country = "美国"
        court_level = repair_text(node_entry.get("courtLevel"))
        if not court_level or court_level == country or court_level.lower() in {"canada", "united states", "usa", "us", "u.s.", "u.s"}:
            court_level = "未标注级别"
        node_label = repair_text(node_entry.get("nodeLabel"))
        if (
            not node_label
            or looks_mojibake(node_label)
            or (re.fullmatch(r"[A-Za-z0-9 ./_-]{4,}", node_label) and len(node_label.strip()) > 4)
        ):
            node_label = case_type[:6] or _clean_risk_level(node_entry.get("riskLevel"))[:6] or "案件"
        node_entry["nodeLabel"] = node_label or "案件"
        node_entry["caseTitle"] = repair_text(node_entry.get("caseTitle")) or "未命名案件"
        node_entry["caseType"] = case_type or "综合"
        node_entry["country"] = country or "未标注国家"
        node_entry["courtLevel"] = court_level
        node_entry["riskLevel"] = _clean_risk_level(node_entry.get("riskLevel"))
        node_entry["prediction"] = repair_text(node_entry.get("prediction"))
        node_entry["laws"] = [repair_text(item) for item in (node_entry.get("laws") or []) if repair_text(item) and not looks_mojibake(item)]
        node_entry["riskPoints"] = [
            repair_text(item)
            for item in (node_entry.get("riskPoints") or [])
            if repair_text(item)
            and not looks_mojibake(item)
            and all(token not in repair_text(item) for token in ["当前结果基于", "本地案例库", "国家/联邦", "省级/地方", "不替代正式法律意见"])
        ]
        nodes.append(node_entry)
    payload["nodes"] = nodes
    return payload


def _sanitize_module_packet(module_packet: dict, module_code: str) -> dict:
    packet = dict(module_packet or {})
    if module_code == "canada":
        packet["focus_title"] = "相关法律法规与案例"
        packet["focus_copy"] = "以下内容按照法规与案例的对应关系整理展示。"
        packet["focus_copy_en"] = ""
        packet["notice"] = "案例与法规的对应关系优先依据本地已建立的 case_rule_relations，而不是只凭关键词相似强行归类。"
    elif module_code == "us_sanctions":
        packet["focus_title"] = "OFAC 规则与相关材料"
        packet["focus_copy"] = "以下内容围绕 OFAC 规则、程序路径和本地命中的相关材料整理展示。"
        packet["focus_copy_en"] = ""
        packet["notice"] = "该模块聚焦 OFAC 制裁、许可、除名和合规整改路径，不扩展到泛化美国案例。"
    laws = []
    for law in packet.get("relevant_laws", []) or []:
        law_entry = dict(law)
        law_entry["article_summary"] = _trim_copy(
            law_entry.get("article_summary")
            or law_entry.get("article_text")
            or law_entry.get("reason")
            or "",
            140,
        )
        if not law_entry["article_summary"]:
            law_title = repair_text(law_entry.get("title"))
            article_no = repair_text(law_entry.get("article_no"))
            legal_type = repair_text(law_entry.get("legal_type") or law_entry.get("rule_level"))
            law_entry["article_summary"] = _trim_copy(
                f"{law_title} {article_no} {legal_type} 的核心内容需要结合条文全文核对。".strip(),
                140,
            )
        related_cases = []
        for case in law_entry.get("related_cases", []) or []:
            case_entry = dict(case)
            case_entry["summary"] = _trim_copy(case_entry.get("summary") or case_entry.get("facts") or "", 88)
            related_cases.append(case_entry)
        law_entry["related_cases"] = related_cases[:4]
        laws.append(law_entry)
    packet["relevant_laws"] = laws
    case_rows = []
    for case in packet.get("case_law_rows", []) or []:
        case_entry = dict(case)
        case_entry["summary"] = _trim_copy(case_entry.get("summary") or case_entry.get("facts") or "", 180)
        rules = []
        for law in case_entry.get("rules", []) or []:
            law_row = dict(law)
            law_row["article_summary"] = _trim_copy(law_row.get("article_summary") or "", 96)
            rules.append(law_row)
        case_entry["rules"] = rules
        case_rows.append(case_entry)
    packet["case_law_rows"] = case_rows
    packet["module_label"] = _presentation_module_profile(module_code).get("label")
    return packet


def _sanitize_prediction_payload(payload: dict, module_code: str) -> dict:
    clean_payload = dict(payload or {})
    clean_payload["module_packet"] = _sanitize_module_packet(clean_payload.get("module_packet") or {}, module_code)
    if isinstance(clean_payload.get("prediction"), dict):
        prediction = dict(clean_payload.get("prediction") or {})
        support_groups = []
        for group in prediction.get("supporting_case_groups", []) or []:
            group_entry = dict(group)
            group_entry["article_summary"] = _trim_copy(group_entry.get("article_summary") or "", 120)
            cases = []
            for case in group_entry.get("cases", []) or []:
                case_entry = dict(case)
                case_entry["summary"] = _trim_copy(case_entry.get("summary") or "", 120)
                case_entry["match_reason"] = _trim_copy(case_entry.get("match_reason") or "", 96)
                cases.append(case_entry)
            group_entry["cases"] = cases
            support_groups.append(group_entry)
        prediction["supporting_case_groups"] = support_groups
        linked_laws = []
        for law in prediction.get("linked_laws", []) or []:
            law_entry = dict(law)
            law_entry["article_summary"] = _trim_copy(law_entry.get("article_summary") or "", 110)
            linked_laws.append(law_entry)
        prediction["linked_laws"] = linked_laws
        clean_payload["prediction"] = prediction
    return clean_payload


class ChatRequest(BaseModel):
    module: str = "canada"
    text: str
    question: str
    limit: int = settings.default_search_limit
    offset: int = 0
    source: str = "all"
    sort: str = "relevance"
    refresh: bool = False


class AuthPayload(BaseModel):
    username: str = ""
    email: str = ""
    password: str
    confirmPassword: str = ""


class LoginPayload(BaseModel):
    login: str = ""
    username: str = ""
    email: str = ""
    password: str


class AnalyzePayload(BaseModel):
    text: str
    limit: int = settings.default_search_limit
    offset: int = 0
    source: str = "all"
    sort: str = "relevance"
    module: str = "canada"
    refresh: bool = False


class ProfilePayload(BaseModel):
    email: str = ""
    phone: str = ""
    organization: str = ""
    real_name: str = ""
    country_preference: str = ""
    legal_type_preference: str = ""
    note: str = ""


class PasswordPayload(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class UserStatusPayload(BaseModel):
    status: str


class ImportUrlPayload(BaseModel):
    source_url: str
    country: str
    data_type: str
    legal_type: str = ""
    case_type: str = ""
    court_level: str = ""
    auto_link: bool = False


class ImportManualPayload(BaseModel):
    data_type: str
    country: str
    title: str
    legal_type: str = ""
    case_type: str = ""
    court_name: str = ""
    court_level: str = ""
    summary: str = ""
    facts: str = ""
    judgment_result: str = ""
    article_no: str = ""
    article_text: str = ""
    article_summary: str = ""
    source_url: str = ""
    auto_link: bool = False


def _base_context(request: Request, page_id: str) -> dict:
    metrics = get_dashboard_metrics()
    module_options = [
        {"value": "canada", **_presentation_module_profile("canada")},
        {"value": "us_sanctions", **_presentation_module_profile("us_sanctions")},
    ]
    current_user = get_current_user(request)
    return {
        "request": request,
        "app_name": settings.app_name,
        "page_id": page_id,
        "dashboard": metrics,
        "module_options": module_options,
        "module_source_map": {item["value"]: item.get("source_options", []) for item in module_options},
        "current_user": current_user,
        "last_query": get_last_query(request),
        "page_notice": str(request.query_params.get("message") or "").strip(),
    }


def _to_bool(value) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _build_query_string(**kwargs) -> str:
    clean = {key: value for key, value in kwargs.items() if value not in {None, ""}}
    return urlencode(clean)


def _landing_url_for_user(user: dict | None) -> str:
    if user and user.get("is_admin"):
        return "/admin"
    return "/home"


def _normalize_next_url(next_url: str | None, user: dict | None = None) -> str:
    clean = str(next_url or "").strip()
    fallback = _landing_url_for_user(user)
    if not clean or clean == "/" or not clean.startswith("/") or clean.startswith("//"):
        return fallback
    if clean.startswith(("/login", "/register", "/auth/login", "/auth/register", "/api/auth/")):
        return fallback
    if user and not user.get("is_admin") and clean.startswith("/admin"):
        return "/home?message=" + quote("当前账号无权限访问该页面")
    return clean


def _login_redirect(next_url: str = "/home", message: str = "登录状态已过期，请重新登录") -> RedirectResponse:
    query = _build_query_string(next=_normalize_next_url(next_url), message=message)
    return RedirectResponse(url=f"/login?{query}", status_code=303)


def _forbidden_redirect() -> RedirectResponse:
    return RedirectResponse(url="/home?message=" + quote("当前账号无权限访问该页面"), status_code=303)


def _require_page_user(request: Request, next_url: str) -> dict | RedirectResponse:
    try:
        return require_user(request)
    except HTTPException as exc:
        return _login_redirect(next_url=next_url, message=str(exc.detail))


def _require_page_admin(request: Request, next_url: str) -> dict | RedirectResponse:
    page_user = _require_page_user(request, next_url)
    if isinstance(page_user, RedirectResponse):
        return page_user
    if not page_user.get("is_admin"):
        return _forbidden_redirect()
    return page_user


def _cache_safe_template(template_name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    response = templates.TemplateResponse(template_name, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _memo_url(text: str, limit: int, offset: int, source: str, sort: str, module: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return f"/memo?{query}"


def _analysis_url(text: str, limit: int, offset: int, source: str, sort: str, module: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return f"/analysis?{query}"


def _predict_url(text: str, limit: int, offset: int, source: str, sort: str, module: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return f"/predict?{query}"


def _memo_download_url(text: str, limit: int, offset: int, source: str, sort: str, module: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return f"/memo/download?{query}"


def _task_context(remote_fetch: dict | None, refresh_url: str) -> dict:
    task = (remote_fetch or {}).get("task") if isinstance(remote_fetch, dict) else None
    return {
        "ingestion_task": task,
        "task_refresh_url": refresh_url,
        "task_poll_ms": max(1000, int(getattr(settings, "ingestion_page_poll_seconds", 3000))),
    }


def _remember_query(
    request: Request,
    *,
    query_text: str,
    restore_url: str,
):
    remember_last_query(request, restore_url, query_text[:80])


def _save_case_history_if_possible(
    request: Request,
    *,
    query_text: str,
    module_code: str,
    result_payload: dict,
) -> int:
    user = get_current_user(request)
    if not user or not query_text.strip():
        return 0
    payload = build_case_history_payload(
        query_text=query_text,
        analysis_payload=result_payload,
        prediction_payload=result_payload.get("prediction") or {},
        module_packet=result_payload.get("module_packet") or {},
        module_code=module_code,
    )
    history_id = upsert_case_history(user_id=int(user["id"]), payload=payload)
    if history_id:
        remember_last_query(request, f"/histories/{history_id}", payload.get("case_title") or query_text[:80])
    return history_id


def _matching_history_id_for_query(*, user_id: int, query_text: str, module_code: str) -> int:
    normalized_query = normalize_case_text(query_text)
    if not normalized_query:
        return 0
    country = "United States" if normalize_module(module_code) == "us_sanctions" else "Canada"
    for item in list_user_histories(user_id=user_id, country=country, limit=200):
        candidate = normalize_case_text(item.get("query_text") or "")
        if candidate == normalized_query:
            try:
                return int(item.get("id") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _exact_history_display_for_query(*, user_id: int, query_text: str, module_code: str) -> tuple[int, dict | None]:
    history_id = _matching_history_id_for_query(user_id=user_id, query_text=query_text, module_code=module_code)
    if not history_id:
        return 0, None
    history = get_history(history_id, user_id=user_id, admin=False, touch=False)
    if not history:
        return 0, None
    return history_id, _sanitize_history_display(build_history_display_payload(history))


def _build_analysis_view_payload(
    *,
    query_text: str,
    module_code: str,
    result_payload: dict,
    history_id: int = 0,
    user_id: int | None = None,
) -> dict:
    if history_id and user_id:
        history = get_history(history_id, user_id=int(user_id), admin=False, touch=False)
        if history:
            return build_history_display_payload(history)

    synthetic = build_case_history_payload(
        query_text=query_text,
        analysis_payload=result_payload,
        prediction_payload=result_payload.get("prediction") or {},
        module_packet=result_payload.get("module_packet") or {},
        module_code=module_code,
    )
    synthetic_row = {
        **synthetic,
        "id": history_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_viewed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "view_count": 1,
    }
    return build_history_display_payload(synthetic_row)


def _history_case_columns_from_display(history_item: dict, law: dict) -> list[dict]:
    rule_id = int(law.get("rule_id") or 0) if str(law.get("rule_id") or "").isdigit() else 0
    title = repair_text(law.get("title"))
    national_items: list[dict] = []
    local_items: list[dict] = []
    for case in history_item.get("supporting_case_rows") or []:
        matched = False
        for linked_law in case.get("rules") or []:
            linked_rule_id = int(linked_law.get("rule_id") or 0) if str(linked_law.get("rule_id") or "").isdigit() else 0
            linked_title = repair_text(linked_law.get("title"))
            if (rule_id and linked_rule_id == rule_id) or (title and linked_title == title):
                matched = True
                break
        if not matched:
            continue
        case_item = {
            "title": repair_text(case.get("title")),
            "court_level": repair_text(case.get("court_level")),
            "case_type": repair_text(case.get("case_type")),
            "source_url": repair_text(case.get("source_url")),
        }
        scope = repair_text(case.get("scope"))
        if scope == "national_federal":
            national_items.append(case_item)
        else:
            local_items.append(case_item)
    return [
        {"key": "national_federal", "label": "国家 / 联邦", "items": national_items[:4]},
        {"key": "provincial_local", "label": "地区 / 地方", "items": local_items[:4]},
    ]


def _history_module_packet_from_display(history_item: dict, module_code: str) -> dict:
    module_profile = _presentation_module_profile(module_code)
    relevant_laws = []
    for law in history_item.get("legal_rules") or []:
        relevant_laws.append(
            {
                "rule_id": law.get("rule_id"),
                "title": repair_text(law.get("title")),
                "article_no": repair_text(law.get("article_no")),
                "article_summary": repair_text(law.get("summary")),
                "country": repair_text(law.get("country")),
                "legal_type": repair_text(law.get("legal_type")),
                "detail_url": repair_text(law.get("detail_url")),
                "source_url": repair_text(law.get("source_url")),
                "linked_case_count": sum(
                    1
                    for case in (history_item.get("supporting_case_rows") or [])
                    if any(
                        (
                            str(rule.get("rule_id") or "") == str(law.get("rule_id") or "")
                            and str(law.get("rule_id") or "")
                        )
                        or repair_text(rule.get("title")) == repair_text(law.get("title"))
                        for rule in (case.get("rules") or [])
                    )
                ),
                "case_columns": _history_case_columns_from_display(history_item, law),
            }
        )
    packet = {
        "module_label": module_profile.get("label"),
        "module_label_en": "",
        "focus_title": "",
        "focus_copy": "",
        "notice": "",
        "relevant_laws": relevant_laws,
        "case_law_rows": history_item.get("supporting_case_rows") or [],
    }
    return _sanitize_module_packet(packet, module_code)


def _history_prediction_process_from_display(history_item: dict) -> list[dict]:
    laws = history_item.get("legal_rules") or []
    cases = history_item.get("supporting_case_rows") or []
    dispute_focus = history_item.get("dispute_focus") or []
    return [
        {
            "kicker": "事实梳理",
            "title": "先确认案情基础",
            "detail": history_item.get("case_summary") or history_item.get("query_text") or "当前记录缺少完整案情摘要。",
            "status": "已复用历史记录中的结构化分析。",
        },
        {
            "kicker": "争议定位",
            "title": "提炼影响结果的核心争点",
            "detail": "；".join(dispute_focus[:3]) or "当前记录没有额外保存争议焦点，建议回看案情原文。",
            "status": "按历史记录中的争议焦点继续展示。",
        },
        {
            "kicker": "法规与案例",
            "title": "核对法规依据与支撑案例",
            "detail": f"已关联法规 {len(laws)} 条，相关案例 {len(cases)} 条。系统继续按法规与案例的对应关系展示。",
            "status": "全部材料来自本地已保存记录。",
        },
        {
            "kicker": "综合判断",
            "title": "输出当前预测结论",
            "detail": history_item.get("prediction", {}).get("explanation") or history_item.get("analysis_summary") or "当前没有保存完整结论说明。",
            "status": "本次页面未重新调用模型。",
        },
    ]


def _prediction_payload_from_history_display(history_item: dict, module_code: str) -> dict:
    keywords = []
    if history_item.get("case_type"):
        keywords.append(history_item["case_type"])
    for law in history_item.get("legal_rules") or []:
        title = repair_text(law.get("title"))
        if title and title not in keywords:
            keywords.append(title)
        if len(keywords) >= 6:
            break
    prediction = history_item.get("prediction") or {}
    return {
        "analysis": {
            "jurisdiction": history_item.get("court_level") or history_item.get("country") or "",
            "summary": history_item.get("analysis_summary") or history_item.get("case_summary") or "",
        },
        "intake_outline": {
            "facts": history_item.get("case_summary") or history_item.get("query_text") or "",
            "disputed_issues": history_item.get("dispute_focus") or [],
            "requested_relief": "",
            "keywords": keywords,
        },
        "bilingual_context": {
            "facts": {"zh": history_item.get("case_summary") or history_item.get("query_text") or "", "en": ""},
            "disputed_issues": {"zh": history_item.get("dispute_focus") or [], "en": []},
            "requested_relief": {"zh": "", "en": ""},
            "keywords": {"zh": keywords, "en": []},
        },
        "prediction": {
            "label": repair_text(prediction.get("label")) or "综合判断",
            "predicted_outcome": repair_text(prediction.get("conclusion")) or repair_text(prediction.get("label")) or "当前没有稳定预测结论。",
            "likely_prevailing_party": repair_text(prediction.get("label")) or "综合判断",
            "confidence": float(prediction.get("confidence") or 0),
            "confidence_percent": int(round(float(prediction.get("confidence") or 0) * 100)),
            "reasoning": repair_text(prediction.get("explanation")) or history_item.get("analysis_summary") or history_item.get("case_summary") or "",
            "reason_points": history_item.get("key_facts") or history_item.get("legal_relations") or [],
            "risk_points": [repair_text(item.get("description") or item.get("name")) for item in (history_item.get("risk_points") or []) if repair_text(item.get("description") or item.get("name"))],
            "risk_level": repair_text(prediction.get("risk_level")) or history_item.get("risk_level") or "未知风险",
            "suggested_actions": prediction.get("suggested_actions") or [],
            "prediction_process": _history_prediction_process_from_display(history_item),
            "jurisdiction": history_item.get("court_level") or history_item.get("country") or "",
            "requested_relief": "",
            "support_case_count": len(history_item.get("supporting_case_rows") or []),
            "execution_summary": [
                "当前页面直接复用已保存的预测结果。",
                "未重新发起新的模型推理调用。",
                "支撑法规与案例继续按本地记录展示。",
            ],
            "model_status": "history_reuse",
            "model_name": "历史结果复用",
            "model_error": "",
            "supporting_case_groups": history_item.get("supporting_case_groups") or [],
            "linked_laws": history_item.get("legal_rules") or [],
            "bilingual": {
                "predicted_outcome_zh": repair_text(prediction.get("conclusion")) or repair_text(prediction.get("label")) or "当前没有稳定预测结论。",
                "predicted_outcome_en": "",
                "reasoning_zh": repair_text(prediction.get("explanation")) or history_item.get("analysis_summary") or "",
                "reasoning_en": "",
                "likely_prevailing_party_zh": repair_text(prediction.get("label")) or "综合判断",
                "likely_prevailing_party_en": "",
            },
        },
        "module_packet": _history_module_packet_from_display(history_item, module_code),
        "history_reused": True,
        "remote_fetch": {"status": "history_reuse", "message": "当前页面直接复用已保存的预测结果。"},
        "coverage_note": "当前结果直接来自已保存历史，不会重新调用模型。",
        "retrieval_summary": {
            "keywords": keywords[:6],
            "law_count": len(history_item.get("legal_rules") or []),
            "case_count": len(history_item.get("supporting_case_rows") or []),
        },
    }


def _normalize_filename_text(value: str, max_length: int = 56) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    raw = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9\\s-]", " ", raw)
    raw = re.sub(r"\s+", "-", raw).strip(" .-_")
    if len(raw) > max_length:
        raw = raw[:max_length].rstrip(" .-_")
    return raw or "case"


def _memo_download_filenames(context: dict) -> tuple[str, str]:
    prediction_result = context.get("prediction_result") or {}
    analysis = prediction_result.get("analysis", {}) if isinstance(prediction_result, dict) else {}
    summary = (
        analysis.get("summary")
        or prediction_result.get("input_text")
        or context.get("text_input")
        or ""
    )
    summary_part = _normalize_filename_text(summary)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ascii_name = f"legal-memo-{timestamp}.pdf"
    pretty_name = f"legal-memo-{summary_part}-{timestamp}.pdf"
    return ascii_name, pretty_name


def _memo_context_payload(text: str, limit: int, offset: int, source: str, sort: str, module: str) -> dict:
    module_code = normalize_module(module)
    effective_source = resolve_source_for_module(module_code, source)
    payload = {
        "text_input": text,
        "limit": limit,
        "offset": offset,
        "source": effective_source,
        "sort": sort,
        "module_code": module_code,
        "module_profile": _presentation_module_profile(module_code),
        "source_options": _presentation_source_options(module_code),
        "prediction_result": None,
        "analysis_url": _analysis_url(text, limit, offset, effective_source, sort, module_code) if text.strip() else "/analysis",
        "predict_url": _predict_url(text, limit, offset, effective_source, sort, module_code) if text.strip() else "/predict",
        "memo_download_url": _memo_download_url(text, limit, offset, effective_source, sort, module_code) if text.strip() else "",
        "memo_date": datetime.now().strftime("%Y-%m-%d"),
    }
    if text.strip():
        payload["prediction_result"] = _sanitize_prediction_payload(
            predict_legal_outcome(
            text=text,
            limit=limit,
            offset=offset,
            source=effective_source,
            sort=sort,
            module=module_code,
            ),
            module_code,
        )
    return payload


@router.get("/", response_class=HTMLResponse)
def root_page(request: Request):
    context = _base_context(request, "login")
    context.update({"next_url": "/", "auth_error": "", "login_value": "", "page_notice": ""})
    return _cache_safe_template("login.html", context)


@router.get("/home", response_class=HTMLResponse)
def index(request: Request):
    page_user = _require_page_user(request, "/home")
    if isinstance(page_user, RedirectResponse):
        return page_user
    context = _base_context(request, "home")
    context.update(
        {
            "module_code": "canada",
            "module_profile": _presentation_module_profile("canada"),
            "source_options": _presentation_source_options("canada"),
        }
    )
    return _cache_safe_template("index.html", context)


@router.get("/results", response_class=HTMLResponse)
def results_page(
    request: Request,
    keywords: str = Query(""),
    sync_first: str | None = Query(None),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    page_user = _require_page_user(
        request,
        f"/results?{_build_query_string(keywords=keywords, limit=limit, offset=offset, source=source, sort=sort, module=module, refresh='1' if refresh else None)}",
    )
    if isinstance(page_user, RedirectResponse):
        return page_user
    module_code = normalize_module(module)
    effective_source = resolve_source_for_module(module_code, source)
    result = search_and_optionally_sync(
        keywords_input=keywords,
        sync_first=_to_bool(sync_first),
        limit=limit,
        offset=offset,
        source=effective_source,
        sort=sort,
        module=module_code,
        refresh=refresh,
        origin_page="search",
    )

    context = _base_context(request, "search")
    context.update(result)
    context.update({
        "module_code": module_code,
        "module_profile": _presentation_module_profile(module_code),
        "source_options": _presentation_source_options(module_code),
        "source": effective_source,
    })
    context.update(
        _task_context(
            result.get("remote_fetch"),
            refresh_url=f"/results?{_build_query_string(keywords=keywords, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code, refresh='1')}",
        )
    )
    if keywords.strip():
        _remember_query(
            request,
            query_text=keywords,
            restore_url=f"/results?{_build_query_string(keywords=keywords, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code)}",
        )
    return _cache_safe_template("results.html", context)


def _render_prediction_page(
    *,
    request: Request,
    page_id: str,
    text: str,
    draft: str,
    limit: int,
    offset: int,
    source: str,
    sort: str,
    module: str,
    refresh: bool,
):
    active_text = text.strip()
    restore_text = active_text or draft
    page_path = "/analysis" if page_id == "analysis" else "/predict"
    next_query = _build_query_string(
        text=active_text if active_text else None,
        draft=draft if draft.strip() and not active_text else None,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    page_user = _require_page_user(
        request,
        f"{page_path}?{next_query}" if next_query else page_path,
    )
    if isinstance(page_user, RedirectResponse):
        return page_user
    module_code = normalize_module(module)
    effective_source = resolve_source_for_module(module_code, source)
    context = _base_context(request, page_id)
    context.update(
        {
            "text_input": restore_text,
            "submitted_text": active_text,
            "draft_text": draft,
            "limit": limit,
            "offset": offset,
            "source": effective_source,
            "sort": sort,
            "module_code": module_code,
            "module_profile": _presentation_module_profile(module_code),
            "source_options": _presentation_source_options(module_code),
            "refresh": refresh,
            "analysis_result": None,
            "analysis_view": None,
            "prediction_result": None,
            "analysis_url": _analysis_url(active_text, limit, offset, effective_source, sort, module_code) if active_text else "/analysis",
            "predict_url": _predict_url(active_text, limit, offset, effective_source, sort, module_code) if active_text else "/predict",
            "memo_download_url": _memo_download_url(active_text, limit, offset, effective_source, sort, module_code) if active_text else "",
            "history_id": 0,
        }
    )
    if active_text:
        if not refresh:
            existing_history_id, existing_history_item = _exact_history_display_for_query(
                user_id=int(page_user["id"]),
                query_text=active_text,
                module_code=module_code,
            )
            if existing_history_id and existing_history_item:
                if page_id == "analysis":
                    _remember_query(
                        request,
                        query_text=active_text,
                        restore_url=f"/histories/{existing_history_id}",
                    )
                    return RedirectResponse(url=f"/histories/{existing_history_id}", status_code=303)
                prediction_payload = _sanitize_prediction_payload(
                    _prediction_payload_from_history_display(existing_history_item, module_code),
                    module_code,
                )
                context["history_id"] = existing_history_id
                context["prediction_result"] = prediction_payload
                context["analysis_result"] = prediction_payload
                context["analysis_view"] = existing_history_item
                _remember_query(
                    request,
                    query_text=active_text,
                    restore_url=f"{page_path}?{_build_query_string(text=active_text, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code)}",
                )
                return _cache_safe_template("predict.html", context)
        existing_history_id = 0
        prediction_payload = _sanitize_prediction_payload(
            predict_legal_outcome(
            text=active_text,
            limit=limit,
            offset=offset,
            source=effective_source,
            sort=sort,
            module=module_code,
            refresh=refresh,
            ),
            module_code,
        )
        context["prediction_result"] = prediction_payload
        context["analysis_result"] = prediction_payload
        context.update(
            _task_context(
                prediction_payload.get("remote_fetch"),
                refresh_url=_predict_url(active_text, limit, offset, effective_source, sort, module_code, refresh=True),
            )
        )
        context["history_id"] = _save_case_history_if_possible(
            request,
            query_text=active_text,
            module_code=module_code,
            result_payload=prediction_payload,
        )
        if page_id == "analysis" and context["history_id"] and prediction_payload.get("history_reused"):
            return RedirectResponse(url=f"/histories/{context['history_id']}", status_code=303)
        context["analysis_view"] = _sanitize_history_display(_build_analysis_view_payload(
            query_text=active_text,
            module_code=module_code,
            result_payload=prediction_payload,
            history_id=context["history_id"],
            user_id=int(page_user["id"]),
        ))
        if page_id == "predict":
            context["prediction_result"] = prediction_payload
        _remember_query(
            request,
            query_text=active_text,
            restore_url=f"{page_path}?{_build_query_string(text=active_text, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code)}",
        )
    template_name = "predict.html" if page_id == "predict" else "analyze.html"
    return _cache_safe_template(template_name, context)


@router.get("/analysis", response_class=HTMLResponse)
def analysis_page(
    request: Request,
    text: str = Query(""),
    draft: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    return _render_prediction_page(
        request=request,
        page_id="analysis",
        text=text,
        draft=draft,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh=refresh,
    )


@router.get("/analyze", response_class=HTMLResponse)
def analyze_page_redirect(
    text: str = Query(""),
    draft: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    redirect_query = _build_query_string(
        text=text if text.strip() else None,
        draft=draft if draft.strip() and not text.strip() else None,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return RedirectResponse(
        url=f"/analysis?{redirect_query}" if redirect_query else "/analysis",
        status_code=307,
    )


@router.get("/predict", response_class=HTMLResponse)
def predict_page(
    request: Request,
    text: str = Query(""),
    draft: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    return _render_prediction_page(
        request=request,
        page_id="predict",
        text=text,
        draft=draft,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh=refresh,
    )


@router.get("/law/canada/{law_slug}", response_class=HTMLResponse)
def canada_law_detail_page(
    request: Request,
    law_slug: str,
    refresh: bool = Query(False),
):
    page_user = _require_page_user(request, f"/law/canada/{quote(law_slug)}")
    if isinstance(page_user, RedirectResponse):
        return page_user
    detail = get_canada_law_detail_packet(law_slug, refresh=refresh)
    if not detail:
        raise HTTPException(status_code=404, detail="law detail not found")

    context = _base_context(request, "law_detail")
    context.update(
        {
            "module_code": "canada",
            "module_profile": _presentation_module_profile("canada"),
            "source_options": _presentation_source_options("canada"),
            "law_detail": detail,
            "refresh": refresh,
        }
    )
    return _cache_safe_template("law_detail.html", context)


@router.get("/memo", response_class=HTMLResponse)
def memo_page(
    request: Request,
    text: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
):
    page_user = _require_page_user(request, _memo_url(text, limit, offset, source, sort, module))
    if isinstance(page_user, RedirectResponse):
        return page_user
    return RedirectResponse(
        url=_analysis_url(text, limit, offset, resolve_source_for_module(normalize_module(module), source), sort, normalize_module(module)),
        status_code=307,
    )


@router.get("/memo/download")
def memo_download(
    request: Request,
    text: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
):
    page_user = _require_page_user(request, _memo_download_url(text, limit, offset, source, sort, module))
    if isinstance(page_user, RedirectResponse):
        return page_user
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required for memo PDF export.")

    context = _base_context(request, "memo")
    context.update(_memo_context_payload(text, limit, offset, source, sort, normalize_module(module)))
    try:
        pdf_bytes = render_legal_memo_pdf(context)
    except PDFRenderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    ascii_name, pretty_name = _memo_download_filenames(context)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(pretty_name)}"
            )
        },
    )


@router.post("/search")
async def search_redirect(request: Request):
    page_user = _require_page_user(request, "/home")
    if isinstance(page_user, RedirectResponse):
        return page_user
    form = await request.form()
    query = urlencode(
        {
            "keywords": form.get("keywords", ""),
            "sync_first": "on" if form.get("sync_first") else "",
            "limit": form.get("limit", settings.default_search_limit),
            "offset": form.get("offset", 0),
            "source": form.get("source", "all"),
            "sort": form.get("sort", "relevance"),
            "module": form.get("module", "canada"),
        }
    )
    return RedirectResponse(url=f"/results?{query}", status_code=303)


@router.get("/api/search")
def api_search(
    request: Request,
    keywords: str = Query(...),
    sync_first: bool = Query(False),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    require_user(request)
    return search_and_optionally_sync(
        keywords_input=keywords,
        sync_first=sync_first,
        limit=limit,
        offset=offset,
        source=resolve_source_for_module(normalize_module(module), source),
        sort=sort,
        module=normalize_module(module),
        refresh=refresh,
        origin_page="search",
    )


@router.get("/api/analyze-search")
def api_analyze_search(
    request: Request,
    text: str = Query(...),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    require_user(request)
    return analyze_sentence_search(
        text=text,
        limit=limit,
        offset=offset,
        source=resolve_source_for_module(normalize_module(module), source),
        sort=sort,
        module=normalize_module(module),
        refresh=refresh,
        origin_page="analyze",
        local_only=True,
    )


@router.get("/api/predict")
def api_predict(
    request: Request,
    text: str = Query(...),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    require_user(request)
    return _sanitize_prediction_payload(
        predict_legal_outcome(
        text=text,
        limit=limit,
        offset=offset,
        source=resolve_source_for_module(normalize_module(module), source),
        sort=sort,
        module=normalize_module(module),
        refresh=refresh,
        ),
        normalize_module(module),
    )


@router.post("/api/agent-chat")
def api_agent_chat(request: Request, payload: ChatRequest):
    require_user(request)
    module_code = normalize_module(payload.module)
    effective_source = resolve_source_for_module(module_code, payload.source)
    analysis_result = analyze_sentence_search(
        text=payload.text,
        limit=payload.limit,
        offset=payload.offset,
        source=effective_source,
        sort=payload.sort,
        module=module_code,
        refresh=payload.refresh,
        origin_page="analyze",
        local_only=True,
    )
    answer = answer_module_question(
        module=module_code,
        question=payload.question,
        analysis_result=analysis_result,
        refresh=payload.refresh,
    )
    return {
        "module_code": module_code,
        "module_profile": _presentation_module_profile(module_code),
        "question": payload.question,
        "answer": answer,
        "analysis_result": {
            "input_text": analysis_result.get("input_text", ""),
            "analysis_mode": analysis_result.get("analysis_mode", ""),
            "intake_outline": analysis_result.get("intake_outline", {}),
            "module_packet": _sanitize_module_packet(analysis_result.get("module_packet", {}), module_code),
        },
    }


@router.get("/api/ingestion-tasks/{task_id}")
def api_ingestion_task(request: Request, task_id: int):
    require_user(request)
    task = get_ingestion_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="ingestion task not found")
    return task


@router.get("/api/archive/status")
def api_archive_status(request: Request):
    require_admin(request)
    return get_archive_status()


@router.post("/api/archive/export")
def api_archive_export(request: Request, source: str = Query("all")):
    require_admin(request)
    return export_source_items_snapshot(source_filter=source)


@router.post("/api/archive/rebuild")
def api_archive_rebuild(request: Request, source: str = Query("all")):
    require_admin(request)
    return rebuild_local_archive_from_db(source_filter=source)


@router.post("/api/sync/ofac")
def api_sync_ofac(request: Request):
    require_admin(request)
    return sync_ofac_demo()


@router.post("/api/sync/canlii")
def api_sync_canlii(request: Request):
    require_admin(request)
    return sync_canlii_demo()


@router.post("/api/sync/all")
def api_sync_all(request: Request):
    require_admin(request)
    return sync_all_sources()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query("/"), message: str = Query("")):
    context = _base_context(request, "login")
    context.update(
        {
            "next_url": _normalize_next_url(next),
            "auth_error": str(message or "").strip(),
            "login_value": "",
            "page_notice": "",
        }
    )
    return _cache_safe_template("login.html", context)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, next: str = Query("/"), message: str = Query("")):
    context = _base_context(request, "register")
    context.update(
        {
            "next_url": _normalize_next_url(next),
            "auth_error": str(message or "").strip(),
            "form_values": {},
            "page_notice": "",
        }
    )
    return _cache_safe_template("register.html", context)


@router.post("/auth/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    login: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    try:
        user = authenticate_user(login, password)
        set_session_user(request, user)
        return RedirectResponse(url=_normalize_next_url(next, user), status_code=303)
    except HTTPException as exc:
        context = _base_context(request, "login")
        context.update(
            {
                "next_url": _normalize_next_url(next),
                "auth_error": str(exc.detail),
                "login_value": login,
                "page_notice": "",
            }
        )
        return _cache_safe_template("login.html", context, status_code=exc.status_code)


@router.post("/auth/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    username: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    next: str = Form("/"),
):
    if password != confirm_password:
        context = _base_context(request, "register")
        context.update(
            {
                "next_url": _normalize_next_url(next),
                "auth_error": "两次输入的密码不一致。",
                "form_values": {"username": username, "email": email},
                "page_notice": "",
            }
        )
        return _cache_safe_template("register.html", context, status_code=400)
    try:
        user = register_user(username=username, password=password, email=email)
        set_session_user(request, user)
        return RedirectResponse(url=_normalize_next_url(next, user), status_code=303)
    except HTTPException as exc:
        context = _base_context(request, "register")
        context.update(
            {
                "next_url": _normalize_next_url(next),
                "auth_error": str(exc.detail),
                "form_values": {"username": username, "email": email},
                "page_notice": "",
            }
        )
        return _cache_safe_template("register.html", context, status_code=exc.status_code)


@router.post("/auth/logout")
def logout_submit(request: Request):
    clear_session_user(request)
    return RedirectResponse(url="/login?message=" + quote("已退出登录，请重新登录"), status_code=303)


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = _require_page_user(request, "/account")
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/home?message=" + quote("请点击右上角头像查看和修改个人信息"), status_code=303)


@router.post("/account/profile")
def account_profile_submit(
    request: Request,
    real_name: str = Form(""),
    phone: str = Form(""),
    organization: str = Form(""),
    country_preference: str = Form(""),
    legal_type_preference: str = Form(""),
    note: str = Form(""),
):
    user = _require_page_user(request, "/account")
    if isinstance(user, RedirectResponse):
        return user
    update_user_profile(
        int(user["id"]),
        real_name=real_name,
        phone=phone,
        organization=organization,
        country_preference=country_preference,
        legal_type_preference=legal_type_preference,
        note=note,
    )
    return RedirectResponse(url="/home?message=" + quote("个人信息已更新"), status_code=303)


@router.get("/histories", response_class=HTMLResponse)
def histories_page(
    request: Request,
    query_type: str = Query(""),
    case_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_type: str = Query(""),
):
    user = _require_page_user(
        request,
        f"/histories?{_build_query_string(query_type=query_type, case_type=case_type, country=country, court_level=court_level, legal_type=legal_type)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "histories")
    context.update(
        {
            "histories": list_user_histories(
                user_id=int(user["id"]),
                query_type=query_type,
                case_type=case_type,
                country=country,
                court_level=court_level,
                legal_type=legal_type,
                limit=100,
            ),
            "filters": {
                "query_type": query_type,
                "case_type": case_type,
                "country": country,
                "court_level": court_level,
                "legal_type": legal_type,
            },
        }
    )
    context["histories"] = [_sanitize_history_display(build_history_display_payload(item)) for item in context["histories"]]
    return _cache_safe_template("histories.html", context)


@router.get("/histories/graph", response_class=HTMLResponse)
def histories_graph_page(
    request: Request,
    case_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_rule: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    limit: int = Query(200),
):
    user = _require_page_user(
        request,
        f"/histories/graph?{_build_query_string(case_type=case_type, country=country, court_level=court_level, legal_rule=legal_rule, start_date=start_date, end_date=end_date, limit=limit)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "histories_graph")
    context.update(
        {
            "graph_filters": {
                "case_type": case_type,
                "country": country,
                "court_level": court_level,
                "legal_rule": legal_rule,
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
            },
            "history_graph_data": build_user_history_graph(
                user_id=int(user["id"]),
                case_type=case_type,
                country=country,
                court_level=court_level,
                legal_rule=legal_rule,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            ),
        }
    )
    context["history_graph_data"] = _sanitize_history_graph(context["history_graph_data"])
    return _cache_safe_template("histories_graph.html", context)


@router.get("/histories/{history_id}", response_class=HTMLResponse)
def history_detail_page(request: Request, history_id: int):
    user = _require_page_user(request, f"/histories/{history_id}")
    if isinstance(user, RedirectResponse):
        return user
    history = get_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")), touch=False)
    if not history:
        raise HTTPException(status_code=404, detail="history not found")
    context = _base_context(request, "history_detail")
    history_item = _sanitize_history_display(build_history_display_payload(history))
    module_code = history_item.get("module_code") or ("us_sanctions" if history_item.get("country") == "United States" else "canada")
    predict_url = f"/predict?draft={quote(history_item.get('query_text') or '')}&module={quote(module_code)}"
    context.update(
        {
            "history_item": history_item,
            "can_predict": not bool(history_item.get("has_prediction")),
            "predict_url": predict_url,
        }
    )
    return _cache_safe_template("history_detail.html", context)


@router.get("/histories/{history_id}/restore")
def restore_history_page(request: Request, history_id: int):
    user = _require_page_user(request, f"/histories/{history_id}/restore")
    if isinstance(user, RedirectResponse):
        return user
    history = get_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")), touch=False)
    if not history:
        raise HTTPException(status_code=404, detail="history not found")
    return RedirectResponse(url=f"/histories/{history_id}", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    user = _require_page_admin(request, "/admin")
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, selected_user_id: int | None = Query(None)):
    user = _require_page_admin(
        request,
        f"/admin/users?{_build_query_string(selected_user_id=selected_user_id)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_users")
    context.update(
        {
            "admin_users": list_all_users(limit=300),
            "selected_user_id": selected_user_id,
            "selected_histories": list_all_histories(user_id=selected_user_id, limit=50) if selected_user_id else [],
        }
    )
    return _cache_safe_template("admin_users.html", context)


@router.get("/admin/histories", response_class=HTMLResponse)
def admin_histories_page(
    request: Request,
    user_id: int | None = Query(None),
    query_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_type: str = Query(""),
    rule_keyword: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    user = _require_page_admin(
        request,
        f"/admin/histories?{_build_query_string(user_id=user_id, query_type=query_type, country=country, court_level=court_level, legal_type=legal_type, rule_keyword=rule_keyword, start_date=start_date, end_date=end_date)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_histories")
    context.update(
        {
            "histories": list_all_histories(
                user_id=user_id,
                query_type=query_type,
                country=country,
                court_level=court_level,
                legal_type=legal_type,
                rule_keyword=rule_keyword,
                start_date=start_date,
                end_date=end_date,
                limit=300,
            ),
            "filters": {
                "user_id": user_id,
                "query_type": query_type,
                "country": country,
                "court_level": court_level,
                "legal_type": legal_type,
                "rule_keyword": rule_keyword,
                "start_date": start_date,
                "end_date": end_date,
            },
            "admin_users": list_all_users(limit=300),
        }
    )
    return _cache_safe_template("admin_histories.html", context)


@router.get("/admin/cases", response_class=HTMLResponse)
def admin_cases_page(
    request: Request,
    country: str = Query(""),
    case_type: str = Query(""),
    court_level: str = Query(""),
    rule_keyword: str = Query(""),
    keyword: str = Query(""),
    source_site: str = Query(""),
):
    user = _require_page_admin(
        request,
        f"/admin/cases?{_build_query_string(country=country, case_type=case_type, court_level=court_level, rule_keyword=rule_keyword, keyword=keyword, source_site=source_site)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_cases")
    context.update(
        {
            "cases": list_cases(
                country=country,
                case_type=case_type,
                court_level=court_level,
                rule_keyword=rule_keyword,
                keyword=keyword,
                source_site=source_site,
                limit=300,
            ),
            "filters": {
                "country": country,
                "case_type": case_type,
                "court_level": court_level,
                "rule_keyword": rule_keyword,
                "keyword": keyword,
                "source_site": source_site,
            },
        }
    )
    return _cache_safe_template("admin_cases.html", context)


@router.get("/admin/rules", response_class=HTMLResponse)
def admin_rules_page(
    request: Request,
    country: str = Query(""),
    legal_type: str = Query(""),
    article_no: str = Query(""),
    keyword: str = Query(""),
):
    user = _require_page_admin(
        request,
        f"/admin/rules?{_build_query_string(country=country, legal_type=legal_type, article_no=article_no, keyword=keyword)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_rules")
    context.update(
        {
            "rules": list_rules(
                country=country,
                legal_type=legal_type,
                article_no=article_no,
                keyword=keyword,
                limit=300,
            ),
            "filters": {
                "country": country,
                "legal_type": legal_type,
                "article_no": article_no,
                "keyword": keyword,
            },
        }
    )
    return _cache_safe_template("admin_rules.html", context)


@router.get("/admin/imports", response_class=HTMLResponse)
def admin_imports_page(request: Request):
    user = _require_page_admin(request, "/admin/imports")
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_imports")
    context.update({"import_tasks": list_import_tasks(limit=200)})
    return _cache_safe_template("admin_imports.html", context)


@router.post("/admin/import/manual")
def admin_import_manual_submit(
    request: Request,
    data_type: str = Form("case"),
    country: str = Form("Canada"),
    title: str = Form(""),
    legal_type: str = Form(""),
    case_type: str = Form(""),
    court_name: str = Form(""),
    court_level: str = Form(""),
    summary: str = Form(""),
    facts: str = Form(""),
    judgment_result: str = Form(""),
    article_no: str = Form(""),
    article_text: str = Form(""),
    article_summary: str = Form(""),
    source_url: str = Form(""),
    auto_link: bool = Form(False),
):
    user = _require_page_admin(request, "/admin/imports")
    if isinstance(user, RedirectResponse):
        return user
    import_manual_entry(
        created_by=int(user["id"]),
        data_type=data_type,
        country=country,
        title=title,
        legal_type=legal_type,
        case_type=case_type,
        court_name=court_name,
        court_level=court_level,
        summary=summary,
        facts=facts,
        judgment_result=judgment_result,
        article_no=article_no,
        article_text=article_text,
        article_summary=article_summary,
        source_url=source_url,
        auto_link=bool(auto_link),
    )
    return RedirectResponse(url="/admin/imports", status_code=303)


@router.post("/admin/import/url")
def admin_import_url_submit(
    request: Request,
    source_url: str = Form(""),
    country: str = Form("Canada"),
    data_type: str = Form("case"),
    legal_type: str = Form(""),
    case_type: str = Form(""),
    court_level: str = Form(""),
    auto_link: bool = Form(False),
):
    user = _require_page_admin(request, "/admin/imports")
    if isinstance(user, RedirectResponse):
        return user
    import_from_url(
        created_by=int(user["id"]),
        target_url=source_url,
        country=country,
        data_type=data_type,
        legal_type=legal_type,
        case_type=case_type,
        court_level=court_level,
        auto_link=bool(auto_link),
    )
    return RedirectResponse(url="/admin/imports", status_code=303)


@router.post("/admin/import/crawler")
def admin_import_crawler_submit(request: Request):
    user = _require_page_admin(request, "/admin/imports")
    if isinstance(user, RedirectResponse):
        return user
    run_canada_crawler_import(created_by=int(user["id"]))
    return RedirectResponse(url="/admin/imports", status_code=303)


@router.post("/api/auth/register")
def api_auth_register(request: Request, payload: AuthPayload):
    if payload.password != payload.confirmPassword:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致。")
    user = register_user(
        username=payload.username,
        password=payload.password,
        email=payload.email,
    )
    set_session_user(request, user)
    return {
        "message": "注册成功",
        "user": user,
        "landing_url": _landing_url_for_user(user),
        "session_max_age": settings.session_max_age_seconds,
    }


@router.post("/api/auth/login")
def api_auth_login(request: Request, payload: LoginPayload):
    login_value = payload.login or payload.username or payload.email
    user = authenticate_user(login_value, payload.password)
    set_session_user(request, user)
    return {
        "message": "登录成功",
        "user": user,
        "landing_url": _landing_url_for_user(user),
        "session_max_age": settings.session_max_age_seconds,
    }


@router.post("/api/auth/logout")
def api_auth_logout(request: Request):
    clear_session_user(request)
    return {"status": "ok", "message": "已退出登录"}


@router.get("/api/auth/me")
def api_auth_me(request: Request):
    user = require_user(request)
    return {"user": user}


@router.put("/api/users/me")
def api_update_me(request: Request, payload: ProfilePayload):
    user = require_user(request)
    updated = update_user_profile(
        int(user["id"]),
        email=payload.email,
        phone=payload.phone,
        organization=payload.organization,
        real_name=payload.real_name,
        country_preference=payload.country_preference,
        legal_type_preference=payload.legal_type_preference,
        note=payload.note,
    )
    return {"message": "个人信息已更新", "user": updated}


@router.put("/api/users/me/password")
def api_update_my_password(request: Request, payload: PasswordPayload):
    user = require_user(request)
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致。")
    update_user_password(int(user["id"]), payload.current_password, payload.new_password)
    return {"message": "密码已更新"}


@router.post("/api/analyze")
def api_analyze(request: Request, payload: AnalyzePayload):
    user = require_user(request)
    module_code = normalize_module(payload.module)
    if not payload.refresh:
        existing_history_id, existing_history_item = _exact_history_display_for_query(
            user_id=int(user["id"]),
            query_text=payload.text,
            module_code=module_code,
        )
        if existing_history_id and existing_history_item:
            existing_history_item["history_id"] = existing_history_id
            return existing_history_item
    result = _sanitize_prediction_payload(
        predict_legal_outcome(
        text=payload.text,
        limit=payload.limit,
        offset=payload.offset,
        source=resolve_source_for_module(module_code, payload.source),
        sort=payload.sort,
        module=module_code,
        refresh=payload.refresh,
        ),
        module_code,
    )
    history_id = _save_case_history_if_possible(
        request,
        query_text=payload.text,
        module_code=module_code,
        result_payload=result,
    )
    if history_id:
        result["history_id"] = history_id
    return _sanitize_history_display(_build_analysis_view_payload(
        query_text=payload.text,
        module_code=module_code,
        result_payload=result,
        history_id=history_id,
        user_id=int(user["id"]),
    ))


@router.get("/api/histories")
def api_histories(request: Request):
    user = require_user(request)
    query_type = request.query_params.get("query_type", "")
    case_type = request.query_params.get("case_type", "")
    country = request.query_params.get("country", "")
    court_level = request.query_params.get("court_level", "")
    legal_type = request.query_params.get("legal_type", "")
    if user.get("is_admin") and request.query_params.get("all") == "1":
        return [_sanitize_history_display(build_history_display_payload(item)) for item in list_all_histories(
            query_type=query_type,
            country=country,
            court_level=court_level,
            legal_type=legal_type,
            limit=200,
        )]
    return [_sanitize_history_display(build_history_display_payload(item)) for item in list_user_histories(
        user_id=int(user["id"]),
        query_type=query_type,
        case_type=case_type,
        country=country,
        court_level=court_level,
        legal_type=legal_type,
        limit=200,
    )]


@router.get("/api/histories/graph")
def api_histories_graph(
    request: Request,
    case_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_rule: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    limit: int = Query(200),
):
    user = require_user(request)
    return _sanitize_history_graph(build_user_history_graph(
        user_id=int(user["id"]),
        case_type=case_type,
        country=country,
        court_level=court_level,
        legal_rule=legal_rule,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    ))


@router.get("/api/histories/{history_id}")
def api_history_detail(request: Request, history_id: int):
    user = require_user(request)
    row = get_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")), touch=False)
    if not row:
        raise HTTPException(status_code=404, detail="history not found")
    return _sanitize_history_display(build_history_display_payload(row))


@router.delete("/api/histories/{history_id}")
def api_history_delete(request: Request, history_id: int):
    user = require_user(request)
    deleted = delete_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")))
    if not deleted:
        raise HTTPException(status_code=404, detail="history not found")
    return {"status": "deleted"}


@router.get("/api/histories/{history_id}/graph")
def api_history_graph(request: Request, history_id: int):
    user = require_user(request)
    history = get_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")), touch=False)
    if not history:
        raise HTTPException(status_code=404, detail="history not found")
    return _sanitize_history_graph(build_user_history_graph(user_id=int(history.get("user_id") or user["id"]), limit=200))


@router.get("/api/cases")
def api_cases(
    request: Request,
    country: str = Query(""),
    case_type: str = Query(""),
    court_level: str = Query(""),
    rule_keyword: str = Query(""),
    keyword: str = Query(""),
    source_site: str = Query(""),
):
    require_user(request)
    return list_cases(
        country=country,
        case_type=case_type,
        court_level=court_level,
        rule_keyword=rule_keyword,
        keyword=keyword,
        source_site=source_site,
        limit=300,
    )


@router.get("/api/cases/{case_id}")
def api_case_detail(request: Request, case_id: int):
    require_user(request)
    row = get_case(case_id)
    if not row:
        raise HTTPException(status_code=404, detail="case not found")
    return row


@router.get("/api/rules")
def api_rules(
    request: Request,
    country: str = Query(""),
    legal_type: str = Query(""),
    article_no: str = Query(""),
    keyword: str = Query(""),
):
    require_user(request)
    return list_rules(
        country=country,
        legal_type=legal_type,
        article_no=article_no,
        keyword=keyword,
        limit=300,
    )


@router.get("/api/rules/{rule_id}")
def api_rule_detail(request: Request, rule_id: int):
    require_user(request)
    row = get_rule(rule_id)
    if not row:
        raise HTTPException(status_code=404, detail="rule not found")
    return row


@router.get("/api/case-rule-relations")
def api_case_rule_relations(
    request: Request,
    case_id: int | None = Query(None),
    rule_id: int | None = Query(None),
):
    require_user(request)
    return list_case_rule_relations(case_id=case_id, rule_id=rule_id, limit=500)


@router.get("/api/admin/users")
def api_admin_users(request: Request):
    require_admin(request)
    return list_all_users(limit=300)


@router.put("/api/admin/users/{user_id}/status")
def api_admin_user_status(request: Request, user_id: int, payload: UserStatusPayload):
    require_admin(request)
    return update_user_status(user_id, payload.status)


@router.get("/api/admin/histories")
def api_admin_histories(
    request: Request,
    user_id: int | None = Query(None),
    query_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_type: str = Query(""),
    rule_keyword: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    require_admin(request)
    return list_all_histories(
        user_id=user_id,
        query_type=query_type,
        country=country,
        court_level=court_level,
        legal_type=legal_type,
        rule_keyword=rule_keyword,
        start_date=start_date,
        end_date=end_date,
        limit=500,
    )


@router.get("/api/admin/cases")
def api_admin_cases(
    request: Request,
    country: str = Query(""),
    case_type: str = Query(""),
    court_level: str = Query(""),
    rule_keyword: str = Query(""),
    keyword: str = Query(""),
    source_site: str = Query(""),
):
    require_admin(request)
    return list_cases(
        country=country,
        case_type=case_type,
        court_level=court_level,
        rule_keyword=rule_keyword,
        keyword=keyword,
        source_site=source_site,
        limit=500,
    )


@router.get("/api/admin/rules")
def api_admin_rules(
    request: Request,
    country: str = Query(""),
    legal_type: str = Query(""),
    article_no: str = Query(""),
    keyword: str = Query(""),
):
    require_admin(request)
    return list_rules(
        country=country,
        legal_type=legal_type,
        article_no=article_no,
        keyword=keyword,
        limit=500,
    )


@router.post("/api/admin/import/url")
def api_admin_import_url(request: Request, payload: ImportUrlPayload):
    user = require_admin(request)
    return import_from_url(
        created_by=int(user["id"]),
        target_url=payload.source_url,
        country=payload.country,
        data_type=payload.data_type,
        legal_type=payload.legal_type,
        case_type=payload.case_type,
        court_level=payload.court_level,
        auto_link=payload.auto_link,
    )


@router.post("/api/admin/import/manual")
def api_admin_import_manual(request: Request, payload: ImportManualPayload):
    user = require_admin(request)
    return import_manual_entry(
        created_by=int(user["id"]),
        data_type=payload.data_type,
        country=payload.country,
        title=payload.title,
        legal_type=payload.legal_type,
        case_type=payload.case_type,
        court_name=payload.court_name,
        court_level=payload.court_level,
        summary=payload.summary,
        facts=payload.facts,
        judgment_result=payload.judgment_result,
        article_no=payload.article_no,
        article_text=payload.article_text,
        article_summary=payload.article_summary,
        source_url=payload.source_url,
        auto_link=payload.auto_link,
    )


@router.post("/api/admin/crawler/run")
def api_admin_crawler_run(request: Request):
    user = require_admin(request)
    return run_canada_crawler_import(created_by=int(user["id"]))


@router.get("/api/admin/import/tasks")
def api_admin_import_tasks(request: Request):
    require_admin(request)
    return list_import_tasks(limit=300)
