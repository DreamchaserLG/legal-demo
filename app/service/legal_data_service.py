import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.service.canada_case_law_service import bootstrap_canada_law_graph
from app.service.canada_legislation_service import sync_canada_legislation_demo
from app.service.canlii_service import sync_canlii_demo
from app.service.common_service import plain_text_preview, repair_text, sha256_text, split_keywords, upsert_source_item

CANADA_CASE_SOURCE_CODES = {"canlii", "manual_canada_case", "url_canada_case"}
CANADA_RULE_SOURCE_CODES = {
    "ca_federal_act",
    "ca_federal_regulation",
    "on_statute",
    "on_regulation",
    "manual_canada_rule",
    "url_canada_rule",
}
ALL_CANADA_SOURCE_CODES = CANADA_CASE_SOURCE_CODES | CANADA_RULE_SOURCE_CODES
SUPREME_COURT_CODES = {"scc", "uksc"}
APPEAL_COURT_CODES = {"fca", "onca", "abca", "bcca", "mbca", "nbca", "nlca", "nsca", "ntca", "nuca", "qcca", "skca", "ykca", "pescad"}
SUPERIOR_COURT_CODES = {"fc", "onsc", "abkb", "abqb", "bcsc", "mbkb", "mbqb", "nbkb", "nbqb", "nlsc", "nssc", "ntsc", "qccs", "skkb", "skqb", "yksc", "pecsc"}
PROVINCIAL_COURT_CODES = {"oncj", "ocj", "qccq", "skpc", "yktc", "nstc", "nspc", "pecp", "ntpc", "nupc"}
_DOMAIN_FETCH_STATE: dict[str, float] = {}
_CANADA_SYNC_STATE_KEY = "canada_local_sync_v1"
_CANADA_SYNC_THREAD: threading.Thread | None = None
_CANADA_SYNC_THREAD_LOCK = threading.Lock()


def ensure_legal_data_tables():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS legal_cases (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            court_name TEXT NOT NULL DEFAULT '',
            court_level TEXT NOT NULL DEFAULT '',
            court_rank INTEGER NOT NULL DEFAULT 0,
            case_type TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            facts TEXT NOT NULL DEFAULT '',
            judgment_result TEXT NOT NULL DEFAULT '',
            judgment_date DATE NULL,
            source_url TEXT NOT NULL DEFAULT '',
            source_site TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '',
            source_item_id BIGINT NULL,
            source_code VARCHAR(50) NOT NULL DEFAULT '',
            external_uid TEXT NOT NULL DEFAULT '',
            normalized_title TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_cases_source_item_full
        ON legal_cases (source_item_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_legal_cases_country_level
        ON legal_cases (country, court_rank DESC, updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_legal_cases_normalized_title
        ON legal_cases (normalized_title)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_cases_source_url
        ON legal_cases (LOWER(source_url))
        WHERE COALESCE(source_url, '') <> ''
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_cases_identity
        ON legal_cases (country, normalized_title, court_name, judgment_date)
        WHERE COALESCE(normalized_title, '') <> ''
        """,
        """
        CREATE TABLE IF NOT EXISTS legal_rules (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            legal_type TEXT NOT NULL DEFAULT '',
            article_no TEXT NOT NULL DEFAULT '',
            article_text TEXT NOT NULL DEFAULT '',
            article_summary TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            source_site TEXT NOT NULL DEFAULT '',
            source_item_id BIGINT NULL,
            canada_law_id BIGINT NULL,
            normalized_title TEXT NOT NULL DEFAULT '',
            slug VARCHAR(240) NOT NULL DEFAULT '',
            rule_level TEXT NOT NULL DEFAULT '',
            citation TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_rules_source_item_full
        ON legal_rules (source_item_id)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_rules_canada_law_full
        ON legal_rules (canada_law_id)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_rules_slug
        ON legal_rules (slug)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_legal_rules_normalized_title
        ON legal_rules (normalized_title)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_rules_source_url
        ON legal_rules (LOWER(source_url))
        WHERE COALESCE(source_url, '') <> ''
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_rules_identity
        ON legal_rules (country, normalized_title, article_no)
        WHERE COALESCE(normalized_title, '') <> ''
        """,
        """
        CREATE TABLE IF NOT EXISTS case_rule_relations (
            id BIGSERIAL PRIMARY KEY,
            case_id BIGINT NOT NULL REFERENCES legal_cases(id) ON DELETE CASCADE,
            rule_id BIGINT NOT NULL REFERENCES legal_rules(id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL DEFAULT '',
            match_score NUMERIC(6, 4) NOT NULL DEFAULT 0,
            match_reason TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_case_rule_relations_pair
        ON case_rule_relations (case_id, rule_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_case_rule_relations_rule
        ON case_rule_relations (rule_id, match_score DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_case_rule_relations_case
        ON case_rule_relations (case_id, match_score DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS import_tasks (
            id BIGSERIAL PRIMARY KEY,
            created_by BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
            import_type VARCHAR(30) NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            total_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_import_tasks_created
        ON import_tasks (created_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS app_runtime_state (
            state_key VARCHAR(120) PRIMARY KEY,
            state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    seed_demo_canada_case_rule_dataset()


def _load_runtime_state(state_key: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT state_json
                FROM app_runtime_state
                WHERE state_key = :state_key
                """
            ),
            {"state_key": state_key},
        ).mappings().first()
    payload = (row or {}).get("state_json")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _save_runtime_state(state_key: str, payload: dict):
    serialized = json.dumps(payload or {}, ensure_ascii=False, default=str)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO app_runtime_state (state_key, state_json, updated_at)
                VALUES (:state_key, CAST(:state_json AS JSONB), CURRENT_TIMESTAMP)
                ON CONFLICT (state_key)
                DO UPDATE SET
                    state_json = EXCLUDED.state_json,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "state_key": state_key,
                "state_json": serialized,
            },
        )


def _current_canada_source_signature() -> dict:
    sql = """
    SELECT
        (SELECT COUNT(*) FROM source_items WHERE source_code = ANY(:case_codes)) AS case_source_count,
        (SELECT COALESCE(MAX(id), 0) FROM source_items WHERE source_code = ANY(:case_codes)) AS case_source_max_id,
        (SELECT COALESCE(MAX(updated_at)::text, '') FROM source_items WHERE source_code = ANY(:case_codes)) AS case_source_max_updated_at,
        (SELECT COUNT(*) FROM source_items WHERE source_code = ANY(:rule_codes)) AS rule_source_count,
        (SELECT COALESCE(MAX(id), 0) FROM source_items WHERE source_code = ANY(:rule_codes)) AS rule_source_max_id,
        (SELECT COALESCE(MAX(updated_at)::text, '') FROM source_items WHERE source_code = ANY(:rule_codes)) AS rule_source_max_updated_at
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(sql),
            {
                "case_codes": sorted(CANADA_CASE_SOURCE_CODES),
                "rule_codes": sorted(CANADA_RULE_SOURCE_CODES),
            },
        ).mappings().first()
    payload = dict(row or {})
    return {
        "case_source_count": int(payload.get("case_source_count") or 0),
        "case_source_max_id": int(payload.get("case_source_max_id") or 0),
        "case_source_max_updated_at": str(payload.get("case_source_max_updated_at") or ""),
        "rule_source_count": int(payload.get("rule_source_count") or 0),
        "rule_source_max_id": int(payload.get("rule_source_max_id") or 0),
        "rule_source_max_updated_at": str(payload.get("rule_source_max_updated_at") or ""),
    }


def _current_canada_derived_counts() -> dict:
    sql = """
    SELECT
        (SELECT COUNT(*) FROM legal_cases WHERE country = 'Canada') AS legal_case_count,
        (SELECT COUNT(*) FROM legal_rules WHERE country = 'Canada') AS legal_rule_count,
        (
            SELECT COUNT(*)
            FROM case_rule_relations crr
            JOIN legal_cases lc ON lc.id = crr.case_id
            WHERE lc.country = 'Canada'
        ) AS relation_count
    """
    with engine.connect() as conn:
        row = conn.execute(text(sql)).mappings().first()
    payload = dict(row or {})
    return {
        "legal_case_count": int(payload.get("legal_case_count") or 0),
        "legal_rule_count": int(payload.get("legal_rule_count") or 0),
        "relation_count": int(payload.get("relation_count") or 0),
    }


def _can_skip_canada_sync(force: bool = False) -> tuple[bool, dict, dict]:
    source_signature = _current_canada_source_signature()
    derived_counts = _current_canada_derived_counts()
    if force:
        return False, source_signature, derived_counts
    saved_state = _load_runtime_state(_CANADA_SYNC_STATE_KEY)
    if not saved_state:
        return False, source_signature, derived_counts
    if (saved_state.get("source_signature") or {}) != source_signature:
        return False, source_signature, derived_counts
    if (saved_state.get("derived_counts") or {}) != derived_counts:
        return False, source_signature, derived_counts
    if source_signature["case_source_count"] > 0 and derived_counts["legal_case_count"] <= 0:
        return False, source_signature, derived_counts
    if source_signature["rule_source_count"] > 0 and derived_counts["legal_rule_count"] <= 0:
        return False, source_signature, derived_counts
    return True, source_signature, derived_counts


def _slugify(value: str) -> str:
    slug = repair_text(value).lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in slug)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:220] or f"rule-{int(time.time())}"


_DEMO_CANADA_RULES = [
    {
        "key": "residential-tenancies-act",
        "title": "Residential Tenancies Act, 2006",
        "country": "Canada",
        "legal_type": "Ontario Statute",
        "article_no": "s. 87 / s. 89 / s. 98",
        "article_text": "Ontario landlords may seek rent arrears, termination, possession, and relief against unauthorized subletting under the Residential Tenancies Act, 2006.",
        "article_summary": "Rent arrears, possession, termination, and unauthorized subletting in Ontario residential tenancies.",
        "source_url": "https://demo.local/canada/rules/residential-tenancies-act-2006",
        "source_site": "Demo Seed",
        "rule_level": "Ontario Statute",
        "citation": "RTA 2006",
    },
    {
        "key": "employment-standards-act",
        "title": "Employment Standards Act, 2000",
        "country": "Canada",
        "legal_type": "Ontario Statute",
        "article_no": "s. 54 / s. 57 / s. 60",
        "article_text": "The Employment Standards Act, 2000 governs notice of termination, wages, and reprisals in Ontario employment disputes.",
        "article_summary": "Termination notice, wage rights, and reprisal protections in Ontario employment matters.",
        "source_url": "https://demo.local/canada/rules/employment-standards-act-2000",
        "source_site": "Demo Seed",
        "rule_level": "Ontario Statute",
        "citation": "ESA 2000",
    },
    {
        "key": "occupiers-liability-act",
        "title": "Occupiers' Liability Act",
        "country": "Canada",
        "legal_type": "Ontario Statute",
        "article_no": "s. 3",
        "article_text": "An occupier owes a duty to take reasonable care to see that persons entering the premises are reasonably safe.",
        "article_summary": "Reasonable-care duty for safety on premises and slip-and-fall style negligence claims.",
        "source_url": "https://demo.local/canada/rules/occupiers-liability-act",
        "source_site": "Demo Seed",
        "rule_level": "Ontario Statute",
        "citation": "OLA s. 3",
    },
    {
        "key": "consumer-protection-act",
        "title": "Consumer Protection Act, 2002",
        "country": "Canada",
        "legal_type": "Ontario Statute",
        "article_no": "s. 14 / s. 18",
        "article_text": "The Consumer Protection Act, 2002 addresses unfair practices, consumer misrepresentation, and statutory remedies.",
        "article_summary": "Misrepresentation, unfair practices, and statutory consumer remedies.",
        "source_url": "https://demo.local/canada/rules/consumer-protection-act-2002",
        "source_site": "Demo Seed",
        "rule_level": "Ontario Statute",
        "citation": "CPA 2002",
    },
    {
        "key": "criminal-code-fraud",
        "title": "Criminal Code",
        "country": "Canada",
        "legal_type": "Federal Act",
        "article_no": "s. 380",
        "article_text": "Section 380 of the Criminal Code addresses fraud, dishonest deprivation, and financial loss caused by deceit or falsehood.",
        "article_summary": "Fraud, dishonest deprivation, and financial loss under section 380 of the Criminal Code.",
        "source_url": "https://demo.local/canada/rules/criminal-code-fraud",
        "source_site": "Demo Seed",
        "rule_level": "Federal Act",
        "citation": "Criminal Code s. 380",
    },
]


_DEMO_CANADA_CASES = [
    {
        "key": "maple-view-v-chen",
        "title": "Maple View Property Management v. Chen",
        "country": "Canada",
        "court_name": "Ontario Landlord and Tenant Board",
        "court_level": "Ontario Landlord and Tenant Board",
        "court_rank": 1,
        "case_type": "Lease dispute",
        "summary": "The tenant stopped paying rent for three months and sublet the residential unit without the landlord's consent. The landlord sought arrears, termination, and possession.",
        "facts": "Residential lease dispute involving rent arrears, unauthorized sublet, possession, and damages.",
        "judgment_result": "Termination and possession granted; rent arrears and reasonable costs awarded.",
        "judgment_date": "2024-03-18",
        "source_url": "https://demo.local/canada/cases/maple-view-v-chen",
        "source_site": "Demo Seed",
        "raw_text": "A residential tenancy dispute about unpaid rent, unauthorized subletting, possession, termination, arrears, and damages.",
        "rule_keys": ["residential-tenancies-act"],
        "match_reason": "Residential lease default, rent arrears, and unauthorized subletting align with the Residential Tenancies Act, 2006.",
    },
    {
        "key": "north-york-housing-v-ibrahim",
        "title": "North York Housing Corp. v. Ibrahim",
        "country": "Canada",
        "court_name": "Ontario Divisional Court",
        "court_level": "Ontario Divisional Court",
        "court_rank": 2,
        "case_type": "Lease appeal",
        "summary": "An appeal concerning repeated rent default and a disputed subletting arrangement in a residential tenancy setting.",
        "facts": "Landlord challenged whether repeated default and subletting justified termination and possession.",
        "judgment_result": "Possession order upheld and matter remitted only on limited damages issues.",
        "judgment_date": "2023-11-02",
        "source_url": "https://demo.local/canada/cases/north-york-housing-v-ibrahim",
        "source_site": "Demo Seed",
        "raw_text": "Residential tenancy appeal involving rent arrears, unauthorized sublet, eviction, and possession.",
        "rule_keys": ["residential-tenancies-act"],
        "match_reason": "The dispute turns on arrears, possession, and subletting issues under Ontario's residential tenancy regime.",
    },
    {
        "key": "singh-v-aurora-logistics",
        "title": "Singh v. Aurora Logistics Inc.",
        "country": "Canada",
        "court_name": "Ontario Superior Court of Justice",
        "court_level": "Ontario Superior Court of Justice",
        "court_rank": 2,
        "case_type": "Employment",
        "summary": "An employee alleged wrongful dismissal, unpaid wages, and inadequate termination notice after a sudden discharge.",
        "facts": "Employment dispute about termination notice, final pay, and records showing ongoing performance concerns.",
        "judgment_result": "Employee obtained unpaid wages and partial notice damages.",
        "judgment_date": "2024-01-26",
        "source_url": "https://demo.local/canada/cases/singh-v-aurora-logistics",
        "source_site": "Demo Seed",
        "raw_text": "Wrongful dismissal and wage dispute involving employment standards notice obligations and unpaid compensation.",
        "rule_keys": ["employment-standards-act"],
        "match_reason": "The record focuses on notice, wages, and dismissal timing under the Employment Standards Act, 2000.",
    },
    {
        "key": "matthews-v-ocean-nutrition",
        "title": "Matthews v. Ocean Nutrition Canada Ltd.",
        "country": "Canada",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court of Canada",
        "court_rank": 4,
        "case_type": "Employment",
        "summary": "A senior employee challenged the loss of compensation following constructive dismissal and argued the bonus plan should remain available during the notice period.",
        "facts": "Constructive dismissal dispute involving compensation, dismissal timing, and contractual limits on bonus payments.",
        "judgment_result": "Employee succeeded on the compensation issue during the reasonable notice period.",
        "judgment_date": "2020-10-09",
        "source_url": "https://demo.local/canada/cases/matthews-v-ocean-nutrition",
        "source_site": "Demo Seed",
        "raw_text": "Employment dismissal appeal about notice-period compensation, bonus entitlement, and termination rights.",
        "rule_keys": ["employment-standards-act"],
        "match_reason": "The case is routinely used to frame notice-period compensation and dismissal analysis.",
    },
    {
        "key": "patel-v-downtown-retail-centre",
        "title": "Patel v. Downtown Retail Centre",
        "country": "Canada",
        "court_name": "Ontario Superior Court of Justice",
        "court_level": "Ontario Superior Court of Justice",
        "court_rank": 2,
        "case_type": "Negligence",
        "summary": "A visitor slipped on an uncleared wet floor and claimed the occupier failed to implement reasonable inspection and cleanup practices.",
        "facts": "Premises liability dispute about inspection logs, warning signs, and reasonable safety measures.",
        "judgment_result": "Liability found with damages reduced for contributory negligence.",
        "judgment_date": "2023-06-14",
        "source_url": "https://demo.local/canada/cases/patel-v-downtown-retail-centre",
        "source_site": "Demo Seed",
        "raw_text": "Slip-and-fall negligence claim about occupier duty, inspection records, warnings, and damages.",
        "rule_keys": ["occupiers-liability-act"],
        "match_reason": "The claim directly concerns occupier safety duties and the reasonable-care standard.",
    },
    {
        "key": "mustapha-v-culligan",
        "title": "Mustapha v. Culligan of Canada Ltd.",
        "country": "Canada",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court of Canada",
        "court_rank": 4,
        "case_type": "Negligence",
        "summary": "The appeal addressed foreseeability and remoteness in a negligence claim arising from a contaminated water bottle incident.",
        "facts": "Negligence appeal about foreseeable harm, psychological injury, and the scope of compensable loss.",
        "judgment_result": "Claim dismissed because the injury was not reasonably foreseeable in law.",
        "judgment_date": "2008-10-17",
        "source_url": "https://demo.local/canada/cases/mustapha-v-culligan",
        "source_site": "Demo Seed",
        "raw_text": "Negligence appeal addressing foreseeability, remoteness, and damages after a contaminated product incident.",
        "rule_keys": ["occupiers-liability-act"],
        "match_reason": "Although factually different, it is commonly used on negligence, foreseeability, and remoteness reasoning.",
    },
    {
        "key": "richard-v-time",
        "title": "Richard v. Time Inc.",
        "country": "Canada",
        "court_name": "Supreme Court of Canada",
        "court_level": "Supreme Court of Canada",
        "court_rank": 4,
        "case_type": "Consumer protection",
        "summary": "The Court considered misleading promotional materials and the consumer's statutory remedies for deceptive marketing.",
        "facts": "Consumer protection dispute about misleading prize notifications and unfair commercial representations.",
        "judgment_result": "Consumer remedies upheld for misleading representation.",
        "judgment_date": "2012-11-28",
        "source_url": "https://demo.local/canada/cases/richard-v-time",
        "source_site": "Demo Seed",
        "raw_text": "Consumer misrepresentation dispute about deceptive marketing, reliance, and statutory remedies.",
        "rule_keys": ["consumer-protection-act"],
        "match_reason": "The dispute centers on consumer misrepresentation and statutory remedies.",
    },
    {
        "key": "r-v-malhotra",
        "title": "R. v. Malhotra",
        "country": "Canada",
        "court_name": "Ontario Court of Justice",
        "court_level": "Ontario Court of Justice",
        "court_rank": 1,
        "case_type": "Criminal fraud",
        "summary": "The prosecution alleged a coordinated invoice scheme designed to obtain funds through false representations and dishonest deprivation.",
        "facts": "Fraud prosecution involving false invoices, financial loss, internal controls, and intent evidence.",
        "judgment_result": "Conviction entered on the fraud count after documentary and accounting evidence was accepted.",
        "judgment_date": "2023-09-15",
        "source_url": "https://demo.local/canada/cases/r-v-malhotra",
        "source_site": "Demo Seed",
        "raw_text": "Criminal fraud matter about false representations, dishonest deprivation, money flow, and documentary proof.",
        "rule_keys": ["criminal-code-fraud"],
        "match_reason": "The charge is framed directly under section 380 of the Criminal Code.",
    },
]


def seed_demo_canada_case_rule_dataset() -> int:
    if not getattr(settings, "demo_history_seed_enabled", False):
        return 0
    seed_count = 0
    with engine.begin() as conn:
        rule_ids: dict[str, int] = {}
        for rule in _DEMO_CANADA_RULES:
            normalized_title = repair_text(rule["title"]).lower()
            slug = _slugify(f"demo-{rule['key']}")
            existing = conn.execute(
                text(
                    """
                    SELECT id
                    FROM legal_rules
                    WHERE slug = :slug
                       OR LOWER(source_url) = LOWER(:source_url)
                       OR (country = :country AND normalized_title = :normalized_title AND article_no = :article_no)
                    LIMIT 1
                    """
                ),
                {
                    "slug": slug,
                    "source_url": rule["source_url"],
                    "country": rule["country"],
                    "normalized_title": normalized_title,
                    "article_no": rule["article_no"],
                },
            ).mappings().first()
            if existing:
                rule_id = int(existing["id"])
                conn.execute(
                    text(
                        """
                        UPDATE legal_rules
                        SET title = :title,
                            country = :country,
                            legal_type = :legal_type,
                            article_no = :article_no,
                            article_text = :article_text,
                            article_summary = :article_summary,
                            source_url = :source_url,
                            source_site = :source_site,
                            normalized_title = :normalized_title,
                            slug = :slug,
                            rule_level = :rule_level,
                            citation = :citation,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :rule_id
                        """
                    ),
                    {"rule_id": rule_id, **rule, "normalized_title": normalized_title, "slug": slug},
                )
            else:
                inserted = conn.execute(
                    text(
                        """
                        INSERT INTO legal_rules (
                            title,
                            country,
                            legal_type,
                            article_no,
                            article_text,
                            article_summary,
                            source_url,
                            source_site,
                            normalized_title,
                            slug,
                            rule_level,
                            citation,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            :title,
                            :country,
                            :legal_type,
                            :article_no,
                            :article_text,
                            :article_summary,
                            :source_url,
                            :source_site,
                            :normalized_title,
                            :slug,
                            :rule_level,
                            :citation,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        RETURNING id
                        """
                    ),
                    {**rule, "normalized_title": normalized_title, "slug": slug},
                ).mappings().first()
                rule_id = int(inserted["id"]) if inserted else 0
                seed_count += 1
            rule_ids[rule["key"]] = rule_id

        for case in _DEMO_CANADA_CASES:
            normalized_title = repair_text(case["title"]).lower()
            existing = conn.execute(
                text(
                    """
                    SELECT id
                    FROM legal_cases
                    WHERE LOWER(source_url) = LOWER(:source_url)
                       OR (
                            country = :country
                        AND normalized_title = :normalized_title
                        AND court_name = :court_name
                        AND judgment_date = CAST(:judgment_date AS date)
                       )
                    LIMIT 1
                    """
                ),
                {
                    "source_url": case["source_url"],
                    "country": case["country"],
                    "normalized_title": normalized_title,
                    "court_name": case["court_name"],
                    "judgment_date": case["judgment_date"],
                },
            ).mappings().first()
            case_payload = {
                "title": case["title"],
                "country": case["country"],
                "court_name": case["court_name"],
                "court_level": case["court_level"],
                "court_rank": int(case["court_rank"]),
                "case_type": case["case_type"],
                "summary": case["summary"],
                "facts": case["facts"],
                "judgment_result": case["judgment_result"],
                "judgment_date": case["judgment_date"],
                "source_url": case["source_url"],
                "source_site": case["source_site"],
                "raw_text": case["raw_text"],
                "source_code": "demo_canada_case",
                "external_uid": case["key"],
                "normalized_title": normalized_title,
            }
            if existing:
                case_id = int(existing["id"])
                conn.execute(
                    text(
                        """
                        UPDATE legal_cases
                        SET title = :title,
                            country = :country,
                            court_name = :court_name,
                            court_level = :court_level,
                            court_rank = :court_rank,
                            case_type = :case_type,
                            summary = :summary,
                            facts = :facts,
                            judgment_result = :judgment_result,
                            judgment_date = CAST(:judgment_date AS date),
                            source_url = :source_url,
                            source_site = :source_site,
                            raw_text = :raw_text,
                            source_code = :source_code,
                            external_uid = :external_uid,
                            normalized_title = :normalized_title,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :case_id
                        """
                    ),
                    {"case_id": case_id, **case_payload},
                )
            else:
                inserted = conn.execute(
                    text(
                        """
                        INSERT INTO legal_cases (
                            title,
                            country,
                            court_name,
                            court_level,
                            court_rank,
                            case_type,
                            summary,
                            facts,
                            judgment_result,
                            judgment_date,
                            source_url,
                            source_site,
                            raw_text,
                            source_code,
                            external_uid,
                            normalized_title,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            :title,
                            :country,
                            :court_name,
                            :court_level,
                            :court_rank,
                            :case_type,
                            :summary,
                            :facts,
                            :judgment_result,
                            CAST(:judgment_date AS date),
                            :source_url,
                            :source_site,
                            :raw_text,
                            :source_code,
                            :external_uid,
                            :normalized_title,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        RETURNING id
                        """
                    ),
                    case_payload,
                ).mappings().first()
                case_id = int(inserted["id"]) if inserted else 0
                seed_count += 1

            for rule_key in case["rule_keys"]:
                rule_id = int(rule_ids.get(rule_key) or 0)
                if not rule_id or not case_id:
                    continue
                conn.execute(
                    text(
                        """
                        INSERT INTO case_rule_relations (
                            case_id,
                            rule_id,
                            relation_type,
                            match_score,
                            match_reason,
                            created_at
                        )
                        VALUES (
                            :case_id,
                            :rule_id,
                            'demo_seed',
                            :match_score,
                            :match_reason,
                            CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (case_id, rule_id)
                        DO UPDATE SET
                            relation_type = EXCLUDED.relation_type,
                            match_score = EXCLUDED.match_score,
                            match_reason = EXCLUDED.match_reason
                        """
                    ),
                    {
                        "case_id": case_id,
                        "rule_id": rule_id,
                        "match_score": 0.84 if case["court_rank"] >= 3 else 0.76,
                        "match_reason": case["match_reason"],
                    },
                )
    return seed_count


def _derive_court_code(row: dict) -> str:
    existing = repair_text(row.get("court_code")).lower()
    if existing:
        return existing
    meta = row.get("raw_json") or {}
    database_page = repair_text(meta.get("database_page")).lower()
    item_url = repair_text(row.get("item_url")).lower()
    candidates = [database_page, item_url]
    for candidate in candidates:
        if "/doc/" in candidate:
            parts = [part for part in candidate.split("/") if part]
            for index, part in enumerate(parts):
                if part == "doc" and index > 0:
                    return parts[index - 1]
        if candidate.rstrip("/"):
            return candidate.rstrip("/").split("/")[-1]
    return ""


def _derive_court_rank(court_code: str) -> int:
    code = repair_text(court_code).lower()
    if code in SUPREME_COURT_CODES:
        return 5
    if code in APPEAL_COURT_CODES:
        return 4
    if code in SUPERIOR_COURT_CODES:
        return 3
    if code in PROVINCIAL_COURT_CODES:
        return 2
    return 1 if code else 0


def _court_level_label(rank: int, court_code: str = "") -> str:
    if rank >= 5:
        return "最高法院"
    if rank == 4:
        return "上诉法院"
    if rank == 3:
        return "高级法院 / 联邦法院"
    if rank == 2:
        return "省级 / 地方法院"
    if rank == 1:
        return repair_text(court_code).upper() or "其他审裁机构"
    return "未识别"


def _case_scope(case_row: dict) -> str:
    rank = int(case_row.get("court_rank") or 0)
    if rank >= 3:
        return "national_federal"
    if rank >= 1:
        return "provincial_local"
    return "other"


def _source_site_for_case(source_code: str) -> str:
    if source_code == "canlii":
        return "CanLII"
    if source_code in {"manual_canada_case", "url_canada_case"}:
        return "Manual / URL Import"
    return repair_text(source_code).upper()


def _source_site_for_rule(source_code: str, title: str = "") -> str:
    if source_code.startswith("ca_federal_"):
        return "Justice Laws Website"
    if source_code.startswith("on_"):
        return "Ontario e-Laws"
    if source_code in {"manual_canada_rule", "url_canada_rule"}:
        return "Manual / URL Import"
    if "OFAC" in title.upper():
        return "OFAC"
    return repair_text(source_code).upper() or "Imported Rule"


def _normalize_title(value: str) -> str:
    return " ".join(repair_text(value).lower().split())


def _fetch_canada_source_cases() -> list[dict]:
    sql = """
    SELECT
        id,
        source_code,
        source_uid,
        title,
        item_url,
        published_at,
        summary,
        raw_text,
        raw_json,
        created_at,
        updated_at
    FROM source_items
    WHERE source_code = ANY(:source_codes)
    ORDER BY updated_at DESC, id DESC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"source_codes": list(CANADA_CASE_SOURCE_CODES)}).mappings().all()
    return [dict(row) for row in rows]


def _fetch_canada_law_rows() -> list[dict]:
    sql = """
    SELECT
        l.*,
        si.summary AS source_summary,
        si.raw_text AS source_raw_text
    FROM canada_laws l
    LEFT JOIN source_items si ON si.id = l.source_item_id
    ORDER BY l.origin = 'official' DESC, l.updated_at DESC, l.title ASC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).mappings().all()
    return [dict(row) for row in rows]


def _find_existing_case_id(
    conn,
    *,
    source_item_id,
    source_url: str,
    country: str,
    normalized_title: str,
    court_name: str,
    judgment_date,
) -> int | None:
    row = conn.execute(
        text(
            """
            SELECT id
            FROM legal_cases
            WHERE (
                    source_item_id IS NOT NULL
                AND source_item_id = :source_item_id
            )
               OR (
                    COALESCE(:source_url, '') <> ''
                AND LOWER(source_url) = LOWER(:source_url)
            )
               OR (
                    country = :country
                AND normalized_title = :normalized_title
                AND court_name = :court_name
                AND judgment_date IS NOT DISTINCT FROM :judgment_date
            )
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {
            "source_item_id": source_item_id,
            "source_url": repair_text(source_url),
            "country": repair_text(country),
            "normalized_title": repair_text(normalized_title),
            "court_name": repair_text(court_name),
            "judgment_date": judgment_date,
        },
    ).mappings().first()
    return int(row["id"]) if row else None


def _find_existing_rule_id(
    conn,
    *,
    source_item_id,
    canada_law_id,
    source_url: str,
    country: str,
    normalized_title: str,
    article_no: str,
) -> int | None:
    row = conn.execute(
        text(
            """
            SELECT id
            FROM legal_rules
            WHERE (
                    source_item_id IS NOT NULL
                AND source_item_id = :source_item_id
            )
               OR (
                    canada_law_id IS NOT NULL
                AND canada_law_id = :canada_law_id
            )
               OR (
                    COALESCE(:source_url, '') <> ''
                AND LOWER(source_url) = LOWER(:source_url)
            )
               OR (
                    country = :country
                AND normalized_title = :normalized_title
                AND article_no = :article_no
            )
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {
            "source_item_id": source_item_id,
            "canada_law_id": canada_law_id,
            "source_url": repair_text(source_url),
            "country": repair_text(country),
            "normalized_title": repair_text(normalized_title),
            "article_no": repair_text(article_no),
        },
    ).mappings().first()
    return int(row["id"]) if row else None


def _upsert_legal_case_from_source(row: dict) -> int:
    title = repair_text(row.get("title"))
    meta = row.get("raw_json") or {}
    court_code = _derive_court_code(row)
    court_rank = _derive_court_rank(court_code)
    court_name = repair_text(meta.get("database_page")) or repair_text(court_code).upper()
    country = repair_text(meta.get("country") or "Canada")
    court_level = _court_level_label(court_rank, court_code)
    case_type = repair_text(meta.get("case_type") or "Case")
    summary = repair_text(row.get("summary"))[:4000]
    facts = repair_text(meta.get("facts") or "")[:4000]
    judgment_result = repair_text(meta.get("judgment_result") or "")[:2000]
    source_url = repair_text(row.get("item_url"))
    source_site = _source_site_for_case(repair_text(row.get("source_code")))
    raw_text = repair_text(row.get("raw_text"))[:20000]
    source_item_id = int(row["id"])
    source_code = repair_text(row.get("source_code"))
    external_uid = repair_text(row.get("source_uid"))
    normalized_title = _normalize_title(title)
    judgment_date = row.get("published_at")
    with engine.begin() as conn:
        existing_id = _find_existing_case_id(
            conn,
            source_item_id=source_item_id,
            source_url=source_url,
            country=country,
            normalized_title=normalized_title,
            court_name=court_name[:500],
            judgment_date=judgment_date,
        )
        if existing_id:
            record = conn.execute(
                text(
                    """
                    UPDATE legal_cases
                    SET title = :title,
                        country = :country,
                        court_name = :court_name,
                        court_level = :court_level,
                        court_rank = :court_rank,
                        case_type = :case_type,
                        summary = :summary,
                        facts = :facts,
                        judgment_result = :judgment_result,
                        judgment_date = :judgment_date,
                        source_url = :source_url,
                        source_site = :source_site,
                        raw_text = :raw_text,
                        source_item_id = COALESCE(:source_item_id, source_item_id),
                        source_code = :source_code,
                        external_uid = :external_uid,
                        normalized_title = :normalized_title,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :case_id
                    RETURNING id
                    """
                ),
                {
                    "case_id": existing_id,
                    "title": title,
                    "country": country,
                    "court_name": court_name[:500],
                    "court_level": court_level,
                    "court_rank": court_rank,
                    "case_type": case_type,
                    "summary": summary,
                    "facts": facts,
                    "judgment_result": judgment_result,
                    "judgment_date": judgment_date,
                    "source_url": source_url,
                    "source_site": source_site,
                    "raw_text": raw_text,
                    "source_item_id": source_item_id,
                    "source_code": source_code,
                    "external_uid": external_uid,
                    "normalized_title": normalized_title,
                },
            ).mappings().first()
        else:
            record = conn.execute(
                text(
                    """
                    INSERT INTO legal_cases (
                        title,
                        country,
                        court_name,
                        court_level,
                        court_rank,
                        case_type,
                        summary,
                        facts,
                        judgment_result,
                        judgment_date,
                        source_url,
                        source_site,
                        raw_text,
                        source_item_id,
                        source_code,
                        external_uid,
                        normalized_title,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :title,
                        :country,
                        :court_name,
                        :court_level,
                        :court_rank,
                        :case_type,
                        :summary,
                        :facts,
                        :judgment_result,
                        :judgment_date,
                        :source_url,
                        :source_site,
                        :raw_text,
                        :source_item_id,
                        :source_code,
                        :external_uid,
                        :normalized_title,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    RETURNING id
                    """
                ),
                {
                    "title": title,
                    "country": country,
                    "court_name": court_name[:500],
                    "court_level": court_level,
                    "court_rank": court_rank,
                    "case_type": case_type,
                    "summary": summary,
                    "facts": facts,
                    "judgment_result": judgment_result,
                    "judgment_date": judgment_date,
                    "source_url": source_url,
                    "source_site": source_site,
                    "raw_text": raw_text,
                    "source_item_id": source_item_id,
                    "source_code": source_code,
                    "external_uid": external_uid,
                    "normalized_title": normalized_title,
                },
            ).mappings().first()
    return int(record["id"]) if record else 0


def _upsert_legal_rule_from_canada_law(row: dict) -> int:
    title = repair_text(row.get("title"))
    citation = repair_text(row.get("citation"))
    article_text = repair_text(row.get("source_raw_text") or "")
    article_summary = repair_text(row.get("source_summary") or "")
    country = repair_text(row.get("jurisdiction") or "Canada")
    legal_type = repair_text(row.get("law_kind") or "law")
    source_url = repair_text(row.get("source_url"))
    source_site = _source_site_for_rule(repair_text(row.get("source_code")), title)
    source_item_id = row.get("source_item_id")
    canada_law_id = int(row["id"])
    normalized_title = _normalize_title(title)
    slug = repair_text(row.get("slug")) or _slugify(title)
    rule_level = repair_text(row.get("law_level") or row.get("origin") or "")
    with engine.begin() as conn:
        existing_id = _find_existing_rule_id(
            conn,
            source_item_id=source_item_id,
            canada_law_id=canada_law_id,
            source_url=source_url,
            country=country,
            normalized_title=normalized_title,
            article_no=citation,
        )
        if existing_id:
            record = conn.execute(
                text(
                    """
                    UPDATE legal_rules
                    SET title = :title,
                        country = :country,
                        legal_type = :legal_type,
                        article_no = :article_no,
                        article_text = :article_text,
                        article_summary = :article_summary,
                        source_url = :source_url,
                        source_site = :source_site,
                        source_item_id = COALESCE(:source_item_id, source_item_id),
                        canada_law_id = COALESCE(:canada_law_id, canada_law_id),
                        normalized_title = :normalized_title,
                        slug = :slug,
                        rule_level = :rule_level,
                        citation = :citation,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :rule_id
                    RETURNING id
                    """
                ),
                {
                    "rule_id": existing_id,
                    "title": title,
                    "country": country,
                    "legal_type": legal_type,
                    "article_no": citation,
                    "article_text": article_text[:20000],
                    "article_summary": article_summary[:4000],
                    "source_url": source_url,
                    "source_site": source_site,
                    "source_item_id": source_item_id,
                    "canada_law_id": canada_law_id,
                    "normalized_title": normalized_title,
                    "slug": slug,
                    "rule_level": rule_level,
                    "citation": citation,
                },
            ).mappings().first()
        else:
            record = conn.execute(
                text(
                    """
                    INSERT INTO legal_rules (
                        title,
                        country,
                        legal_type,
                        article_no,
                        article_text,
                        article_summary,
                        source_url,
                        source_site,
                        source_item_id,
                        canada_law_id,
                        normalized_title,
                        slug,
                        rule_level,
                        citation,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :title,
                        :country,
                        :legal_type,
                        :article_no,
                        :article_text,
                        :article_summary,
                        :source_url,
                        :source_site,
                        :source_item_id,
                        :canada_law_id,
                        :normalized_title,
                        :slug,
                        :rule_level,
                        :citation,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    RETURNING id
                    """
                ),
                {
                    "title": title,
                    "country": country,
                    "legal_type": legal_type,
                    "article_no": citation,
                    "article_text": article_text[:20000],
                    "article_summary": article_summary[:4000],
                    "source_url": source_url,
                    "source_site": source_site,
                    "source_item_id": source_item_id,
                    "canada_law_id": canada_law_id,
                    "normalized_title": normalized_title,
                    "slug": slug,
                    "rule_level": rule_level,
                    "citation": citation,
                },
            ).mappings().first()
    return int(record["id"]) if record else 0


def _sync_canada_case_rule_relations():
    sql = """
    SELECT
        cl.case_item_id,
        cl.law_id,
        cl.matched_alias,
        cl.match_source,
        cl.match_score,
        cl.evidence_excerpt,
        lc.id AS legal_case_id,
        lr.id AS legal_rule_id
    FROM canada_case_law_links cl
    JOIN legal_cases lc ON lc.source_item_id = cl.case_item_id
    JOIN legal_rules lr ON lr.canada_law_id = cl.law_id
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM case_rule_relations crr
                USING legal_cases lc, legal_rules lr
                WHERE crr.case_id = lc.id
                  AND crr.rule_id = lr.id
                  AND lc.country = 'Canada'
                  AND lr.country = 'Canada'
                  AND COALESCE(crr.relation_type, '') <> 'demo_seed'
                """
            )
        )
        rows = conn.execute(text(sql)).mappings().all()
        for row in rows:
            reason = repair_text(row.get("matched_alias"))
            if reason:
                reason = f"Matched by {repair_text(row.get('match_source'))}: {reason}"
            else:
                reason = repair_text(row.get("match_source"))
            conn.execute(
                text(
                    """
                    INSERT INTO case_rule_relations (
                        case_id,
                        rule_id,
                        relation_type,
                        match_score,
                        match_reason,
                        created_at
                    )
                    VALUES (
                        :case_id,
                        :rule_id,
                        :relation_type,
                        :match_score,
                        :match_reason,
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (case_id, rule_id)
                    DO UPDATE SET
                        relation_type = EXCLUDED.relation_type,
                        match_score = EXCLUDED.match_score,
                        match_reason = EXCLUDED.match_reason
                    """
                ),
                {
                    "case_id": int(row["legal_case_id"]),
                    "rule_id": int(row["legal_rule_id"]),
                    "relation_type": repair_text(row.get("match_source") or "direct_mention"),
                    "match_score": float(row.get("match_score") or 0),
                    "match_reason": reason[:3000],
                },
            )


def sync_canada_legal_data(force: bool = False) -> dict:
    ensure_legal_data_tables()
    should_skip, source_signature, derived_counts = _can_skip_canada_sync(force=force)
    if should_skip:
        return {
            "cases": 0,
            "rules": 0,
            "relations": int(derived_counts.get("relation_count") or 0),
            "skipped": True,
            "reason": "up_to_date",
        }
    bootstrap_canada_law_graph(force=force)
    cases = _fetch_canada_source_cases()
    laws = _fetch_canada_law_rows()
    case_count = 0
    rule_count = 0
    for row in cases:
        if _upsert_legal_case_from_source(row):
            case_count += 1
    for row in laws:
        if _upsert_legal_rule_from_canada_law(row):
            rule_count += 1
    _sync_canada_case_rule_relations()
    refreshed_counts = _current_canada_derived_counts()
    _save_runtime_state(
        _CANADA_SYNC_STATE_KEY,
        {
            "source_signature": source_signature,
            "derived_counts": refreshed_counts,
            "synced_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return {
        "cases": case_count,
        "rules": rule_count,
        "relations": int(refreshed_counts.get("relation_count") or 0),
        "skipped": False,
    }


def schedule_canada_legal_data_sync(force: bool = False) -> dict:
    global _CANADA_SYNC_THREAD
    ensure_legal_data_tables()
    with _CANADA_SYNC_THREAD_LOCK:
        if _CANADA_SYNC_THREAD and _CANADA_SYNC_THREAD.is_alive():
            return {"scheduled": False, "reason": "already_running"}

        def _runner():
            try:
                sync_canada_legal_data(force=force)
            except Exception as exc:
                print(f"[startup] canada local sync failed: {exc}")

        _CANADA_SYNC_THREAD = threading.Thread(
            target=_runner,
            name="canada-local-sync",
            daemon=True,
        )
        _CANADA_SYNC_THREAD.start()
    return {"scheduled": True, "reason": "started"}


def _fetch_case_rule_matches(case_source_item_ids: list[int], rule_source_item_ids: list[int]) -> list[dict]:
    params = {
        "case_source_item_ids": case_source_item_ids or [-1],
        "rule_source_item_ids": rule_source_item_ids or [-1],
    }
    sql = """
    SELECT
        lc.id AS case_id,
        lc.title AS case_title,
        lc.country AS case_country,
        lc.court_name,
        lc.court_level,
        lc.court_rank,
        lc.case_type,
        lc.summary AS case_summary,
        lc.facts AS case_facts,
        lc.judgment_result,
        lc.judgment_date,
        lc.source_url AS case_source_url,
        lc.source_site AS case_source_site,
        lc.source_item_id AS case_source_item_id,
        lr.id AS rule_id,
        lr.title AS rule_title,
        lr.country AS rule_country,
        lr.legal_type,
        lr.article_no,
        lr.article_text,
        lr.article_summary,
        lr.source_url AS rule_source_url,
        lr.source_site AS rule_source_site,
        lr.slug,
        lr.rule_level,
        crr.relation_type,
        crr.match_score,
        crr.match_reason
    FROM case_rule_relations crr
    JOIN legal_cases lc ON lc.id = crr.case_id
    JOIN legal_rules lr ON lr.id = crr.rule_id
    WHERE lc.country = 'Canada'
      AND (
            lc.source_item_id = ANY(:case_source_item_ids)
         OR lr.source_item_id = ANY(:rule_source_item_ids)
      )
    ORDER BY crr.match_score DESC, lc.court_rank DESC, lc.judgment_date DESC NULLS LAST, lc.id DESC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _keyword_match_score(keywords: list[str], *parts: str) -> int:
    haystack = " ".join(repair_text(part).lower() for part in parts if repair_text(part))
    score = 0
    for keyword in keywords or []:
        normalized = repair_text(keyword).lower()
        if not normalized:
            continue
        if normalized in haystack:
            score += 3 if normalized in repair_text(parts[0] if parts else "").lower() else 1
    return score


def _keyword_only_relevant_laws(keywords: list[str], limit: int = 8) -> list[dict]:
    normalized_keywords = [repair_text(item) for item in keywords or [] if repair_text(item)]
    if not normalized_keywords:
        return []
    rows = list_rules(country="Canada", limit=500)
    scored = []
    for row in rows:
        score = _keyword_match_score(
            normalized_keywords,
            row.get("title"),
            row.get("article_no"),
            row.get("article_summary"),
            row.get("article_text"),
        )
        if score <= 0:
            continue
        item = dict(row)
        item["keyword_score"] = score
        scored.append(item)
    scored.sort(
        key=lambda item: (
            int(item.get("keyword_score") or 0),
            int(item.get("related_case_count") or 0),
            repair_text(item.get("title")).lower(),
        ),
        reverse=True,
    )
    laws = []
    for row in scored[:limit]:
        laws.append(
            {
                "rule_id": row.get("id"),
                "title": repair_text(row.get("title")),
                "country": repair_text(row.get("country") or "Canada"),
                "legal_type": repair_text(row.get("legal_type") or "law"),
                "article_no": repair_text(row.get("article_no")),
                "article_summary": repair_text(row.get("article_summary")) or plain_text_preview(row.get("article_text"))[:280],
                "source_url": repair_text(row.get("source_url")),
                "detail_url": f"/law/canada/{repair_text(row.get('slug'))}" if repair_text(row.get("slug")) else "",
                "rule_level": repair_text(row.get("rule_level") or row.get("legal_type") or ""),
                "linked_case_count": int(row.get("related_case_count") or 0),
                "national_case_count": 0,
                "local_case_count": 0,
                "related_cases": [],
                "case_columns": [],
            }
        )
    return laws


_CANADA_KEYWORD_ALIAS_GROUPS = [
    (("租", "租赁", "房东", "承租", "tenant", "landlord", "lease", "rent", "sublet", "tenancy"), ("lease", "tenancy", "rent", "tenant", "landlord", "sublet", "eviction", "termination")),
    (("合同", "合约", "违约", "contract", "agreement", "breach"), ("contract", "agreement", "breach", "damages", "termination")),
    (("劳动", "劳务", "雇佣", "解雇", "工资", "employee", "employment", "dismissal", "wage", "employer"), ("employment", "labor", "employee", "employer", "dismissal", "wages", "wrongful dismissal")),
    (("侵权", "过失", "损害", "injury", "tort", "negligence"), ("tort", "negligence", "damages", "liability", "duty of care")),
    (("知识产权", "知产", "商标", "版权", "专利", "intellectual property", "copyright", "trademark", "patent"), ("intellectual property", "copyright", "trademark", "patent")),
    (("行政", "处罚", "许可", "复议", "审查", "administrative", "tribunal", "judicial review"), ("administrative", "tribunal", "judicial review", "licence", "penalty")),
    (("刑事", "诈骗", "洗钱", "量刑", "定罪", "criminal", "fraud", "offence", "sentencing"), ("criminal", "fraud", "offence", "sentencing", "prosecution")),
    (("消费者", "消费", "误导", "质量", "consumer", "unfair practice"), ("consumer", "consumer protection", "unfair practice", "misrepresentation")),
]


def _expand_canada_keywords(keywords: list[str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()

    def _add(value: str):
        clean = repair_text(value).strip()
        key = clean.lower()
        if not clean or key in seen:
            return
        seen.add(key)
        expanded.append(clean)

    for keyword in keywords or []:
        clean = repair_text(keyword)
        if not clean:
            continue
        _add(clean)
        lowered = clean.lower()
        for markers, aliases in _CANADA_KEYWORD_ALIAS_GROUPS:
            if any(marker in clean or marker in lowered for marker in markers):
                for alias in aliases:
                    _add(alias)
    return expanded


def _fetch_canada_case_rule_corpus(limit: int = 2500) -> list[dict]:
    sql = """
    SELECT
        lc.id AS case_id,
        lc.title AS case_title,
        lc.country AS case_country,
        lc.court_name,
        lc.court_level,
        lc.court_rank,
        lc.case_type,
        lc.summary AS case_summary,
        lc.facts AS case_facts,
        lc.raw_text AS case_raw_text,
        lc.judgment_result,
        lc.judgment_date,
        lc.source_url AS case_source_url,
        lc.source_site AS case_source_site,
        lc.source_item_id AS case_source_item_id,
        lr.id AS rule_id,
        lr.title AS rule_title,
        lr.country AS rule_country,
        lr.legal_type,
        lr.article_no,
        lr.article_text,
        lr.article_summary,
        lr.source_url AS rule_source_url,
        lr.source_site AS rule_source_site,
        lr.slug,
        lr.rule_level,
        crr.relation_type,
        crr.match_score,
        crr.match_reason
    FROM case_rule_relations crr
    JOIN legal_cases lc ON lc.id = crr.case_id
    JOIN legal_rules lr ON lr.id = crr.rule_id
    WHERE lc.country = 'Canada'
      AND lr.country = 'Canada'
    ORDER BY crr.match_score DESC, lc.court_rank DESC, lc.judgment_date DESC NULLS LAST, lc.id DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"limit": int(limit)}).mappings().all()
    return [dict(row) for row in rows]


def _keyword_relation_rows(keywords: list[str], limit_cases: int = 12) -> list[dict]:
    expanded_keywords = _expand_canada_keywords(keywords or [])
    if not expanded_keywords:
        return []
    rows = _fetch_canada_case_rule_corpus()
    scored_rows = []
    for row in rows:
        score = _keyword_match_score(
            expanded_keywords,
            row.get("case_title"),
            row.get("case_summary"),
            row.get("case_facts"),
            row.get("case_raw_text"),
            row.get("judgment_result"),
            row.get("rule_title"),
            row.get("article_no"),
            row.get("article_summary"),
            row.get("article_text"),
            row.get("match_reason"),
        )
        if score <= 0:
            continue
        item = dict(row)
        item["_keyword_score"] = score
        scored_rows.append(item)
    scored_rows.sort(
        key=lambda item: (
            int(item.get("_keyword_score") or 0),
            float(item.get("match_score") or 0),
            int(item.get("court_rank") or 0),
            item.get("judgment_date") or "",
        ),
        reverse=True,
    )
    selected_case_ids: set[int] = set()
    relation_rows: list[dict] = []
    for row in scored_rows:
        relation_rows.append(row)
        selected_case_ids.add(int(row.get("case_id") or 0))
        if len(selected_case_ids) >= limit_cases:
            break
    return relation_rows


def build_canada_case_rule_packet(result_rows: list[dict], refresh: bool = False, keywords: list[str] | None = None) -> dict:
    ensure_legal_data_tables()
    if refresh:
        sync_canada_legal_data(force=True)
    expanded_keywords = _expand_canada_keywords(keywords or [])
    keyword_rows = _keyword_relation_rows(expanded_keywords)
    case_source_ids = [
        int(row["id"])
        for row in result_rows
        if row.get("source_code") in CANADA_CASE_SOURCE_CODES and row.get("id") is not None
    ]
    rule_source_ids = [
        int(row["id"])
        for row in result_rows
        if row.get("source_code") in CANADA_RULE_SOURCE_CODES and row.get("id") is not None
    ]
    relation_rows = _fetch_case_rule_matches(case_source_ids, rule_source_ids)
    if keyword_rows:
        merged_rows: dict[tuple[int, int], dict] = {}
        for row in relation_rows + keyword_rows:
            key = (int(row.get("case_id") or 0), int(row.get("rule_id") or 0))
            if key == (0, 0):
                continue
            existing = merged_rows.get(key)
            if not existing:
                merged_rows[key] = dict(row)
                continue
            existing_keyword = float(existing.get("_keyword_score") or 0)
            row_keyword = float(row.get("_keyword_score") or 0)
            if row_keyword > existing_keyword:
                merged_rows[key] = {**existing, **dict(row)}
            else:
                existing.update({k: v for k, v in dict(row).items() if k not in existing or existing[k] in {None, "", 0}})
        relation_rows = list(merged_rows.values())
    elif not relation_rows:
        relation_rows = keyword_rows
    if not relation_rows:
        return {"relevant_laws": _keyword_only_relevant_laws(expanded_keywords), "case_law_rows": []}

    cases: dict[int, dict] = {}
    law_stats: dict[int, dict[str, Any]] = {}

    for row in relation_rows:
        case_id = int(row["case_id"])
        rule_id = int(row["rule_id"])
        case_entry = cases.setdefault(
            case_id,
            {
                "case_id": case_id,
                "title": repair_text(row.get("case_title")),
                "country": repair_text(row.get("case_country") or "Canada"),
                "court_name": repair_text(row.get("court_name")),
                "court_level": repair_text(row.get("court_level")),
                "court_rank": int(row.get("court_rank") or 0),
                "case_type": repair_text(row.get("case_type") or "Case"),
                "summary": repair_text(row.get("case_summary")),
                "facts": repair_text(row.get("case_facts")),
                "judgment_result": repair_text(row.get("judgment_result")),
                "judgment_date": str(row.get("judgment_date") or "")[:10],
                "source_url": repair_text(row.get("case_source_url")),
                "source_site": repair_text(row.get("case_source_site")),
                "match_score": 0.0,
                "keyword_score": 0.0,
                "match_reason": "",
                "scope": "",
                "rules": [],
            },
        )
        score = float(row.get("match_score") or 0)
        keyword_score = float(row.get("_keyword_score") or 0)
        if score >= float(case_entry.get("match_score") or 0):
            case_entry["match_score"] = score
            case_entry["match_reason"] = repair_text(row.get("match_reason"))
        if keyword_score >= float(case_entry.get("keyword_score") or 0):
            case_entry["keyword_score"] = keyword_score

        scope = _case_scope(case_entry)
        case_entry["scope"] = scope
        rule_entry = {
            "rule_id": rule_id,
            "title": repair_text(row.get("rule_title")),
            "country": repair_text(row.get("rule_country") or "Canada"),
            "legal_type": repair_text(row.get("legal_type") or "law"),
            "article_no": repair_text(row.get("article_no")),
            "article_text": repair_text(row.get("article_text")),
            "article_summary": repair_text(row.get("article_summary")),
            "source_url": repair_text(row.get("rule_source_url")),
            "source_site": repair_text(row.get("rule_source_site")),
            "slug": repair_text(row.get("slug")),
            "rule_level": repair_text(row.get("rule_level")),
            "match_score": score,
            "match_reason": repair_text(row.get("match_reason")),
            "detail_url": f"/law/canada/{repair_text(row.get('slug'))}",
        }
        if all(existing["rule_id"] != rule_id for existing in case_entry["rules"]):
            case_entry["rules"].append(rule_entry)

        stats = law_stats.setdefault(
            rule_id,
            {
                "rule_id": rule_id,
                "title": rule_entry["title"],
                "country": rule_entry["country"],
                "legal_type": rule_entry["legal_type"],
                "article_no": rule_entry["article_no"],
                "article_summary": rule_entry["article_summary"] or plain_text_preview(rule_entry["article_text"])[:280],
                "source_url": rule_entry["source_url"],
                "detail_url": rule_entry["detail_url"],
                "rule_level": rule_entry["rule_level"],
                "linked_case_count": 0,
                "national_case_count": 0,
                "local_case_count": 0,
                "keyword_score": 0.0,
                "related_cases": [],
                "_case_ids": set(),
            },
        )
        stats["keyword_score"] = max(float(stats.get("keyword_score") or 0), keyword_score)
        if case_id not in stats["_case_ids"]:
            stats["_case_ids"].add(case_id)
            case_summary = repair_text(case_entry.get("summary") or case_entry.get("facts"))
            stats["related_cases"].append(
                {
                    "case_id": case_id,
                    "title": repair_text(case_entry.get("title")),
                    "court_level": repair_text(case_entry.get("court_level")),
                    "court_rank": int(case_entry.get("court_rank") or 0),
                    "case_type": repair_text(case_entry.get("case_type")),
                    "judgment_date": repair_text(case_entry.get("judgment_date")),
                    "summary": plain_text_preview(case_summary)[:120],
                    "source_url": repair_text(case_entry.get("source_url")),
                    "scope": scope,
                }
            )
            if scope == "national_federal":
                stats["national_case_count"] += 1
            elif scope == "provincial_local":
                stats["local_case_count"] += 1

    # linked_case_count should count unique cases per rule
    unique_rule_cases: dict[int, set[int]] = defaultdict(set)
    for case in cases.values():
        for rule in case["rules"]:
            unique_rule_cases[int(rule["rule_id"])].add(int(case["case_id"]))
    for rule_id, ids in unique_rule_cases.items():
        law_stats[rule_id]["linked_case_count"] = len(ids)
        law_stats[rule_id]["case_columns"] = [
            {
                "key": "national_federal",
                "label": "国家 / 联邦法院",
                "items": sorted(
                    [item for item in law_stats[rule_id]["related_cases"] if item.get("scope") == "national_federal"],
                    key=lambda entry: (int(entry.get("court_rank") or 0), entry.get("judgment_date") or ""),
                    reverse=True,
                ),
            },
            {
                "key": "provincial_local",
                "label": "省级 / 地方法院",
                "items": sorted(
                    [item for item in law_stats[rule_id]["related_cases"] if item.get("scope") == "provincial_local"],
                    key=lambda entry: (int(entry.get("court_rank") or 0), entry.get("judgment_date") or ""),
                    reverse=True,
                ),
            },
        ]
        law_stats[rule_id].pop("_case_ids", None)

    case_rows = sorted(
        cases.values(),
        key=lambda item: (
            float(item.get("keyword_score") or 0),
            float(item.get("match_score") or 0),
            int(item.get("court_rank") or 0),
            item.get("judgment_date") or "",
        ),
        reverse=True,
    )
    relevant_laws = sorted(
        law_stats.values(),
        key=lambda item: (
            float(item.get("keyword_score") or 0),
            int(item.get("linked_case_count") or 0),
            int(item.get("national_case_count") or 0),
            item.get("title", "").lower(),
        ),
        reverse=True,
    )
    return {"relevant_laws": relevant_laws, "case_law_rows": case_rows}


def get_canada_rule_detail_packet(rule_slug: str) -> dict | None:
    ensure_legal_data_tables()
    if not rule_slug:
        return None
    sql = """
    SELECT *
    FROM legal_rules
    WHERE slug = :slug AND country = 'Canada'
    LIMIT 1
    """
    with engine.connect() as conn:
        rule = conn.execute(text(sql), {"slug": repair_text(rule_slug)}).mappings().first()
        if not rule:
            return None
        case_rows = conn.execute(
            text(
                """
                SELECT
                    lc.*,
                    crr.relation_type,
                    crr.match_score,
                    crr.match_reason
                FROM case_rule_relations crr
                JOIN legal_cases lc ON lc.id = crr.case_id
                WHERE crr.rule_id = :rule_id
                ORDER BY lc.court_rank DESC, crr.match_score DESC, lc.judgment_date DESC NULLS LAST
                """
            ),
            {"rule_id": int(rule["id"])},
        ).mappings().all()
    cases = [dict(row) for row in case_rows]
    national = [item for item in cases if _case_scope(item) == "national_federal"]
    local = [item for item in cases if _case_scope(item) == "provincial_local"]
    return {
        "law": dict(rule),
        "total_cases": len(cases),
        "national_case_count": len(national),
        "local_case_count": len(local),
        "case_columns": [
            {
                "key": "national_federal",
                "label": "国家 / 联邦法院",
                "label_en": "National / Federal Courts",
                "description": "优先查看更高位阶、全国性或联邦体系的裁判。",
                "items": national,
                "empty_copy": "当前没有已关联的国家 / 联邦层面案例。",
            },
            {
                "key": "provincial_local",
                "label": "省级 / 地方法院",
                "label_en": "Provincial / Local Courts",
                "description": "这里展示更贴近地方实践的省级和地方层面裁判。",
                "items": local,
                "empty_copy": "当前没有已关联的省级 / 地方层面案例。",
            },
        ],
    }


def list_cases(
    *,
    country: str = "",
    case_type: str = "",
    court_level: str = "",
    rule_keyword: str = "",
    keyword: str = "",
    source_site: str = "",
    limit: int = 100,
) -> list[dict]:
    conditions = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
    joins = ""
    if country:
        conditions.append("LOWER(lc.country) = LOWER(:country)")
        params["country"] = repair_text(country)
    if case_type:
        conditions.append("LOWER(lc.case_type) = LOWER(:case_type)")
        params["case_type"] = repair_text(case_type)
    if court_level:
        conditions.append("LOWER(lc.court_level) LIKE LOWER(:court_level)")
        params["court_level"] = f"%{repair_text(court_level)}%"
    if source_site:
        conditions.append("LOWER(lc.source_site) LIKE LOWER(:source_site)")
        params["source_site"] = f"%{repair_text(source_site)}%"
    if keyword:
        conditions.append("(LOWER(lc.title) LIKE LOWER(:keyword) OR LOWER(lc.summary) LIKE LOWER(:keyword) OR LOWER(lc.raw_text) LIKE LOWER(:keyword))")
        params["keyword"] = f"%{repair_text(keyword)}%"
    if rule_keyword:
        joins = """
        LEFT JOIN case_rule_relations crr_filter ON crr_filter.case_id = lc.id
        LEFT JOIN legal_rules lr_filter ON lr_filter.id = crr_filter.rule_id
        """
        conditions.append("LOWER(lr_filter.title) LIKE LOWER(:rule_keyword)")
        params["rule_keyword"] = f"%{repair_text(rule_keyword)}%"
    sql = f"""
    SELECT
        lc.*,
        COUNT(DISTINCT crr.rule_id) AS related_rule_count
    FROM legal_cases lc
    {joins}
    LEFT JOIN case_rule_relations crr ON crr.case_id = lc.id
    WHERE {' AND '.join(conditions)}
    GROUP BY lc.id
    ORDER BY lc.updated_at DESC, lc.id DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def get_case(case_id: int) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM legal_cases WHERE id = :case_id LIMIT 1"), {"case_id": int(case_id)}).mappings().first()
    return dict(row) if row else None


def list_rules(
    *,
    country: str = "",
    legal_type: str = "",
    article_no: str = "",
    keyword: str = "",
    limit: int = 100,
) -> list[dict]:
    conditions = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
    if country:
        conditions.append("LOWER(lr.country) = LOWER(:country)")
        params["country"] = repair_text(country)
    if legal_type:
        conditions.append("LOWER(lr.legal_type) LIKE LOWER(:legal_type)")
        params["legal_type"] = f"%{repair_text(legal_type)}%"
    if article_no:
        conditions.append("LOWER(lr.article_no) LIKE LOWER(:article_no)")
        params["article_no"] = f"%{repair_text(article_no)}%"
    if keyword:
        conditions.append("(LOWER(lr.title) LIKE LOWER(:keyword) OR LOWER(lr.article_summary) LIKE LOWER(:keyword) OR LOWER(lr.article_text) LIKE LOWER(:keyword))")
        params["keyword"] = f"%{repair_text(keyword)}%"
    sql = f"""
    SELECT
        lr.*,
        COUNT(DISTINCT crr.case_id) AS related_case_count
    FROM legal_rules lr
    LEFT JOIN case_rule_relations crr ON crr.rule_id = lr.id
    WHERE {' AND '.join(conditions)}
    GROUP BY lr.id
    ORDER BY related_case_count DESC, lr.updated_at DESC, lr.id DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def get_rule(rule_id: int) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM legal_rules WHERE id = :rule_id LIMIT 1"), {"rule_id": int(rule_id)}).mappings().first()
    return dict(row) if row else None


def list_case_rule_relations(*, case_id: int | None = None, rule_id: int | None = None, limit: int = 200) -> list[dict]:
    conditions = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if case_id:
        conditions.append("crr.case_id = :case_id")
        params["case_id"] = int(case_id)
    if rule_id:
        conditions.append("crr.rule_id = :rule_id")
        params["rule_id"] = int(rule_id)
    sql = f"""
    SELECT
        crr.*,
        lc.title AS case_title,
        lr.title AS rule_title
    FROM case_rule_relations crr
    JOIN legal_cases lc ON lc.id = crr.case_id
    JOIN legal_rules lr ON lr.id = crr.rule_id
    WHERE {' AND '.join(conditions)}
    ORDER BY crr.created_at DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _create_import_task(*, created_by: int | None, import_type: str, country: str, source_url: str, payload: dict | None) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO import_tasks (
                    created_by,
                    import_type,
                    country,
                    source_url,
                    status,
                    total_count,
                    success_count,
                    fail_count,
                    error_message,
                    payload_json,
                    created_at,
                    updated_at
                )
                VALUES (
                    :created_by,
                    :import_type,
                    :country,
                    :source_url,
                    'running',
                    0,
                    0,
                    0,
                    '',
                    CAST(:payload_json AS jsonb),
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                RETURNING id
                """
            ),
            {
                "created_by": created_by,
                "import_type": repair_text(import_type),
                "country": repair_text(country),
                "source_url": repair_text(source_url),
                "payload_json": json.dumps(payload or {}, ensure_ascii=False),
            },
        ).mappings().first()
    return int(row["id"]) if row else 0


def _finish_import_task(task_id: int, *, status: str, total_count: int, success_count: int, fail_count: int, error_message: str = ""):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE import_tasks
                SET status = :status,
                    total_count = :total_count,
                    success_count = :success_count,
                    fail_count = :fail_count,
                    error_message = :error_message,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :task_id
                """
            ),
            {
                "task_id": int(task_id),
                "status": repair_text(status),
                "total_count": int(total_count),
                "success_count": int(success_count),
                "fail_count": int(fail_count),
                "error_message": repair_text(error_message),
            },
        )


def list_import_tasks(limit: int = 100) -> list[dict]:
    sql = """
    SELECT
        it.*,
        u.username AS created_by_username
    FROM import_tasks it
    LEFT JOIN users u ON u.id = it.created_by
    ORDER BY it.created_at DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"limit": max(1, min(int(limit), 500))}).mappings().all()
    return [dict(row) for row in rows]


def _respect_robots_and_delay(target_url: str):
    parsed = urlparse(target_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = f"{base}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except Exception:
        pass
    if parser.default_entry and not parser.can_fetch("*", target_url):
        raise ValueError("robots.txt 不允许抓取该地址。")

    now = time.time()
    last = _DOMAIN_FETCH_STATE.get(parsed.netloc, 0.0)
    min_interval = max(1.0, float(getattr(settings, "request_timeout", 30)) / 30.0)
    wait_seconds = min_interval - (now - last)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    _DOMAIN_FETCH_STATE[parsed.netloc] = time.time()


def _fetch_url_text(target_url: str) -> dict:
    _respect_robots_and_delay(target_url)
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        target_url,
        timeout=settings.request_timeout,
        headers={"User-Agent": "Legal Demo Import Bot/1.0"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    title = repair_text((soup.title.string if soup.title else "") or "")
    text_value = repair_text(" ".join(soup.stripped_strings))
    return {
        "title": title or target_url,
        "text": text_value[:20000],
        "source_url": target_url,
        "source_site": urlparse(target_url).netloc,
    }


def _upsert_manual_case(
    *,
    title: str,
    country: str,
    court_name: str,
    court_level: str,
    case_type: str,
    summary: str,
    facts: str,
    judgment_result: str,
    source_url: str,
    source_site: str,
    raw_text: str,
    source_code: str,
) -> int:
    meta = {
        "country": country,
        "court_name": court_name,
        "court_level": court_level,
        "case_type": case_type,
        "facts": facts,
        "judgment_result": judgment_result,
        "source_site": source_site,
    }
    source_uid = f"{source_code}:{sha256_text((source_url or title) + '|' + title)[:24]}"
    item_id = upsert_source_item(
        source_code=source_code,
        source_uid=source_uid,
        title=title,
        item_url=source_url,
        summary=summary,
        raw_text=raw_text or facts or summary,
        raw_json=meta,
    )
    row = {
        "id": item_id,
        "source_code": source_code,
        "source_uid": source_uid,
        "title": title,
        "item_url": source_url,
        "published_at": None,
        "summary": summary,
        "raw_text": raw_text or facts or summary,
        "raw_json": meta,
    }
    return _upsert_legal_case_from_source(row)


def _upsert_manual_rule(
    *,
    title: str,
    country: str,
    legal_type: str,
    article_no: str,
    article_text: str,
    article_summary: str,
    source_url: str,
    source_site: str,
    source_code: str,
) -> int:
    source_uid = f"{source_code}:{sha256_text((source_url or title) + '|' + title)[:24]}"
    item_id = upsert_source_item(
        source_code=source_code,
        source_uid=source_uid,
        title=title,
        item_url=source_url,
        summary=article_summary or article_text[:1000],
        raw_text=article_text or article_summary or title,
        raw_json={
            "country": country,
            "legal_type": legal_type,
            "citation": article_no,
            "source_site": source_site,
        },
    )
    sql = """
    INSERT INTO legal_rules (
        title,
        country,
        legal_type,
        article_no,
        article_text,
        article_summary,
        source_url,
        source_site,
        source_item_id,
        normalized_title,
        slug,
        rule_level,
        citation,
        created_at,
        updated_at
    )
    VALUES (
        :title,
        :country,
        :legal_type,
        :article_no,
        :article_text,
        :article_summary,
        :source_url,
        :source_site,
        :source_item_id,
        :normalized_title,
        :slug,
        :rule_level,
        :citation,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    )
    ON CONFLICT (source_item_id)
    DO UPDATE SET
        title = EXCLUDED.title,
        country = EXCLUDED.country,
        legal_type = EXCLUDED.legal_type,
        article_no = EXCLUDED.article_no,
        article_text = EXCLUDED.article_text,
        article_summary = EXCLUDED.article_summary,
        source_url = EXCLUDED.source_url,
        source_site = EXCLUDED.source_site,
        normalized_title = EXCLUDED.normalized_title,
        slug = EXCLUDED.slug,
        rule_level = EXCLUDED.rule_level,
        citation = EXCLUDED.citation,
        updated_at = CURRENT_TIMESTAMP
    RETURNING id
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(sql),
            {
                "title": repair_text(title),
                "country": repair_text(country),
                "legal_type": repair_text(legal_type),
                "article_no": repair_text(article_no),
                "article_text": repair_text(article_text)[:20000],
                "article_summary": repair_text(article_summary)[:4000],
                "source_url": repair_text(source_url),
                "source_site": repair_text(source_site),
                "source_item_id": item_id,
                "normalized_title": _normalize_title(title),
                "slug": _slugify(title),
                "rule_level": repair_text(legal_type),
                "citation": repair_text(article_no),
            },
        ).mappings().first()
    return int(row["id"]) if row else 0


def _auto_link_case(case_id: int):
    case = get_case(case_id)
    if not case:
        return
    text_value = " ".join(
        [
            repair_text(case.get("title")),
            repair_text(case.get("summary")),
            repair_text(case.get("facts")),
            repair_text(case.get("raw_text")),
        ]
    ).lower()
    if not text_value:
        return
    rules = list_rules(country=repair_text(case.get("country")), limit=500)
    with engine.begin() as conn:
        for rule in rules:
            title = repair_text(rule.get("title"))
            if len(title) < 6:
                continue
            if title.lower() not in text_value:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO case_rule_relations (
                        case_id,
                        rule_id,
                        relation_type,
                        match_score,
                        match_reason,
                        created_at
                    )
                    VALUES (
                        :case_id,
                        :rule_id,
                        'manual_link',
                        0.75,
                        :match_reason,
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (case_id, rule_id)
                    DO UPDATE SET
                        relation_type = EXCLUDED.relation_type,
                        match_score = EXCLUDED.match_score,
                        match_reason = EXCLUDED.match_reason
                    """
                ),
                {
                    "case_id": int(case_id),
                    "rule_id": int(rule["id"]),
                    "match_reason": f"Manual/URL imported case text directly mentions rule title: {title}",
                },
            )


def _auto_link_rule(rule_id: int):
    rule = get_rule(rule_id)
    if not rule:
        return
    title = repair_text(rule.get("title"))
    if len(title) < 6:
        return
    cases = list_cases(country=repair_text(rule.get("country")), limit=1000)
    with engine.begin() as conn:
        for case in cases:
            text_value = " ".join(
                [
                    repair_text(case.get("title")),
                    repair_text(case.get("summary")),
                    repair_text(case.get("facts")),
                    repair_text(case.get("raw_text")),
                ]
            ).lower()
            if title.lower() not in text_value:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO case_rule_relations (
                        case_id,
                        rule_id,
                        relation_type,
                        match_score,
                        match_reason,
                        created_at
                    )
                    VALUES (
                        :case_id,
                        :rule_id,
                        'manual_link',
                        0.75,
                        :match_reason,
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (case_id, rule_id)
                    DO UPDATE SET
                        relation_type = EXCLUDED.relation_type,
                        match_score = EXCLUDED.match_score,
                        match_reason = EXCLUDED.match_reason
                    """
                ),
                {
                    "case_id": int(case["id"]),
                    "rule_id": int(rule_id),
                    "match_reason": f"Manual/URL imported rule title directly appears in case text: {title}",
                },
            )


def import_manual_entry(
    *,
    created_by: int | None,
    data_type: str,
    country: str,
    legal_type: str = "",
    case_type: str = "",
    court_name: str = "",
    court_level: str = "",
    title: str,
    summary: str = "",
    facts: str = "",
    judgment_result: str = "",
    article_no: str = "",
    article_text: str = "",
    article_summary: str = "",
    source_url: str = "",
    auto_link: bool = False,
) -> dict:
    ensure_legal_data_tables()
    task_id = _create_import_task(
        created_by=created_by,
        import_type="manual",
        country=country,
        source_url=source_url,
        payload={"data_type": data_type, "title": title},
    )
    try:
        if repair_text(data_type).lower() == "case":
            source_code = "manual_canada_case" if repair_text(country).lower() == "canada" else "manual_case"
            case_id = _upsert_manual_case(
                title=title,
                country=country,
                court_name=court_name,
                court_level=court_level,
                case_type=case_type or "Case",
                summary=summary,
                facts=facts,
                judgment_result=judgment_result,
                source_url=source_url,
                source_site="Manual Import",
                raw_text=facts or summary,
                source_code=source_code,
            )
            if auto_link:
                _auto_link_case(case_id)
            _finish_import_task(task_id, status="completed", total_count=1, success_count=1, fail_count=0)
            return {"task_id": task_id, "case_id": case_id}
        source_code = "manual_canada_rule" if repair_text(country).lower() == "canada" else "manual_rule"
        rule_id = _upsert_manual_rule(
            title=title,
            country=country,
            legal_type=legal_type or "law",
            article_no=article_no,
            article_text=article_text,
            article_summary=article_summary,
            source_url=source_url,
            source_site="Manual Import",
            source_code=source_code,
        )
        if auto_link:
            _auto_link_rule(rule_id)
        _finish_import_task(task_id, status="completed", total_count=1, success_count=1, fail_count=0)
        return {"task_id": task_id, "rule_id": rule_id}
    except Exception as exc:
        _finish_import_task(task_id, status="failed", total_count=1, success_count=0, fail_count=1, error_message=str(exc))
        raise


def import_from_url(
    *,
    created_by: int | None,
    target_url: str,
    country: str,
    data_type: str,
    legal_type: str = "",
    case_type: str = "",
    court_level: str = "",
    auto_link: bool = False,
) -> dict:
    ensure_legal_data_tables()
    task_id = _create_import_task(
        created_by=created_by,
        import_type="url",
        country=country,
        source_url=target_url,
        payload={
            "data_type": data_type,
            "legal_type": legal_type,
            "case_type": case_type,
            "court_level": court_level,
            "auto_link": auto_link,
        },
    )
    try:
        fetched = _fetch_url_text(target_url)
        summary = plain_text_preview(fetched["text"])[:1000]
        if repair_text(data_type).lower() == "case":
            source_code = "url_canada_case" if repair_text(country).lower() == "canada" else "url_case"
            case_id = _upsert_manual_case(
                title=fetched["title"],
                country=country,
                court_name=fetched["source_site"],
                court_level=court_level or "未指定",
                case_type=case_type or "Imported Case",
                summary=summary,
                facts=fetched["text"][:8000],
                judgment_result="",
                source_url=target_url,
                source_site=fetched["source_site"],
                raw_text=fetched["text"],
                source_code=source_code,
            )
            if auto_link:
                _auto_link_case(case_id)
            _finish_import_task(task_id, status="completed", total_count=1, success_count=1, fail_count=0)
            return {"task_id": task_id, "case_id": case_id}
        source_code = "url_canada_rule" if repair_text(country).lower() == "canada" else "url_rule"
        rule_id = _upsert_manual_rule(
            title=fetched["title"],
            country=country,
            legal_type=legal_type or "Imported Rule",
            article_no="",
            article_text=fetched["text"],
            article_summary=summary,
            source_url=target_url,
            source_site=fetched["source_site"],
            source_code=source_code,
        )
        if auto_link:
            _auto_link_rule(rule_id)
        _finish_import_task(task_id, status="completed", total_count=1, success_count=1, fail_count=0)
        return {"task_id": task_id, "rule_id": rule_id}
    except Exception as exc:
        _finish_import_task(task_id, status="failed", total_count=1, success_count=0, fail_count=1, error_message=str(exc))
        raise


def run_canada_crawler_import(created_by: int | None = None) -> dict:
    ensure_legal_data_tables()
    task_id = _create_import_task(
        created_by=created_by,
        import_type="crawler",
        country="Canada",
        source_url="",
        payload={"source": "canada_bulk_sync"},
    )
    try:
        legislation_result = sync_canada_legislation_demo()
        canlii_result = sync_canlii_demo()
        sync_summary = sync_canada_legal_data(force=True)
        total = int(legislation_result.get("processed") or 0) + int(canlii_result.get("processed") or 0)
        _finish_import_task(
            task_id,
            status="completed",
            total_count=total,
            success_count=total,
            fail_count=0,
            error_message="",
        )
        return {
            "task_id": task_id,
            "legislation_result": legislation_result,
            "canlii_result": canlii_result,
            "sync_summary": sync_summary,
        }
    except Exception as exc:
        _finish_import_task(task_id, status="failed", total_count=1, success_count=0, fail_count=1, error_message=str(exc))
        raise
