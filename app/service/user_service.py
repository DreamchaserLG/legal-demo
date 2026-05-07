import base64
import hashlib
import hmac
import json
import os
import re
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import engine
from app.service.common_service import repair_text

SESSION_USER_ID_KEY = "user_id"
SESSION_LAST_QUERY_URL_KEY = "last_query_url"
SESSION_LAST_QUERY_LABEL_KEY = "last_query_label"
_PBKDF2_ITERATIONS = 200_000
_DEV_HISTORY_PATTERNS = [
    "请你先完整阅读当前项目结构",
    "帮我完成上述功能",
    "管理员页面",
    "数据库结构补充",
    "/api/admin/",
    "user_id + case_hash",
    "接口调整",
]


def ensure_user_tables():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username VARCHAR(80) NOT NULL,
            password_hash TEXT NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'user',
            email VARCHAR(255) NOT NULL,
            phone VARCHAR(50) NOT NULL DEFAULT '',
            organization VARCHAR(255) NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_login_at TIMESTAMP NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active'
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username
        ON users (LOWER(username))
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email
        ON users (LOWER(email))
        """,
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            real_name TEXT NOT NULL DEFAULT '',
            country_preference TEXT NOT NULL DEFAULT '',
            legal_type_preference TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS search_histories (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            query_text TEXT NOT NULL DEFAULT '',
            query_type VARCHAR(30) NOT NULL DEFAULT 'analysis',
            country TEXT NOT NULL DEFAULT '',
            court_level TEXT NOT NULL DEFAULT '',
            legal_type TEXT NOT NULL DEFAULT '',
            result_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            case_hash TEXT NOT NULL DEFAULT '',
            case_title TEXT NOT NULL DEFAULT '',
            case_summary TEXT NOT NULL DEFAULT '',
            case_type TEXT NOT NULL DEFAULT '',
            key_facts_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            dispute_focus_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            legal_rule_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            legal_rules_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            risk_points_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            model_analysis_summary TEXT NOT NULL DEFAULT '',
            prediction_label TEXT NOT NULL DEFAULT '',
            prediction_conclusion TEXT NOT NULL DEFAULT '',
            prediction_explanation TEXT NOT NULL DEFAULT '',
            prediction_confidence NUMERIC(8, 4) NOT NULL DEFAULT 0,
            confidence NUMERIC(8, 4) NOT NULL DEFAULT 0,
            graph_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            view_count INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_viewed_at TIMESTAMP NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active'
        )
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS case_hash TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS case_title TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS case_type TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS case_summary TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS key_facts_json JSONB NOT NULL DEFAULT '[]'::jsonb
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS dispute_focus_json JSONB NOT NULL DEFAULT '[]'::jsonb
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS legal_rule_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS legal_rules_json JSONB NOT NULL DEFAULT '[]'::jsonb
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS risk_points_json JSONB NOT NULL DEFAULT '[]'::jsonb
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS model_analysis_summary TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS prediction_label TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS prediction_conclusion TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS prediction_explanation TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS prediction_confidence NUMERIC(8, 4) NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS confidence NUMERIC(8, 4) NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS graph_json JSONB NOT NULL DEFAULT '{}'::jsonb
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS view_count INTEGER NOT NULL DEFAULT 1
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS last_viewed_at TIMESTAMP NULL
        """,
        """
        ALTER TABLE search_histories
        ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_search_histories_user_viewed
        ON search_histories (user_id, COALESCE(last_viewed_at, updated_at, created_at) DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_search_histories_case_title
        ON search_histories (LOWER(case_title))
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_search_histories_country_level
        ON search_histories (country, court_level)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_search_histories_user_case_hash_active
        ON search_histories (user_id, case_hash)
        WHERE case_hash <> '' AND status = 'active'
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    _ensure_default_users()
    cleanup_invalid_histories()
    repair_incomplete_histories()
    deduplicate_histories()
    seed_demo_histories()


def _build_available_email(conn, username: str, preferred_email: str) -> str:
    preferred = repair_text(preferred_email).lower() or f"{repair_text(username).lower()}@local.test"
    local_part, separator, domain = preferred.partition("@")
    if not separator:
        local_part = preferred
        domain = "local.test"

    counter = 0
    while True:
        suffix = f"+{counter}" if counter else ""
        candidate = f"{local_part}{suffix}@{domain}"
        exists = conn.execute(
            text(
                """
                SELECT id
                FROM users
                WHERE LOWER(email) = :email
                LIMIT 1
                """
            ),
            {"email": candidate.lower()},
        ).mappings().first()
        if not exists:
            return candidate
        counter += 1


def _upsert_seed_user(*, username: str, password: str, role: str, preferred_email: str):
    clean_username = repair_text(username)
    clean_role = "admin" if str(role or "").strip().lower() == "admin" else "user"
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, email
                FROM users
                WHERE LOWER(username) = :username
                LIMIT 1
                """
            ),
            {"username": clean_username.lower()},
        ).mappings().first()
        if row:
            email_value = repair_text(row.get("email") or "") or _build_available_email(conn, clean_username, preferred_email)
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET username = :username,
                        password_hash = :password_hash,
                        role = :role,
                        email = :email,
                        status = 'active',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :user_id
                    """
                ),
                {
                    "user_id": int(row["id"]),
                    "username": clean_username,
                    "password_hash": hash_password(password),
                    "role": clean_role,
                    "email": email_value,
                },
            )
            user_id = int(row["id"])
        else:
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO users (
                        username,
                        password_hash,
                        role,
                        email,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :username,
                        :password_hash,
                        :role,
                        :email,
                        'active',
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    RETURNING id
                    """
                ),
                {
                    "username": clean_username,
                    "password_hash": hash_password(password),
                    "role": clean_role,
                    "email": _build_available_email(conn, clean_username, preferred_email),
                },
            ).mappings().first()
            user_id = int(inserted["id"])

        conn.execute(
            text(
                """
                INSERT INTO user_profiles (user_id, updated_at)
                VALUES (:user_id, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO NOTHING
                """
            ),
            {"user_id": user_id},
        )


def _ensure_default_users():
    username = repair_text(settings.initial_admin_username)
    password = str(settings.initial_admin_password or "").strip()
    preferred_email = repair_text(settings.initial_admin_email) or f"{username}@local.test"
    if not username or not password:
        return
    _upsert_seed_user(
        username=username,
        password=password,
        role="admin",
        preferred_email=preferred_email,
    )


def hash_password(password: str) -> str:
    clean = str(password or "")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", clean.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iteration_text, salt_text, digest_text = str(password_hash or "").split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iteration_text)
        salt = base64.b64decode(salt_text.encode("ascii"))
        expected = base64.b64decode(digest_text.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _fetch_user_row(where_sql: str, params: dict[str, Any]) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"""
                SELECT
                    u.*,
                    p.real_name,
                    p.country_preference,
                    p.legal_type_preference,
                    p.note
                FROM users u
                LEFT JOIN user_profiles p ON p.user_id = u.id
                WHERE {where_sql}
                LIMIT 1
                """
            ),
            params,
        ).mappings().first()
    return dict(row) if row else None


def sanitize_user(row: dict | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data.pop("password_hash", None)
    data["is_admin"] = str(data.get("role") or "").strip().lower() == "admin"
    data["is_active"] = str(data.get("status") or "").strip().lower() == "active"
    return data


def get_user_by_id(user_id: int | None) -> dict | None:
    if not user_id:
        return None
    return sanitize_user(_fetch_user_row("u.id = :user_id", {"user_id": int(user_id)}))


def get_user_by_login(login_value: str) -> dict | None:
    clean = repair_text(login_value).lower()
    if not clean:
        return None
    return sanitize_user(
        _fetch_user_row("LOWER(u.username) = :login OR LOWER(u.email) = :login", {"login": clean})
    )


def _legacy_register_user(*, username: str, password: str, email: str) -> dict:
    clean_username = repair_text(username)
    clean_email = repair_text(email).lower()
    if len(clean_username) < 2:
        raise HTTPException(status_code=400, detail="用户名至少需要 2 个字符。")
    if len(str(password or "")) < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 个字符。")
    if clean_email and "@" not in clean_email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确。")

    with engine.begin() as conn:
        existing = conn.execute(
            text(
                """
                SELECT id
                FROM users
                WHERE LOWER(username) = :username OR LOWER(email) = :email
                LIMIT 1
                """
            ),
            {"username": clean_username.lower(), "email": clean_email or "__no_email__"},
        ).mappings().first()
        if existing:
            raise HTTPException(status_code=409, detail="用户名或邮箱已存在。")

        email_value = clean_email or _build_available_email(conn, clean_username, f"{clean_username}@local.test")
        row = conn.execute(
            text(
                """
                INSERT INTO users (
                    username,
                    password_hash,
                    role,
                    email,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (
                    :username,
                    :password_hash,
                    'user',
                    :email,
                    'active',
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                RETURNING id
                """
            ),
            {
                "username": clean_username,
                "password_hash": hash_password(password),
                "email": email_value,
            },
        ).mappings().first()
        conn.execute(
            text(
                """
                INSERT INTO user_profiles (user_id, updated_at)
                VALUES (:user_id, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO NOTHING
                """
            ),
            {"user_id": int(row["id"])},
        )
    return get_user_by_id(int(row["id"])) or {}


def register_user(*, username: str, password: str, email: str) -> dict:
    clean_username = repair_text(username)
    clean_email = repair_text(email).lower()
    if not clean_username:
        raise HTTPException(status_code=400, detail="用户名不能为空。")
    if len(clean_username) < 2:
        raise HTTPException(status_code=400, detail="用户名至少需要 2 个字符。")
    if len(str(password or "")) < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 个字符。")
    if clean_email and "@" not in clean_email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确。")

    try:
        with engine.begin() as conn:
            existing_username = conn.execute(
                text(
                    """
                    SELECT id
                    FROM users
                    WHERE LOWER(username) = :username
                    LIMIT 1
                    """
                ),
                {"username": clean_username.lower()},
            ).mappings().first()
            if existing_username:
                raise HTTPException(status_code=409, detail="用户名已存在。")

            existing_email = None
            if clean_email:
                existing_email = conn.execute(
                    text(
                        """
                        SELECT id
                        FROM users
                        WHERE LOWER(email) = :email
                        LIMIT 1
                        """
                    ),
                    {"email": clean_email},
                ).mappings().first()
            if existing_email:
                raise HTTPException(status_code=409, detail="邮箱已存在。")

            email_value = clean_email or _build_available_email(conn, clean_username, f"{clean_username}@local.test")
            row = conn.execute(
                text(
                    """
                    INSERT INTO users (
                        username,
                        password_hash,
                        role,
                        email,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :username,
                        :password_hash,
                        'user',
                        :email,
                        'active',
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    RETURNING id
                    """
                ),
                {
                    "username": clean_username,
                    "password_hash": hash_password(password),
                    "email": email_value,
                },
            ).mappings().first()
            user_id = int(row["id"])
            conn.execute(
                text(
                    """
                    INSERT INTO user_profiles (user_id, updated_at)
                    VALUES (:user_id, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id) DO NOTHING
                    """
                ),
                {"user_id": user_id},
            )
    except IntegrityError as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if "uq_users_username" in message or "username" in message:
            raise HTTPException(status_code=409, detail="用户名已存在。") from exc
        if "uq_users_email" in message or "email" in message:
            raise HTTPException(status_code=409, detail="邮箱已存在。") from exc
        raise

    return get_user_by_id(user_id) or {}


def authenticate_user(login_value: str, password: str) -> dict:
    clean = repair_text(login_value).lower()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    u.*,
                    p.real_name,
                    p.country_preference,
                    p.legal_type_preference,
                    p.note
                FROM users u
                LEFT JOIN user_profiles p ON p.user_id = u.id
                WHERE LOWER(u.username) = :login OR LOWER(u.email) = :login
                LIMIT 1
                """
            ),
            {"login": clean},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=401, detail="用户名、邮箱或密码错误。")
        record = dict(row)
        if str(record.get("status") or "").strip().lower() != "active":
            raise HTTPException(status_code=403, detail="当前账号已被禁用。")
        if not verify_password(password, record.get("password_hash", "")):
            raise HTTPException(status_code=401, detail="用户名、邮箱或密码错误。")
        conn.execute(
            text(
                """
                UPDATE users
                SET last_login_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :user_id
                """
            ),
            {"user_id": int(record["id"])},
        )
    return get_user_by_id(int(record["id"])) or {}


def set_session_user(request: Request, user: dict):
    request.session[SESSION_USER_ID_KEY] = int(user["id"])


def clear_session_user(request: Request):
    request.session.clear()


def remember_last_query(request: Request, url: str, label: str = ""):
    request.session[SESSION_LAST_QUERY_URL_KEY] = repair_text(url)
    request.session[SESSION_LAST_QUERY_LABEL_KEY] = repair_text(label)


def get_last_query(request: Request) -> dict:
    return {
        "url": repair_text(request.session.get(SESSION_LAST_QUERY_URL_KEY)),
        "label": repair_text(request.session.get(SESSION_LAST_QUERY_LABEL_KEY)),
    }


def get_current_user(request: Request) -> dict | None:
    return get_user_by_id(request.session.get(SESSION_USER_ID_KEY))


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="登录状态已过期，请重新登录")
    if not user.get("is_active"):
        request.session.clear()
        raise HTTPException(status_code=403, detail="当前账号已被禁用。")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="当前账号无权限访问该页面")
    return user


def update_user_profile(
    user_id: int,
    *,
    real_name: str = "",
    phone: str = "",
    organization: str = "",
    country_preference: str = "",
    legal_type_preference: str = "",
    note: str = "",
    email: str = "",
) -> dict:
    clean_email = repair_text(email).lower()
    if clean_email and "@" not in clean_email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确。")
    with engine.begin() as conn:
        if clean_email:
            existing = conn.execute(
                text(
                    """
                    SELECT id
                    FROM users
                    WHERE LOWER(email) = :email AND id <> :user_id
                    LIMIT 1
                    """
                ),
                {"email": clean_email, "user_id": int(user_id)},
            ).mappings().first()
            if existing:
                raise HTTPException(status_code=409, detail="邮箱已被其他用户使用。")
        conn.execute(
            text(
                """
                UPDATE users
                SET email = COALESCE(NULLIF(:email, ''), email),
                    phone = :phone,
                    organization = :organization,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :user_id
                """
            ),
            {
                "user_id": int(user_id),
                "email": clean_email,
                "phone": repair_text(phone),
                "organization": repair_text(organization),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO user_profiles (
                    user_id,
                    real_name,
                    country_preference,
                    legal_type_preference,
                    note,
                    updated_at
                )
                VALUES (
                    :user_id,
                    :real_name,
                    :country_preference,
                    :legal_type_preference,
                    :note,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (user_id)
                DO UPDATE SET
                    real_name = EXCLUDED.real_name,
                    country_preference = EXCLUDED.country_preference,
                    legal_type_preference = EXCLUDED.legal_type_preference,
                    note = EXCLUDED.note,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "user_id": int(user_id),
                "real_name": repair_text(real_name),
                "country_preference": repair_text(country_preference),
                "legal_type_preference": repair_text(legal_type_preference),
                "note": repair_text(note),
            },
        )
    return get_user_by_id(user_id) or {}


def update_user_password(user_id: int, current_password: str, new_password: str) -> bool:
    if len(str(new_password or "")) < 6:
        raise HTTPException(status_code=400, detail="新密码至少需要 6 个字符。")
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT password_hash FROM users WHERE id = :user_id LIMIT 1"),
            {"user_id": int(user_id)},
        ).mappings().first()
        if not row or not verify_password(current_password, row.get("password_hash", "")):
            raise HTTPException(status_code=400, detail="当前密码不正确。")
        conn.execute(
            text(
                """
                UPDATE users
                SET password_hash = :password_hash,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :user_id
                """
            ),
            {"user_id": int(user_id), "password_hash": hash_password(new_password)},
        )
    return True


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe_list(values: Any, limit: int | None = None) -> list[str]:
    items: list[str] = []
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


def _split_case_sentences(text: str) -> list[str]:
    return [
        part.strip(" ，,；;。")
        for part in re.split(r"[。！？!?；;\n]+", repair_text(text))
        if part.strip(" ，,；;。")
    ]


def normalize_case_text(text: str) -> str:
    normalized = repair_text(text).lower()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub("[^\\w一-鿿 ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def build_case_hash(query_text: str, case_type: str = "", country: str = "", court_level: str = "") -> str:
    normalized = "|".join(
        [
            normalize_case_text(query_text),
            repair_text(case_type).lower(),
            repair_text(country).lower(),
            repair_text(court_level).lower(),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _derive_case_type_label(*parts: str, legal_topics: list[str] | None = None) -> str:
    topics = _safe_list(legal_topics or [], limit=4)
    combined = normalize_case_text(" ".join([*topics, *[repair_text(part) for part in parts]]))
    rules = [
        (["contract", "lease", "rent", "sale", "supply", "agreement"], "Contract Dispute"),
        (["employment", "labour", "labor", "overtime", "dismiss", "wage", "wrongful dismissal"], "Labour Dispute"),
        (["tort", "negligence", "injury", "damage", "slip", "occupier"], "Tort Liability"),
        (["privacy", "personal data", "tracking", "platform", "algorithm", "meta", "google", "instagram", "youtube", "flo"], "Privacy and Data"),
        (["intellectual", "copyright", "trademark", "patent", "license"], "Intellectual Property"),
        (["administrative", "licensing", "permit", "penalty", "municipal"], "Administrative Review"),
        (["criminal", "fraud", "theft", "wallet", "investor"], "Criminal Risk"),
        (["bribery", "kickback", "corruption", "medical device", "hospital", "doctor"], "Anti-Corruption Risk"),
        (["consumer", "warranty", "defect", "retailer", "appliance"], "Consumer Protection"),
        (["trust", "estate", "inheritance", "beneficiar", "probate"], "Trust and Estate"),
        (["shareholder", "company", "board", "corporate", "director"], "Corporate Governance"),
        (["sanction", "ofac", "export", "screening"], "Sanctions Compliance"),
    ]
    for keywords, label in rules:
        if any(keyword in combined for keyword in keywords):
            return label
    if topics:
        joined = " / ".join(topics)
        if len(joined) <= 48:
            return joined
        return topics[0]
    return "Case Analysis"


def _derive_case_title(query_text: str, analysis_summary: str = "") -> str:
    title = repair_text(analysis_summary) or repair_text(query_text)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:72] or "未命名案件"


def _extract_rule_ids(module_packet: dict | None) -> list[int]:
    rule_ids: list[int] = []
    for law in (module_packet or {}).get("relevant_laws", []):
        try:
            rule_id = int(law.get("rule_id"))
        except (TypeError, ValueError):
            continue
        if rule_id not in rule_ids:
            rule_ids.append(rule_id)
    for row in (module_packet or {}).get("case_law_rows", []):
        for law in row.get("rules", []):
            try:
                rule_id = int(law.get("rule_id"))
            except (TypeError, ValueError):
                continue
            if rule_id not in rule_ids:
                rule_ids.append(rule_id)
    return rule_ids


def _compact_case_type(case_type: str) -> str:
    raw = repair_text(case_type)
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in {"case analysis", "analysis", "case"}:
        return ""
    mappings = [
        (["contract", "合同", "lease", "租赁", "sale"], "合同"),
        (["employment", "labour", "labor", "劳动", "工资", "工伤"], "劳动"),
        (["tort", "侵权", "negligence", "injury", "损害"], "侵权"),
        (["criminal", "crime", "刑事", "诈骗", "盗窃"], "刑事"),
        (["privacy", "data", "personal information", "meta", "google", "instagram", "youtube", "flo", "隐私"], "隐私"),
        (["intellectual", "ip", "patent", "copyright", "trademark", "知产", "midjourney", "warner", "superman", "scooby"], "知产"),
        (["administrative", "行政", "许可", "处罚"], "行政"),
        (["bribery", "kickback", "corruption", "medical device", "hospital", "doctor", "贿赂", "商业贿赂"], "贿赂"),
        (["family", "婚姻", "继承", "遗产", "监护"], "家事"),
        (["company", "corporate", "股权", "公司", "董事"], "公司"),
        (["sanction", "ofac", "制裁"], "制裁"),
        (["tax", "税"], "税务"),
    ]
    for keywords, label in mappings:
        if any(keyword in lowered for keyword in keywords):
            return label
    compact = raw.replace("纠纷", "").replace("案件", "").replace("争议", "").strip()
    return compact[:6]


def _prediction_short_label(text: str) -> str:
    lowered = repair_text(text).lower()
    if not lowered:
        return ""
    if any(token in lowered for token in ["支持", "胜诉", "favour", "favor", "uphold"]):
        return "支持"
    if any(token in lowered for token in ["驳回", "dismiss", "deny", "败诉"]):
        return "驳回"
    if any(token in lowered for token in ["补证", "evidence", "proof"]):
        return "待补证"
    if any(token in lowered for token in ["risk", "风险", "liable", "uncertain"]):
        return "风险"
    return repair_text(text)[:6]


def _guess_risk_level(*parts: str) -> str:
    text_value = " ".join(repair_text(part).lower() for part in parts if repair_text(part))
    if not text_value:
        return ""
    high_terms = ["高风险", "high risk", "败诉", "重大", "严重", "处罚", "制裁", "不足", "缺失", "不利"]
    medium_terms = ["中风险", "medium risk", "待补", "不明确", "不明", "存疑", "有限", "波动", "争议"]
    low_terms = ["低风险", "low risk", "支持", "有利", "可行", "稳定"]
    if any(term in text_value for term in high_terms):
        return "高风险"
    if any(term in text_value for term in medium_terms):
        return "中风险"
    if any(term in text_value for term in low_terms):
        return "低风险"
    return "中风险"


def _build_rule_entries(module_packet: dict, default_country: str) -> list[dict]:
    rules = []
    for law in (module_packet or {}).get("relevant_laws", [])[:8]:
        rules.append(
            {
                "rule_id": law.get("rule_id"),
                "title": repair_text(law.get("title")),
                "article_no": repair_text(law.get("article_no") or law.get("citation")),
                "summary": repair_text(law.get("article_summary") or law.get("reason")),
                "country": repair_text(law.get("country") or default_country),
                "legal_type": repair_text(law.get("legal_type") or law.get("rule_level") or law.get("level")),
                "source_url": repair_text(law.get("source_url")),
                "detail_url": repair_text(law.get("detail_url")),
            }
        )
    return rules


def _build_risk_point_entries(analysis: dict, prediction: dict) -> list[dict]:
    points = _safe_list(
        (analysis or {}).get("risk_flags") or [],
        limit=6,
    )
    points = _safe_list(points + _safe_list((prediction or {}).get("risk_points") or [], limit=6), limit=6)
    if not points:
        fallback = repair_text((prediction or {}).get("predicted_outcome") or (prediction or {}).get("reasoning"))
        if fallback:
            points = [fallback[:48]]
    result = []
    for item in points[:6]:
        level = _guess_risk_level(item)
        result.append(
            {
                "name": item[:24],
                "level": level or "中风险",
                "description": item,
            }
        )
    return result


def _build_history_node_label(case_type: str, risk_points: list[dict], prediction_text: str, case_title: str) -> str:
    case_type_label = _compact_case_type(case_type)
    if not case_type_label:
        inferred_type = _derive_case_type_label(case_type, prediction_text, case_title)
        inferred_map = [
            ("contract", "鍚堝悓"),
            ("labour", "鍔冲姩"),
            ("tort", "渚垫潈"),
            ("criminal", "鍒戜簨"),
            ("intellectual", "鐭ヤ骇"),
            ("administrative", "琛屾斂"),
            ("consumer", "娑堣垂"),
            ("trust", "淇℃墭"),
            ("corporate", "鍏徃"),
            ("sanction", "鍒惰"),
        ]
        lowered_inferred = inferred_type.lower()
        for token, label in inferred_map:
            if token in lowered_inferred:
                case_type_label = label
                break
    if case_type_label:
        return case_type_label[:6]
    if risk_points:
        risk_label = repair_text((risk_points[0] or {}).get("level"))
        if risk_label:
            return risk_label[:6]
    prediction_label = _prediction_short_label(prediction_text)
    if prediction_label:
        return prediction_label[:6]
    return repair_text(case_title)[:6] or "案件"


def _build_history_node_label(case_type: str, risk_points: list[dict], prediction_text: str, case_title: str) -> str:
    case_type_label = _compact_case_type(case_type)
    if not case_type_label:
        inferred_type = _derive_case_type_label(case_type, prediction_text, case_title)
        inferred_map = [
            ("contract", "合同"),
            ("labour", "劳动"),
            ("tort", "侵权"),
            ("criminal", "刑事"),
            ("intellectual", "知产"),
            ("administrative", "行政"),
            ("consumer", "消费"),
            ("trust", "信托"),
            ("corporate", "公司"),
            ("sanction", "制裁"),
        ]
        lowered_inferred = inferred_type.lower()
        for token, label in inferred_map:
            if token in lowered_inferred:
                case_type_label = label
                break
    if case_type_label:
        return case_type_label[:6]
    if risk_points:
        risk_label = repair_text((risk_points[0] or {}).get("level"))
        if risk_label:
            return risk_label[:6]
    prediction_label = _prediction_short_label(prediction_text)
    if prediction_label:
        return prediction_label[:6]
    return repair_text(case_title)[:6] or "案件"


def _build_legal_relation_entries(
    *,
    case_type: str,
    legal_topics: list[str],
    rules: list[dict],
    module_code: str,
    requested_relief: str,
) -> list[str]:
    relations: list[str] = []
    compact_case_type = _compact_case_type(case_type)
    if compact_case_type:
        relations.append(f"{compact_case_type}涓昏娉曞緥鍏崇郴")
    for topic in _safe_list(legal_topics, limit=4):
        relations.append(topic)
    for rule in rules[:3]:
        title = repair_text(rule.get("title"))
        if title:
            relations.append(f"涓庛€?{title}銆?鐩稿叧")
    if repair_text(requested_relief):
        relations.append(f"璇夋眰鏂瑰悜锛?{repair_text(requested_relief)[:30]}")
    if normalize_case_text(module_code) == "us_sanctions":
        relations.append("鍚堣瀹℃煡涓庡埗瑁侀闄╄瘎浼?")
    return _safe_list(relations, limit=5)


def _build_evidence_focus_entries(
    *,
    case_type: str,
    query_text: str,
    disputed_issues: list[str],
    risk_points: list[dict],
) -> list[str]:
    focus: list[str] = []
    risk_text = " ".join(repair_text(item.get("description")) for item in risk_points if isinstance(item, dict))
    query_and_issue_text = " ".join([repair_text(query_text), *[repair_text(item) for item in disputed_issues]])
    lowered = normalize_case_text(case_type + " " + query_and_issue_text + " " + risk_text)

    if any(token in lowered for token in ["contract", "鍚堝悓", "lease", "rent", "sale"]):
        focus.extend(["鍚堝悓姝ｅ紡鏂囨湰", "浠樻涓庡眾琛岃褰?", "娌熼€氬線鏉ユ潗鏂?"])
    if any(token in lowered for token in ["employment", "labour", "labor", "鍔冲姩", "宸ヨ祫", "dismiss"]):
        focus.extend(["鍔冲姩鍚堝悓涓庤亴浣嶈鏄?", "鑰冨嫟涓庤柂閰褰?", "瑙ｉ櫎鎴栧鍒嗛€氱煡"])
    if any(token in lowered for token in ["tort", "渚垫潈", "injury", "damage", "negligence"]):
        focus.extend(["鐜板満鐓х墖涓庤棰?", "鍖荤枟/鎹熷け鍑瘉", "鍥犳灉鍏崇郴璇佹嵁"])
    if any(token in lowered for token in ["intellectual", "ip", "patent", "copyright", "trademark", "鐭ヤ骇"]):
        focus.extend(["鏉冨睘璇佷功", "渚垫潈姣斿鏉愭枡", "鎺堟潈/鍚堜綔鏂囨。"])
    if any(token in lowered for token in ["administrative", "琛屾斂", "penalty", "permit", "license"]):
        focus.extend(["琛屾斂澶勭綒鍐冲畾", "绋嬪簭閫氱煡鏂囦功", "鐢宠鲸鎴栧惉璇佹潗鏂?"])
    if any(token in lowered for token in ["criminal", "鍒戜簨", "fraud", "theft"]):
        focus.extend(["璧勯噾娴佸拰杞处璁板綍", "鑱婂ぉ/閭欢璁板綍", "涓昏鏁呮剰鐩稿叧璇佹嵁"])
    if any(token in lowered for token in ["sanction", "ofac", "鍒惰", "export"]):
        focus.extend(["瀵逛氦鏂逛俊鎭笌灞忓箷璁板綍", "鎶ュ叧/杩愯緭鏂囦欢", "鍚堣瀹℃煡璁板綍"])

    for item in risk_points[:3]:
        description = repair_text(item.get("description"))
        if any(token in normalize_case_text(description) for token in ["璇佹嵁", "evidence", "proof", "record", "chain"]):
            focus.append(description[:36])
    if not focus:
        focus = ["妗堟儏鏍稿績浜嬪疄鐨勫師濮嬭瘉鎹?", "鍚勬柟娌熼€氫笌灞ヨ璁板綍"]
    return _safe_list(focus, limit=5)


def _build_suggested_actions(
    *,
    case_type: str,
    risk_level: str,
    risk_points: list[dict],
    rules: list[dict],
) -> list[str]:
    actions: list[str] = []
    if risk_level == "楂橀闄?":
        actions.append("浼樺厛琛ュ己璇佹嵁閾惧苟閲嶆柊鏍稿鏃堕棿绾?")
    elif risk_level == "涓闄?":
        actions.append("鍥寸粫浜夎鐒︾偣琛ュ厖鍏抽敭浜嬪疄涓庝氦鏄撹褰?")
    else:
        actions.append("淇濈暀宸叉湁璇佹嵁缁撴瀯锛屽噯澶囬拡瀵规€ц璇佹潗鏂?")
    if rules:
        actions.append(f"鍥寸粫銆?{repair_text(rules[0].get('title'))}銆?鍑嗗瀵瑰簲璁洪噺涓庤瘉鎹?")
    compact_case_type = _compact_case_type(case_type)
    if compact_case_type == "鍔冲姩":
        actions.append("鏁寸悊鍔冲姩鍏崇郴銆佽€冨嫟鍜岃柂閰潗鏂?")
    elif compact_case_type == "鍚堝悓":
        actions.append("鏍稿鍚堝悓鏉℃銆佸彉鏇村崗璁拰灞ヨ鍑瘉")
    elif compact_case_type == "渚垫潈":
        actions.append("鍔犲己鍥犳灉鍏崇郴涓庢崯澶辨暟棰濈殑璇佹嵁")
    return _safe_list(actions, limit=4)

def _is_case_type_match(case_type: str, *keywords: str) -> bool:
    lowered = repair_text(case_type).lower()
    return bool(lowered) and any(keyword in lowered for keyword in keywords)


def _readable_risk_level(risk_level: str) -> str:
    lowered = repair_text(risk_level).lower()
    if any(token in lowered for token in ["high", "高"]):
        return "高风险"
    if any(token in lowered for token in ["low", "低"]):
        return "低风险"
    if any(token in lowered for token in ["medium", "mid", "中"]):
        return "中风险"
    return "中风险"


def _build_legal_relation_entries(
    *,
    case_type: str,
    legal_topics: list[str],
    rules: list[dict],
    module_code: str,
    requested_relief: str,
) -> list[str]:
    relations: list[str] = []
    compact_case_type = _compact_case_type(case_type)
    if compact_case_type:
        relations.append(f"{compact_case_type}案件的核心法律关系")

    for rule in rules[:3]:
        title = repair_text(rule.get("title"))
        if title:
            relations.append(f"与《{title}》直接相关")

    if normalize_case_text(module_code) == "us_sanctions":
        relations.append("优先核对 OFAC 规则、许可路径和除名程序")

    return _safe_list(relations, limit=5)


def _build_evidence_focus_entries(
    *,
    case_type: str,
    query_text: str,
    disputed_issues: list[str],
    risk_points: list[dict],
) -> list[str]:
    focus: list[str] = []
    lowered = normalize_case_text(" ".join([case_type, query_text, *disputed_issues]))

    if _is_case_type_match(case_type, "contract", "lease", "rent", "sale") or any(
        token in lowered for token in ["contract", "lease", "rent", "sale", "agreement"]
    ):
        focus.extend(
            [
                "合同正文、补充协议与履行记录",
                "付款、催告、违约通知与沟通记录",
                "围绕争议事实形成的原始证据链",
            ]
        )

    if _is_case_type_match(case_type, "employment", "labour", "labor") or any(
        token in lowered for token in ["employment", "labour", "labor", "dismiss", "wage"]
    ):
        focus.extend(
            [
                "劳动关系、工资、考勤与岗位调整记录",
                "解除、处罚、申诉和内部沟通材料",
                "能够证明损失和程序瑕疵的原始文件",
            ]
        )

    if _is_case_type_match(case_type, "tort", "injury", "negligence") or any(
        token in lowered for token in ["tort", "injury", "damage", "negligence", "slip"]
    ):
        focus.extend(
            [
                "事故发生经过、现场情况与时间线",
                "损害后果、维修费用或医疗材料",
                "因果关系与过错程度相关证据",
            ]
        )

    if _is_case_type_match(case_type, "intellectual", "patent", "copyright", "trademark") or any(
        token in lowered for token in ["intellectual", "patent", "copyright", "trademark", "license"]
    ):
        focus.extend(
            [
                "权属证明、授权链条和登记文件",
                "侵权比对材料与传播范围证据",
                "许可、收费和损害计算依据",
            ]
        )

    if _is_case_type_match(case_type, "administrative", "permit", "license") or any(
        token in lowered for token in ["administrative", "permit", "license", "penalty", "municipal"]
    ):
        focus.extend(
            [
                "行政决定、送达材料与程序记录",
                "许可、备案或审批的原始档案",
                "听证、申辩与复议环节材料",
            ]
        )

    if _is_case_type_match(case_type, "criminal", "fraud", "theft") or any(
        token in lowered for token in ["criminal", "fraud", "theft"]
    ):
        focus.extend(
            [
                "资金流向、交易记录和身份识别材料",
                "沟通记录、报案材料和证人证言",
                "能够证明主观状态与损失结果的证据",
            ]
        )

    if _is_case_type_match(case_type, "sanction", "ofac", "export") or any(
        token in lowered for token in ["sanction", "ofac", "export", "screening"]
    ):
        focus.extend(
            [
                "筛查记录、控制关系和受益所有权文件",
                "资金流、交易路径与第三方沟通记录",
                "许可、申诉、整改和合规审查材料",
            ]
        )

    for item in risk_points[:3]:
        description = repair_text(item.get("description"))
        if description and any(token in normalize_case_text(description) for token in ["evidence", "proof", "record", "chain", "证据", "记录"]):
            focus.append(description[:36])

    if not focus:
        focus = [
            "案情核心事实的原始证据",
            "各方沟通与履行记录",
            "能够直接支撑请求事项的关键材料",
        ]

    return _safe_list(focus, limit=5)


def _build_suggested_actions(
    *,
    case_type: str,
    risk_level: str,
    risk_points: list[dict],
    rules: list[dict],
) -> list[str]:
    actions: list[str] = []
    readable_risk_level = _readable_risk_level(risk_level)

    if readable_risk_level == "高风险":
        actions.append("优先补强关键证据，再重新评估主张强度")
    elif readable_risk_level == "中风险":
        actions.append("围绕争议焦点补充书面证据和时间线")
    else:
        actions.append("保留现有论证结构，继续补充针对性佐证")

    if rules:
        title = repair_text(rules[0].get("title"))
        if title:
            actions.append(f"围绕《{title}》准备对应论证与证据")

    if _is_case_type_match(case_type, "employment", "labour", "labor"):
        actions.append("整理劳动关系、工资、考勤和解除经过材料")
    elif _is_case_type_match(case_type, "contract", "lease", "rent", "sale"):
        actions.append("核对合同条款、补充协议和履约凭证")
    elif _is_case_type_match(case_type, "tort", "injury", "negligence"):
        actions.append("补强因果关系和损失金额证据")
    elif _is_case_type_match(case_type, "sanction", "ofac", "export"):
        actions.append("同步准备合规整改、许可申请或复审材料")

    return _safe_list(actions, limit=4)


def build_history_graph(
    *,
    case_title: str,
    key_facts: list[str],
    disputed_issues: list[str],
    rules: list[dict],
    conclusion: str,
) -> dict:
    nodes = [{"id": "case", "label": case_title or "案件", "type": "case"}]
    edges = []

    for index, fact in enumerate(_safe_list(key_facts, limit=4), start=1):
        node_id = f"fact_{index}"
        nodes.append({"id": node_id, "label": fact, "type": "fact"})
        edges.append({"source": "case", "target": node_id, "label": "包含"})

    for index, issue in enumerate(_safe_list(disputed_issues, limit=4), start=1):
        node_id = f"issue_{index}"
        nodes.append({"id": node_id, "label": issue, "type": "issue"})
        if key_facts:
            source_id = f"fact_{((index - 1) % max(1, min(len(key_facts), 4))) + 1}"
        else:
            source_id = "case"
        edges.append({"source": source_id, "target": node_id, "label": "关联"})

    for index, rule in enumerate(rules[:6], start=1):
        node_id = f"rule_{index}"
        nodes.append(
            {
                "id": node_id,
                "label": repair_text(rule.get("title")) or f"法律法规 {index}",
                "type": "rule",
            }
        )
        if disputed_issues:
            source_id = f"issue_{((index - 1) % max(1, min(len(disputed_issues), 4))) + 1}"
        elif key_facts:
            source_id = f"fact_{((index - 1) % max(1, min(len(key_facts), 4))) + 1}"
        else:
            source_id = "case"
        edges.append({"source": source_id, "target": node_id, "label": "适用"})

    nodes.append({"id": "conclusion", "label": conclusion or "预测结论", "type": "conclusion"})
    for node in nodes:
        if node["type"] == "rule":
            edges.append({"source": node["id"], "target": "conclusion", "label": "支持"})
    if not any(node["type"] == "rule" for node in nodes):
        if disputed_issues:
            edges.append({"source": "issue_1", "target": "conclusion", "label": "推导"})
        elif key_facts:
            edges.append({"source": "fact_1", "target": "conclusion", "label": "推导"})
        else:
            edges.append({"source": "case", "target": "conclusion", "label": "推导"})
    return {"nodes": nodes, "edges": edges}


def build_case_history_payload(
    *,
    query_text: str,
    analysis_payload: dict,
    prediction_payload: dict,
    module_packet: dict | None,
    module_code: str,
) -> dict:
    analysis = analysis_payload or {}
    prediction = prediction_payload or {}
    analysis_block = analysis.get("analysis", {}) or {}
    intake_outline = analysis.get("intake_outline") or {}
    module_packet = module_packet or {}

    facts = _safe_list(_split_case_sentences(intake_outline.get("facts") or analysis_block.get("facts") or ""), limit=5)
    issues = _safe_list(intake_outline.get("disputed_issues") or analysis_block.get("disputed_issues") or [], limit=5)
    rule_ids = _extract_rule_ids(module_packet)
    legal_topics = _safe_list(analysis_block.get("legal_topics") or [], limit=3)
    case_type = _derive_case_type_label(
        query_text,
        intake_outline.get("facts") or analysis_block.get("facts") or "",
        " ".join(issues),
        legal_topics=legal_topics,
    )
    country = "United States" if repair_text(module_code).lower() == "us_sanctions" else "Canada"
    rules = _build_rule_entries(module_packet, country)
    risk_points = _build_risk_point_entries(analysis_block, prediction)

    case_title = _derive_case_title(query_text, analysis_block.get("summary") or prediction.get("predicted_outcome") or "")
    case_summary = repair_text(analysis_block.get("summary") or prediction.get("reasoning") or intake_outline.get("facts") or "")
    summary = repair_text(prediction.get("reasoning") or analysis_block.get("summary") or "")
    conclusion = repair_text(prediction.get("predicted_outcome") or "")
    prediction_explanation = repair_text(prediction.get("reasoning") or prediction.get("predicted_outcome") or "")
    prediction_confidence = float(prediction.get("confidence") or 0)
    has_prediction = bool(conclusion or prediction_explanation or prediction_confidence > 0)
    risk_level = _guess_risk_level(
        " ".join((item.get("level") or "") + " " + (item.get("description") or "") for item in risk_points),
        conclusion,
        prediction_explanation,
    )
    node_label = _build_history_node_label(case_type, risk_points, conclusion, case_title)
    court_level = repair_text(analysis_block.get("jurisdiction") or "")
    requested_relief = repair_text(intake_outline.get("requested_relief") or analysis_block.get("requested_relief") or "")
    legal_relations = _build_legal_relation_entries(
        case_type=case_type,
        legal_topics=legal_topics,
        rules=rules,
        module_code=module_code,
        requested_relief=requested_relief,
    )
    evidence_focus = _build_evidence_focus_entries(
        case_type=case_type,
        query_text=query_text,
        disputed_issues=issues,
        risk_points=risk_points,
    )
    suggested_actions = _build_suggested_actions(
        case_type=case_type,
        risk_level=risk_level,
        risk_points=risk_points,
        rules=rules,
    )
    prediction_label = repair_text(prediction.get("likely_prevailing_party") or prediction.get("status") or "")
    if not prediction_label:
        prediction_label = _prediction_short_label(conclusion)

    return {
        "case_hash": build_case_hash(query_text, case_type=case_type, country=country, court_level=court_level),
        "case_title": case_title,
        "case_summary": case_summary,
        "query_text": repair_text(query_text),
        "query_type": "analysis",
        "country": country,
        "court_level": court_level,
        "legal_type": "OFAC" if country == "United States" else "Foreign Case Law",
        "case_type": case_type,
        "key_facts_json": facts,
        "dispute_focus_json": issues,
        "legal_rule_ids_json": rule_ids,
        "legal_rules_json": rules,
        "risk_points_json": risk_points,
        "model_analysis_summary": summary,
        "prediction_label": prediction_label,
        "prediction_conclusion": conclusion,
        "prediction_explanation": prediction_explanation,
        "prediction_confidence": prediction_confidence,
        "confidence": prediction_confidence,
        "graph_json": {},
        "result_snapshot": {
            "module_code": repair_text(module_code),
            "case_title": case_title,
            "case_summary": case_summary,
            "node_label": node_label,
            "risk_level": risk_level,
            "analysis": {
                "summary": repair_text(analysis_block.get("summary") or ""),
                "facts": repair_text(intake_outline.get("facts") or ""),
                "disputed_issues": issues,
                "requested_relief": requested_relief,
                "keywords": _safe_list(intake_outline.get("keywords") or [], limit=8),
                "legal_topics": legal_topics,
                "legal_relations": legal_relations,
                "evidence_focus": evidence_focus,
            },
            "prediction": {
                "label": prediction_label,
                "conclusion": conclusion,
                "confidence": prediction_confidence,
                "explanation": prediction_explanation,
                "risk_points": risk_points,
                "risk_level": risk_level,
                "suggested_actions": suggested_actions,
            },
            "rules": rules,
            "linked_laws": prediction.get("linked_laws") or [],
            "supporting_case_groups": prediction.get("supporting_case_groups") or [],
            "risk_points": risk_points,
            "legal_relations": legal_relations,
            "evidence_focus": evidence_focus,
            "suggested_actions": suggested_actions,
            "has_prediction": has_prediction,
        },
    }


def cleanup_invalid_histories() -> int:
    conditions = " OR ".join([f"LOWER(COALESCE(query_text, '')) LIKE :pattern_{index}" for index, _ in enumerate(_DEV_HISTORY_PATTERNS)])
    params = {f"pattern_{index}": f"%{pattern.lower()}%" for index, pattern in enumerate(_DEV_HISTORY_PATTERNS)}
    with engine.begin() as conn:
        result = conn.execute(
            text(
                f"""
                UPDATE search_histories
                SET status = 'invalid',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status <> 'invalid'
                  AND ({conditions})
                """
            ),
            params,
        )
    return int(result.rowcount or 0)


def _coerce_datetime(value: Any) -> Any:
    if value is None or value == "":
        return None
    if hasattr(value, "year") and hasattr(value, "month"):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _history_payload_score(row: dict) -> int:
    snapshot = row.get("result_snapshot") or {}
    score = 0
    if repair_text(row.get("prediction_conclusion")) or repair_text((snapshot.get("prediction") or {}).get("conclusion")):
        score += 6
    if repair_text(row.get("prediction_explanation")) or repair_text((snapshot.get("prediction") or {}).get("explanation")):
        score += 5
    if repair_text(row.get("model_analysis_summary")) or repair_text((snapshot.get("analysis") or {}).get("summary")):
        score += 4
    if row.get("legal_rules_json") or snapshot.get("rules"):
        score += 4
    if row.get("risk_points_json") or snapshot.get("risk_points"):
        score += 4
    if row.get("key_facts_json") or repair_text((snapshot.get("analysis") or {}).get("facts")):
        score += 3
    if row.get("dispute_focus_json") or (snapshot.get("analysis") or {}).get("disputed_issues"):
        score += 3
    if repair_text(row.get("case_summary")):
        score += 2
    if repair_text(row.get("case_type")):
        score += 1
    return score


def repair_incomplete_histories() -> int:
    repaired = 0
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT *
                FROM search_histories
                WHERE status = 'active'
                """
            )
        ).mappings().all()
        for raw_row in rows:
            row = dict(raw_row)
            snapshot = row.get("result_snapshot") or {}
            analysis_snapshot = snapshot.get("analysis") or {}
            prediction_snapshot = snapshot.get("prediction") or {}

            case_summary = (
                repair_text(row.get("case_summary"))
                or repair_text(snapshot.get("case_summary"))
                or repair_text(analysis_snapshot.get("summary"))
                or repair_text(row.get("model_analysis_summary"))
                or repair_text(row.get("query_text"))[:220]
            )
            current_title = repair_text(row.get("case_title"))
            standardized_title = current_title
            if (
                not standardized_title
                or standardized_title.startswith("从当前案情看")
                or standardized_title.startswith("从当前材料看")
                or standardized_title.startswith("A full prediction")
            ):
                standardized_title = _derive_case_title(repair_text(row.get("query_text")), case_summary)
            standardized_case_type = _derive_case_type_label(
                repair_text(row.get("case_type")),
                repair_text(row.get("query_text")),
                case_summary,
                legal_topics=_safe_list(analysis_snapshot.get("legal_topics") or [], limit=4),
            )
            rules = row.get("legal_rules_json") or snapshot.get("rules") or []
            risk_points = row.get("risk_points_json") or snapshot.get("risk_points") or prediction_snapshot.get("risk_points") or []
            key_facts = row.get("key_facts_json") or _split_case_sentences(analysis_snapshot.get("facts") or "")
            dispute_focus = row.get("dispute_focus_json") or analysis_snapshot.get("disputed_issues") or []
            prediction_label = (
                repair_text(row.get("prediction_label"))
                or repair_text(prediction_snapshot.get("label"))
                or _prediction_short_label(repair_text(row.get("prediction_conclusion")))
            )
            prediction_conclusion = (
                repair_text(row.get("prediction_conclusion"))
                or repair_text(prediction_snapshot.get("conclusion"))
                or repair_text(row.get("model_analysis_summary"))
            )
            prediction_explanation = (
                repair_text(row.get("prediction_explanation"))
                or repair_text(prediction_snapshot.get("explanation"))
                or repair_text(row.get("model_analysis_summary"))
                or prediction_conclusion
            )
            prediction_confidence = row.get("prediction_confidence") or row.get("confidence") or prediction_snapshot.get("confidence") or 0
            try:
                prediction_confidence = float(prediction_confidence or 0)
            except (TypeError, ValueError):
                prediction_confidence = 0.0

            needs_update = any(
                [
                    not repair_text(row.get("case_summary")) and case_summary,
                    not (row.get("legal_rules_json") or []) and rules,
                    not (row.get("risk_points_json") or []) and risk_points,
                    not (row.get("key_facts_json") or []) and key_facts,
                    not (row.get("dispute_focus_json") or []) and dispute_focus,
                    standardized_title != current_title,
                    standardized_case_type != repair_text(row.get("case_type")),
                    not repair_text(row.get("prediction_label")) and prediction_label,
                    not repair_text(row.get("prediction_conclusion")) and prediction_conclusion,
                    not repair_text(row.get("prediction_explanation")) and prediction_explanation,
                    float(row.get("prediction_confidence") or 0) == 0 and prediction_confidence > 0,
                ]
            )
            if not needs_update:
                continue

            conn.execute(
                text(
                    """
                    UPDATE search_histories
                    SET case_title = :case_title,
                        case_summary = :case_summary,
                        case_type = :case_type,
                        key_facts_json = CAST(:key_facts_json AS jsonb),
                        dispute_focus_json = CAST(:dispute_focus_json AS jsonb),
                        legal_rules_json = CAST(:legal_rules_json AS jsonb),
                        risk_points_json = CAST(:risk_points_json AS jsonb),
                        model_analysis_summary = COALESCE(NULLIF(model_analysis_summary, ''), :analysis_summary),
                        prediction_label = COALESCE(NULLIF(prediction_label, ''), :prediction_label),
                        prediction_conclusion = COALESCE(NULLIF(prediction_conclusion, ''), :prediction_conclusion),
                        prediction_explanation = COALESCE(NULLIF(prediction_explanation, ''), :prediction_explanation),
                        prediction_confidence = CASE
                            WHEN COALESCE(prediction_confidence, 0) > 0 THEN prediction_confidence
                            ELSE :prediction_confidence
                        END,
                        confidence = CASE
                            WHEN COALESCE(confidence, 0) > 0 THEN confidence
                            ELSE :prediction_confidence
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :history_id
                    """
                ),
                {
                    "history_id": int(row["id"]),
                    "case_title": standardized_title,
                    "case_summary": case_summary,
                    "case_type": standardized_case_type,
                    "key_facts_json": _json_text(key_facts),
                    "dispute_focus_json": _json_text(dispute_focus),
                    "legal_rules_json": _json_text(rules),
                    "risk_points_json": _json_text(risk_points),
                    "analysis_summary": repair_text(row.get("model_analysis_summary")) or repair_text(analysis_snapshot.get("summary")),
                    "prediction_label": prediction_label,
                    "prediction_conclusion": prediction_conclusion,
                    "prediction_explanation": prediction_explanation,
                    "prediction_confidence": prediction_confidence,
                },
            )
            repaired += 1
    return repaired


def deduplicate_histories() -> int:
    merged_groups = 0
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT *
                FROM search_histories
                WHERE status = 'active'
                  AND COALESCE(case_hash, '') <> ''
                ORDER BY user_id, case_hash, created_at ASC, id ASC
                """
            )
        ).mappings().all()

        grouped: dict[tuple[int, str], list[dict]] = {}
        for raw_row in rows:
            row = dict(raw_row)
            key = (int(row.get("user_id") or 0), repair_text(row.get("case_hash")))
            grouped.setdefault(key, []).append(row)

        for (_, _), entries in grouped.items():
            if len(entries) <= 1:
                continue
            entries.sort(
                key=lambda item: (
                    _history_payload_score(item),
                    _coerce_datetime(item.get("last_viewed_at")) or _coerce_datetime(item.get("updated_at")) or _coerce_datetime(item.get("created_at")) or datetime.min,
                    int(item.get("id") or 0),
                ),
                reverse=True,
            )
            keeper = entries[0]
            duplicates = entries[1:]
            total_view_count = sum(int(item.get("view_count") or 0) for item in entries)
            created_candidates = [_coerce_datetime(item.get("created_at")) for item in entries if _coerce_datetime(item.get("created_at"))]
            viewed_candidates = [
                _coerce_datetime(item.get("last_viewed_at")) or _coerce_datetime(item.get("updated_at")) or _coerce_datetime(item.get("created_at"))
                for item in entries
                if _coerce_datetime(item.get("last_viewed_at")) or _coerce_datetime(item.get("updated_at")) or _coerce_datetime(item.get("created_at"))
            ]
            earliest_created = min(created_candidates) if created_candidates else None
            latest_viewed = max(viewed_candidates) if viewed_candidates else None

            conn.execute(
                text(
                    """
                    UPDATE search_histories
                    SET view_count = :view_count,
                        created_at = COALESCE(:created_at, created_at),
                        last_viewed_at = COALESCE(:last_viewed_at, last_viewed_at),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :history_id
                    """
                ),
                {
                    "history_id": int(keeper["id"]),
                    "view_count": total_view_count,
                    "created_at": earliest_created,
                    "last_viewed_at": latest_viewed,
                },
            )
            for duplicate in duplicates:
                conn.execute(
                    text(
                        """
                        UPDATE search_histories
                        SET status = 'duplicate',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :duplicate_id
                        """
                    ),
                    {"duplicate_id": int(duplicate["id"])},
                )
            merged_groups += 1
    return merged_groups


def _demo_rules(rule_specs: list[tuple[str, str, str, str]]) -> list[dict]:
    rules = []
    for title, article_no, summary, source_url in rule_specs:
        rules.append(
            {
                "title": title,
                "article_no": article_no,
                "summary": summary,
                "country": "Canada",
                "legal_type": "Statute",
                "source_url": source_url,
                "detail_url": "",
            }
        )
    return rules


def _demo_history_records() -> list[dict]:
    return [
        {
            "case_title": "Commercial Lease Deposit Dispute",
            "query_text": "The tenant withheld the final two months of rent after alleging that the landlord failed to repair heating and ventilation problems in a Toronto warehouse lease.",
            "case_summary": "A lease dispute over withheld rent, repair obligations, and an offset claim tied to business interruption losses.",
            "case_type": "Contract Dispute",
            "country": "Canada",
            "court_level": "Ontario Superior Court of Justice",
            "key_facts": ["A written lease allocated maintenance duties between landlord and tenant.", "The tenant stopped paying rent after repeated complaints about heating failures.", "The landlord claims the tenant never followed the contractual notice process."],
            "dispute_focus": ["Whether the repair covenant was materially breached.", "Whether the tenant could lawfully set off rent against alleged losses."],
            "legal_relations": ["Lease performance and breach", "Set-off and mitigation", "Commercial landlord-tenant obligations"],
            "risk_points": [{"name": "Notice gap", "level": "high risk", "description": "Repair complaints may not satisfy the formal notice clause."}, {"name": "Loss proof", "level": "medium risk", "description": "Business interruption loss evidence is incomplete."}],
            "evidence_focus": ["Executed lease and repair schedules", "Email complaints and service tickets", "Invoices showing business interruption loss"],
            "prediction_label": "Risk",
            "prediction_conclusion": "There is a meaningful risk that the tenant cannot justify the full rent withholding.",
            "prediction_explanation": "The strongest issue is whether the tenant complied with the contractual notice mechanism before stopping payment.",
            "prediction_confidence": 0.84,
            "suggested_actions": ["Organize the repair notice timeline.", "Separate repair cost claims from rent set-off arguments.", "Prepare evidence of actual operational loss."],
            "rules": _demo_rules([
                ("Commercial Tenancies Act", "s. 20", "Commercial rent recovery and tenant remedies remain primarily contract driven.", "https://www.ontario.ca/laws/statute/90l07"),
                ("Courts of Justice Act", "s. 96", "Superior court jurisdiction over commercial civil disputes.", "https://www.ontario.ca/laws/statute/90c43"),
            ]),
            "created_at": "2026-04-18 10:25:00",
            "last_viewed_at": "2026-05-03 11:42:00",
            "view_count": 4,
        },
        {
            "case_title": "Supply Agreement Price Adjustment Conflict",
            "query_text": "A manufacturer raised prices under a supply agreement citing raw material volatility, while the distributor insists the price adjustment clause was never triggered.",
            "case_summary": "A contract pricing dispute centering on the scope of a variation clause and the sufficiency of supporting market data.",
            "case_type": "Contract Dispute",
            "country": "Canada",
            "court_level": "Alberta Court of King's Bench",
            "key_facts": ["The supply agreement included a narrow raw-material escalation clause.", "The manufacturer issued a unilateral price notice with little documentary support.", "The distributor continued to order goods under protest."],
            "dispute_focus": ["How strictly the variation clause should be interpreted.", "Whether continued performance amounts to acceptance."],
            "legal_relations": ["Contract interpretation", "Course of dealing", "Commercial good faith"],
            "risk_points": [{"name": "Clause ambiguity", "level": "medium risk", "description": "The escalation clause is too open-ended and may be construed against the drafter."}],
            "evidence_focus": ["Signed supply agreement", "Historical price notices", "Commodity market reports"],
            "prediction_label": "Uncertain",
            "prediction_conclusion": "The manufacturer has only a moderate chance of enforcing the unilateral price increase.",
            "prediction_explanation": "Success depends on proving that the agreed trigger conditions actually occurred and were transparently documented.",
            "prediction_confidence": 0.73,
            "suggested_actions": ["Pin down the exact trigger language.", "Prepare market evidence tied to the notice date.", "Address acquiescence arguments from continued orders."],
            "rules": _demo_rules([
                ("Sale of Goods Act", "s. 12", "Contract terms and commercial performance remain central to goods disputes.", "https://www.canlii.org/en/ab/laws/stat/rsa-2000-c-s-2/latest/rsa-2000-c-s-2.html"),
                ("Fair Trading Act", "s. 6", "Misleading conduct risks arise if the adjustment notice overstated contractual rights.", "https://www.canlii.org/en/ab/laws/stat/rsa-2000-c-f-2/latest/rsa-2000-c-f-2.html"),
            ]),
            "created_at": "2026-04-19 09:10:00",
            "last_viewed_at": "2026-05-02 16:20:00",
            "view_count": 3,
        },
        {
            "case_title": "Overtime Classification Complaint",
            "query_text": "A project coordinator says she was misclassified as management and denied overtime for two years despite following fixed schedules and limited supervisory duties.",
            "case_summary": "An employment dispute over overtime entitlement, job classification, and payroll reconstruction.",
            "case_type": "Labour Dispute",
            "country": "Canada",
            "court_level": "Ontario Labour Relations Board",
            "key_facts": ["The employee had a fixed weekly schedule.", "She approved no hiring or firing decisions.", "Payroll records do not cleanly reflect off-hours work."],
            "dispute_focus": ["Whether the managerial exemption applies.", "Whether overtime hours can be reliably reconstructed."],
            "legal_relations": ["Employment standards compliance", "Overtime entitlement", "Payroll record obligations"],
            "risk_points": [{"name": "Time records", "level": "high risk", "description": "Internal timekeeping was inconsistent."}, {"name": "Role ambiguity", "level": "medium risk", "description": "The employer may frame the role as partly supervisory."}],
            "evidence_focus": ["Job description revisions", "Slack or email timestamps", "Payroll exports and schedule rosters"],
            "prediction_label": "Employee advantage",
            "prediction_conclusion": "The employee appears to have a strong basis for at least part of the overtime claim.",
            "prediction_explanation": "The exemption defense looks weak if the employer cannot show genuine managerial authority.",
            "prediction_confidence": 0.86,
            "suggested_actions": ["Map duties against the statutory exemption.", "Rebuild the overtime timeline from communication logs.", "Quantify hours conservatively."],
            "rules": _demo_rules([
                ("Employment Standards Act, 2000", "s. 22", "Overtime pay depends on hours worked and any applicable exemption.", "https://www.ontario.ca/laws/statute/00e41"),
                ("Employment Standards Act, 2000", "s. 15", "Employers must maintain employment records.", "https://www.ontario.ca/laws/statute/00e41"),
            ]),
            "created_at": "2026-04-20 13:40:00",
            "last_viewed_at": "2026-05-03 09:15:00",
            "view_count": 6,
        },
        {
            "case_title": "Termination for Cause Review",
            "query_text": "A software company dismissed a sales manager for cause after expense irregularities and private customer side deals, but the evidence chain is incomplete.",
            "case_summary": "A labour dispute over just cause, internal investigation fairness, and documentary proof of dishonesty.",
            "case_type": "Labour Dispute",
            "country": "Canada",
            "court_level": "British Columbia Supreme Court",
            "key_facts": ["The employer found questionable expense submissions.", "There are allegations of side deals outside approved channels.", "The employee says the company approved the expense practice informally."],
            "dispute_focus": ["Whether the misconduct meets the just-cause threshold.", "Whether the investigation record is reliable enough for summary judgment."],
            "legal_relations": ["Just cause dismissal", "Duty of honesty", "Procedural fairness in workplace investigations"],
            "risk_points": [{"name": "Proof gap", "level": "high risk", "description": "Key approval conversations were oral and poorly documented."}],
            "evidence_focus": ["Expense policies", "Approval emails or chat logs", "Customer account notes and payment trails"],
            "prediction_label": "Employer risk",
            "prediction_conclusion": "The employer faces a serious risk if it cannot prove intentional dishonesty with a clean evidence chain.",
            "prediction_explanation": "Cause cases demand proportionality and reliable documentation; the present record looks uneven.",
            "prediction_confidence": 0.81,
            "suggested_actions": ["Separate policy breach from fraud allegations.", "Corroborate oral approvals with surrounding records.", "Prepare an alternative without-cause damages position."],
            "rules": _demo_rules([
                ("Employment Standards Act", "s. 63", "Termination obligations remain relevant if just cause fails.", "https://www.bclaws.gov.bc.ca/civix/document/id/complete/statreg/00_96113_01"),
                ("Evidence Act", "s. 42", "Document handling and admissibility remain important for internal investigation records.", "https://www.canlii.org/en/bc/laws/stat/rsbc-1996-c-124/latest/rsbc-1996-c-124.html"),
            ]),
            "created_at": "2026-04-21 15:05:00",
            "last_viewed_at": "2026-05-03 08:50:00",
            "view_count": 5,
        },
        {
            "case_title": "Slip and Fall in Retail Plaza",
            "query_text": "A customer slipped on untreated ice in a parking area shared by several tenants and the property manager denies responsibility for the exact zone.",
            "case_summary": "A tort claim about occupiers' liability, winter maintenance, and causal proof for personal injury damages.",
            "case_type": "Tort Liability",
            "country": "Canada",
            "court_level": "Ontario Superior Court of Justice",
            "key_facts": ["The fall occurred in a common parking area.", "Snow removal was subcontracted.", "Maintenance logs have timing gaps."],
            "dispute_focus": ["Who controlled the exact hazard location.", "Whether the maintenance system was reasonable."],
            "legal_relations": ["Occupiers' liability", "Delegated maintenance duties", "Causation and damages"],
            "risk_points": [{"name": "Control issue", "level": "medium risk", "description": "Responsibility for the precise location is contested."}, {"name": "Maintenance records", "level": "medium risk", "description": "The winter log is incomplete."}],
            "evidence_focus": ["Site photos and weather history", "Snow removal contract", "Maintenance log timestamps"],
            "prediction_label": "Mixed",
            "prediction_conclusion": "Liability is plausible, but the control and notice issues will materially affect exposure.",
            "prediction_explanation": "The plaintiff benefits from common-area control arguments, while the defense will stress reasonable maintenance systems.",
            "prediction_confidence": 0.76,
            "suggested_actions": ["Pin down who controlled the area.", "Tie weather records to maintenance response times.", "Separate liability from damages evidence."],
            "rules": _demo_rules([
                ("Occupiers' Liability Act", "s. 3", "Occupiers owe a duty to take reasonable care for visitor safety.", "https://www.ontario.ca/laws/statute/90o02"),
                ("Negligence Act", "s. 1", "Apportionment may matter if contributory negligence is raised.", "https://www.ontario.ca/laws/statute/90n01"),
            ]),
            "created_at": "2026-04-22 11:00:00",
            "last_viewed_at": "2026-05-01 19:10:00",
            "view_count": 3,
        },
        {
            "case_title": "Construction Water Damage Claim",
            "query_text": "A condo owner alleges that a renovation contractor caused hidden water damage, but expert reports disagree on whether the leak predated the work.",
            "case_summary": "A tort and property damage dispute driven by expert conflict and causation uncertainty.",
            "case_type": "Tort Liability",
            "country": "Canada",
            "court_level": "Provincial Court of British Columbia",
            "key_facts": ["A renovation preceded the leak discovery.", "Two experts disagree on leak origin timing.", "The owner delayed invasive inspection for several weeks."],
            "dispute_focus": ["Whether the contractor caused the loss.", "Whether delay in investigation weakens causation."],
            "legal_relations": ["Negligence and causation", "Expert evidence reliability", "Property damage quantification"],
            "risk_points": [{"name": "Expert conflict", "level": "medium risk", "description": "Competing reports create an unstable causation record."}],
            "evidence_focus": ["Competing expert reports", "Pre-renovation unit condition records", "Repair invoices and moisture readings"],
            "prediction_label": "Uncertain",
            "prediction_conclusion": "The claim remains viable but causation is presently too contested for a confident merits forecast.",
            "prediction_explanation": "The outcome may turn on which expert methodology the court finds more reliable.",
            "prediction_confidence": 0.68,
            "suggested_actions": ["Stress-test both expert assumptions.", "Preserve building records from before the renovation.", "Clarify the damages timeline."],
            "rules": _demo_rules([
                ("Negligence Act", "s. 4", "Liability turns on breach, causation, and loss.", "https://www.canlii.org/en/bc/laws/stat/rsbc-1996-c-333/latest/rsbc-1996-c-333.html"),
                ("Evidence Act", "s. 12", "Expert evidence handling must be disciplined and well documented.", "https://www.canlii.org/en/bc/laws/stat/rsbc-1996-c-124/latest/rsbc-1996-c-124.html"),
            ]),
            "created_at": "2026-04-23 14:15:00",
            "last_viewed_at": "2026-04-30 17:45:00",
            "view_count": 2,
        },
        {
            "case_title": "Software Licensing Copyright Conflict",
            "query_text": "A former reseller continued using archived source packages after its software distribution license expired and claims there was an implied renewal.",
            "case_summary": "An intellectual property dispute about license scope, post-termination use, and evidence of commercial acquiescence.",
            "case_type": "Intellectual Property",
            "country": "Canada",
            "court_level": "Federal Court",
            "key_facts": ["The distribution agreement expired without a signed extension.", "The reseller continued limited servicing work for legacy clients.", "The copyright owner accepted some post-expiry royalty payments."],
            "dispute_focus": ["Whether conduct created an implied renewal or estoppel.", "Whether continued use exceeded any residual servicing rights."],
            "legal_relations": ["Copyright license scope", "Post-termination conduct", "Estoppel and acquiescence"],
            "risk_points": [{"name": "Scope creep", "level": "high risk", "description": "Archived package use may exceed any arguable servicing carve-out."}],
            "evidence_focus": ["License agreement and expiry notices", "Royalty payment records", "Deployment logs for post-expiry use"],
            "prediction_label": "Owner advantage",
            "prediction_conclusion": "The copyright owner currently appears to have the stronger position unless the reseller can prove a narrow implied extension.",
            "prediction_explanation": "Post-expiry conduct may soften remedies, but it does not obviously authorize broader continued distribution.",
            "prediction_confidence": 0.83,
            "suggested_actions": ["Separate servicing use from new distribution.", "Map post-expiry royalties to any alleged extension.", "Preserve deployment logs."],
            "rules": _demo_rules([
                ("Copyright Act", "s. 27", "Unauthorized reproduction or distribution can infringe copyright.", "https://laws-lois.justice.gc.ca/eng/acts/C-42/"),
                ("Federal Courts Act", "s. 20", "The Federal Court can hear copyright matters.", "https://laws-lois.justice.gc.ca/eng/acts/F-7/"),
            ]),
            "created_at": "2026-04-24 10:35:00",
            "last_viewed_at": "2026-05-03 07:30:00",
            "view_count": 4,
        },
        {
            "case_title": "Municipal Licensing Penalty Review",
            "query_text": "A restaurant challenges a municipal licensing suspension, arguing the inspection process was rushed and key hearing materials were disclosed late.",
            "case_summary": "An administrative law matter focused on procedural fairness, disclosure timing, and proportionality of a licensing suspension.",
            "case_type": "Administrative Review",
            "country": "Canada",
            "court_level": "Ontario Divisional Court",
            "key_facts": ["The suspension followed multiple inspection notices.", "Disclosure of witness statements was delayed.", "The restaurant says the sanction was disproportionate to the violations."],
            "dispute_focus": ["Whether late disclosure caused procedural unfairness.", "Whether the penalty was proportionate."],
            "legal_relations": ["Procedural fairness", "Municipal licensing powers", "Reasonableness review"],
            "risk_points": [{"name": "Procedure", "level": "medium risk", "description": "The fairness argument depends on proving actual prejudice from late disclosure."}],
            "evidence_focus": ["Inspection timeline", "Disclosure package and hearing notices", "Prior compliance history"],
            "prediction_label": "Partial relief",
            "prediction_conclusion": "There is a realistic chance of obtaining procedural relief, but not necessarily full restoration of the licence.",
            "prediction_explanation": "The most persuasive angle is disclosure fairness rather than attacking every inspection finding on the merits.",
            "prediction_confidence": 0.74,
            "suggested_actions": ["Document the disclosure timeline.", "Show how late materials impaired preparation.", "Offer a narrower remedy theory."],
            "rules": _demo_rules([
                ("Statutory Powers Procedure Act", "s. 5.4", "Parties are entitled to basic procedural fairness in tribunal-style hearings.", "https://www.ontario.ca/laws/statute/90s22"),
                ("Municipal Act, 2001", "s. 151", "Municipal licensing authority must still be exercised fairly and lawfully.", "https://www.ontario.ca/laws/statute/01m25"),
            ]),
            "created_at": "2026-04-25 16:05:00",
            "last_viewed_at": "2026-05-02 13:12:00",
            "view_count": 3,
        },
        {
            "case_title": "Crypto Fraud Exposure Review",
            "query_text": "An employee routed client funds into a private crypto wallet during a high-yield investment program and now argues there was no intent to permanently deprive the investors.",
            "case_summary": "A criminal risk assessment concerning fraudulent intent, fund tracing, and representations made to investors.",
            "case_type": "Criminal Risk",
            "country": "Canada",
            "court_level": "Superior Court of Quebec",
            "key_facts": ["Investor money was diverted from the stated custody account.", "Marketing materials promised secure, segregated handling.", "The employee later returned part of the funds after complaints."],
            "dispute_focus": ["Whether the diversion shows fraudulent intent.", "Whether partial repayment meaningfully changes culpability."],
            "legal_relations": ["Fraud and dishonest deprivation", "Tracing of funds", "Representations to investors"],
            "risk_points": [{"name": "Tracing record", "level": "high risk", "description": "Blockchain and bank trail evidence may strongly support intent."}],
            "evidence_focus": ["Wallet movement records", "Investor communications", "Custody account promises and onboarding documents"],
            "prediction_label": "High exposure",
            "prediction_conclusion": "The fact pattern presents a high risk of adverse criminal findings if the fund trail confirms intentional diversion.",
            "prediction_explanation": "Partial repayment does not neutralize the original deprivation theory if misrepresentation and diversion are established.",
            "prediction_confidence": 0.88,
            "suggested_actions": ["Reconstruct the money trail in detail.", "Separate reckless conduct from deliberate deception arguments.", "Preserve every investor-facing statement."],
            "rules": _demo_rules([
                ("Criminal Code", "s. 380", "Fraud requires dishonest conduct and deprivation or risk of deprivation.", "https://laws-lois.justice.gc.ca/eng/acts/C-46/"),
                ("Canada Evidence Act", "s. 31.1", "Electronic records must be organized carefully for admissibility and weight.", "https://laws-lois.justice.gc.ca/eng/acts/C-5/"),
            ]),
            "created_at": "2026-04-26 12:22:00",
            "last_viewed_at": "2026-05-03 10:58:00",
            "view_count": 7,
        },
        {
            "case_title": "Defective Appliance Consumer Claim",
            "query_text": "A consumer bought a premium smart refrigerator that repeatedly failed within six months, and the retailer insists repairs are the only remedy despite months of delays.",
            "case_summary": "A consumer rights dispute over defective goods, repeated repair delay, and the availability of replacement or rescission remedies.",
            "case_type": "Consumer Protection",
            "country": "Canada",
            "court_level": "Ontario Small Claims Court",
            "key_facts": ["The appliance failed several times in the warranty period.", "Repair appointments were repeatedly cancelled.", "The consumer seeks rescission or replacement rather than another repair cycle."],
            "dispute_focus": ["Whether repeated failed repairs amount to a substantial breach.", "Whether the retailer can insist on repair-only remedies."],
            "legal_relations": ["Consumer sale of goods", "Implied conditions of quality", "Fair remedy expectations"],
            "risk_points": [{"name": "Damage proof", "level": "low risk", "description": "The main challenge is quantifying consequential food loss and inconvenience."}],
            "evidence_focus": ["Purchase invoice and warranty", "Repair logs and technician reports", "Photos or videos of repeat failures"],
            "prediction_label": "Consumer advantage",
            "prediction_conclusion": "The consumer appears well positioned to argue for a stronger remedy than another delayed repair attempt.",
            "prediction_explanation": "Repeated failure and repair delay strengthen the argument that the product did not meet reasonable quality expectations.",
            "prediction_confidence": 0.79,
            "suggested_actions": ["Organize the failed repair chronology.", "Separate direct replacement relief from incidental loss claims.", "Preserve technician reports and defect media."],
            "rules": _demo_rules([
                ("Consumer Protection Act, 2002", "s. 9", "Consumer transactions must not unfairly deprive purchasers of meaningful remedies.", "https://www.ontario.ca/laws/statute/02c30"),
                ("Sale of Goods Act", "s. 15", "Implied conditions of merchantable quality remain relevant to defective goods.", "https://www.ontario.ca/laws/statute/90s01"),
            ]),
            "created_at": "2026-04-27 09:48:00",
            "last_viewed_at": "2026-05-01 14:05:00",
            "view_count": 2,
        },
    ]


def seed_demo_histories() -> int:
    if not getattr(settings, "demo_history_seed_enabled", False):
        return 0
    if getattr(settings, "app_env", "development") == "production":
        return 0

    seed_count = 0
    with engine.begin() as conn:
        user_row = conn.execute(
            text(
                """
                SELECT id
                FROM users
                WHERE LOWER(username) = 'lg'
                LIMIT 1
                """
            )
        ).mappings().first()
        if not user_row:
            return 0
        user_id = int(user_row["id"])

        for item in _demo_history_records():
            case_hash = build_case_hash(
                item["query_text"],
                case_type=item["case_type"],
                country=item["country"],
                court_level=item["court_level"],
            )
            payload = {
                "case_hash": case_hash,
                "case_title": item["case_title"],
                "case_summary": item["case_summary"],
                "query_text": item["query_text"],
                "query_type": "analysis",
                "country": item["country"],
                "court_level": item["court_level"],
                "legal_type": "Foreign Case Law",
                "case_type": item["case_type"],
                "key_facts_json": item["key_facts"],
                "dispute_focus_json": item["dispute_focus"],
                "legal_rule_ids_json": [],
                "legal_rules_json": item["rules"],
                "risk_points_json": item["risk_points"],
                "model_analysis_summary": item["case_summary"],
                "prediction_label": item["prediction_label"],
                "prediction_conclusion": item["prediction_conclusion"],
                "prediction_explanation": item["prediction_explanation"],
                "prediction_confidence": item["prediction_confidence"],
                "confidence": item["prediction_confidence"],
                "graph_json": {},
                "result_snapshot": {
                    "case_title": item["case_title"],
                    "case_summary": item["case_summary"],
                    "node_label": _build_history_node_label(item["case_type"], item["risk_points"], item["prediction_conclusion"], item["case_title"]),
                    "risk_level": _guess_risk_level(item["prediction_conclusion"], item["prediction_explanation"], " ".join(point["description"] for point in item["risk_points"])),
                    "analysis": {
                        "summary": item["case_summary"],
                        "facts": " ".join(item["key_facts"]),
                        "disputed_issues": item["dispute_focus"],
                        "requested_relief": "",
                        "keywords": [],
                        "legal_topics": [item["case_type"]],
                        "legal_relations": item["legal_relations"],
                        "evidence_focus": item["evidence_focus"],
                    },
                    "prediction": {
                        "label": item["prediction_label"],
                        "conclusion": item["prediction_conclusion"],
                        "confidence": item["prediction_confidence"],
                        "explanation": item["prediction_explanation"],
                        "risk_level": _guess_risk_level(item["prediction_conclusion"], item["prediction_explanation"]),
                        "risk_points": item["risk_points"],
                        "suggested_actions": item["suggested_actions"],
                    },
                    "rules": item["rules"],
                    "risk_points": item["risk_points"],
                    "legal_relations": item["legal_relations"],
                    "evidence_focus": item["evidence_focus"],
                    "suggested_actions": item["suggested_actions"],
                },
            }
            existing = conn.execute(
                text(
                    """
                    SELECT id, view_count, created_at, last_viewed_at
                    FROM search_histories
                    WHERE user_id = :user_id
                      AND case_hash = :case_hash
                      AND status = 'active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id, "case_hash": case_hash},
            ).mappings().first()
            if existing:
                conn.execute(
                    text(
                        """
                        UPDATE search_histories
                        SET case_title = :case_title,
                            case_summary = :case_summary,
                            query_text = :query_text,
                            query_type = :query_type,
                            country = :country,
                            court_level = :court_level,
                            legal_type = :legal_type,
                            case_type = :case_type,
                            key_facts_json = CAST(:key_facts_json AS jsonb),
                            dispute_focus_json = CAST(:dispute_focus_json AS jsonb),
                            legal_rule_ids_json = CAST(:legal_rule_ids_json AS jsonb),
                            legal_rules_json = CAST(:legal_rules_json AS jsonb),
                            risk_points_json = CAST(:risk_points_json AS jsonb),
                            model_analysis_summary = :model_analysis_summary,
                            prediction_label = :prediction_label,
                            prediction_conclusion = :prediction_conclusion,
                            prediction_explanation = :prediction_explanation,
                            prediction_confidence = :prediction_confidence,
                            confidence = :confidence,
                            graph_json = CAST(:graph_json AS jsonb),
                            result_snapshot = CAST(:result_snapshot AS jsonb),
                            view_count = GREATEST(COALESCE(view_count, 1), :view_count),
                            created_at = LEAST(created_at, CAST(:created_at AS timestamp)),
                            last_viewed_at = GREATEST(COALESCE(last_viewed_at, CAST(:last_viewed_at AS timestamp)), CAST(:last_viewed_at AS timestamp)),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :history_id
                        """
                    ),
                    {
                        **{key: value for key, value in payload.items() if key not in {"result_snapshot", "key_facts_json", "dispute_focus_json", "legal_rule_ids_json", "legal_rules_json", "risk_points_json", "graph_json"}},
                        "history_id": int(existing["id"]),
                        "key_facts_json": _json_text(payload["key_facts_json"]),
                        "dispute_focus_json": _json_text(payload["dispute_focus_json"]),
                        "legal_rule_ids_json": _json_text(payload["legal_rule_ids_json"]),
                        "legal_rules_json": _json_text(payload["legal_rules_json"]),
                        "risk_points_json": _json_text(payload["risk_points_json"]),
                        "graph_json": _json_text(payload["graph_json"]),
                        "result_snapshot": _json_text(payload["result_snapshot"]),
                        "view_count": int(item["view_count"]),
                        "created_at": item["created_at"],
                        "last_viewed_at": item["last_viewed_at"],
                    },
                )
            else:
                conn.execute(
                    text(
                        """
                        INSERT INTO search_histories (
                            user_id,
                            query_text,
                            query_type,
                            country,
                            court_level,
                            legal_type,
                            result_snapshot,
                            case_hash,
                            case_title,
                            case_summary,
                            case_type,
                            key_facts_json,
                            dispute_focus_json,
                            legal_rule_ids_json,
                            legal_rules_json,
                            risk_points_json,
                            model_analysis_summary,
                            prediction_label,
                            prediction_conclusion,
                            prediction_explanation,
                            prediction_confidence,
                            confidence,
                            graph_json,
                            view_count,
                            created_at,
                            updated_at,
                            last_viewed_at,
                            status
                        )
                        VALUES (
                            :user_id,
                            :query_text,
                            :query_type,
                            :country,
                            :court_level,
                            :legal_type,
                            CAST(:result_snapshot AS jsonb),
                            :case_hash,
                            :case_title,
                            :case_summary,
                            :case_type,
                            CAST(:key_facts_json AS jsonb),
                            CAST(:dispute_focus_json AS jsonb),
                            CAST(:legal_rule_ids_json AS jsonb),
                            CAST(:legal_rules_json AS jsonb),
                            CAST(:risk_points_json AS jsonb),
                            :model_analysis_summary,
                            :prediction_label,
                            :prediction_conclusion,
                            :prediction_explanation,
                            :prediction_confidence,
                            :confidence,
                            CAST(:graph_json AS jsonb),
                            :view_count,
                            CAST(:created_at AS timestamp),
                            CURRENT_TIMESTAMP,
                            CAST(:last_viewed_at AS timestamp),
                            'active'
                        )
                        """
                    ),
                    {
                        "user_id": user_id,
                        **{key: value for key, value in payload.items() if key not in {"result_snapshot", "key_facts_json", "dispute_focus_json", "legal_rule_ids_json", "legal_rules_json", "risk_points_json", "graph_json"}},
                        "key_facts_json": _json_text(payload["key_facts_json"]),
                        "dispute_focus_json": _json_text(payload["dispute_focus_json"]),
                        "legal_rule_ids_json": _json_text(payload["legal_rule_ids_json"]),
                        "legal_rules_json": _json_text(payload["legal_rules_json"]),
                        "risk_points_json": _json_text(payload["risk_points_json"]),
                        "graph_json": _json_text(payload["graph_json"]),
                        "result_snapshot": _json_text(payload["result_snapshot"]),
                        "view_count": int(item["view_count"]),
                        "created_at": item["created_at"],
                        "last_viewed_at": item["last_viewed_at"],
                    },
                )
            seed_count += 1
    return seed_count


def upsert_case_history(*, user_id: int, payload: dict) -> int:
    case_hash = repair_text(payload.get("case_hash"))
    if not case_hash:
        return 0
    params = {
        "user_id": int(user_id),
        "query_text": repair_text(payload.get("query_text")),
        "query_type": repair_text(payload.get("query_type") or "analysis"),
        "country": repair_text(payload.get("country")),
        "court_level": repair_text(payload.get("court_level")),
        "legal_type": repair_text(payload.get("legal_type")),
        "result_snapshot": _json_text(payload.get("result_snapshot") or {}),
        "case_hash": case_hash,
        "case_title": repair_text(payload.get("case_title")),
        "case_summary": repair_text(payload.get("case_summary")),
        "case_type": repair_text(payload.get("case_type")),
        "key_facts_json": _json_text(payload.get("key_facts_json") or []),
        "dispute_focus_json": _json_text(payload.get("dispute_focus_json") or []),
        "legal_rule_ids_json": _json_text(payload.get("legal_rule_ids_json") or []),
        "legal_rules_json": _json_text(payload.get("legal_rules_json") or []),
        "risk_points_json": _json_text(payload.get("risk_points_json") or []),
        "model_analysis_summary": repair_text(payload.get("model_analysis_summary")),
        "prediction_label": repair_text(payload.get("prediction_label")),
        "prediction_conclusion": repair_text(payload.get("prediction_conclusion")),
        "prediction_explanation": repair_text(payload.get("prediction_explanation")),
        "prediction_confidence": float(payload.get("prediction_confidence") or payload.get("confidence") or 0),
        "confidence": float(payload.get("confidence") or 0),
        "graph_json": _json_text(payload.get("graph_json") or {}),
    }
    normalized_query = normalize_case_text(params["query_text"])
    with engine.begin() as conn:
        text_matches = []
        if normalized_query:
            candidate_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM search_histories
                    WHERE user_id = :user_id
                      AND status = 'active'
                      AND country = :country
                    ORDER BY updated_at DESC, id DESC
                    """
                ),
                {"user_id": int(user_id), "country": params["country"]},
            ).mappings().all()
            for row in candidate_rows:
                if normalize_case_text(row.get("query_text") or "") == normalized_query:
                    text_matches.append(dict(row))
        if text_matches:
            text_matches.sort(
                key=lambda item: (
                    _history_payload_score(item),
                    _coerce_datetime(item.get("last_viewed_at")) or _coerce_datetime(item.get("updated_at")) or _coerce_datetime(item.get("created_at")) or datetime.min,
                    int(item.get("id") or 0),
                ),
                reverse=True,
            )
            keeper = text_matches[0]
            duplicate_rows = text_matches[1:]
            total_view_count = sum(int(item.get("view_count") or 0) for item in text_matches) + 1
            created_candidates = [_coerce_datetime(item.get("created_at")) for item in text_matches if _coerce_datetime(item.get("created_at"))]
            viewed_candidates = [
                _coerce_datetime(item.get("last_viewed_at")) or _coerce_datetime(item.get("updated_at")) or _coerce_datetime(item.get("created_at"))
                for item in text_matches
                if _coerce_datetime(item.get("last_viewed_at")) or _coerce_datetime(item.get("updated_at")) or _coerce_datetime(item.get("created_at"))
            ]
            conn.execute(
                text(
                    """
                    UPDATE search_histories
                    SET case_hash = :case_hash,
                        case_title = :case_title,
                        case_summary = :case_summary,
                        query_text = :query_text,
                        query_type = :query_type,
                        country = :country,
                        court_level = :court_level,
                        legal_type = :legal_type,
                        case_type = :case_type,
                        key_facts_json = CAST(:key_facts_json AS jsonb),
                        dispute_focus_json = CAST(:dispute_focus_json AS jsonb),
                        legal_rule_ids_json = CAST(:legal_rule_ids_json AS jsonb),
                        legal_rules_json = CAST(:legal_rules_json AS jsonb),
                        risk_points_json = CAST(:risk_points_json AS jsonb),
                        model_analysis_summary = :model_analysis_summary,
                        prediction_label = :prediction_label,
                        prediction_conclusion = :prediction_conclusion,
                        prediction_explanation = :prediction_explanation,
                        prediction_confidence = :prediction_confidence,
                        confidence = :confidence,
                        graph_json = CAST(:graph_json AS jsonb),
                        result_snapshot = CAST(:result_snapshot AS jsonb),
                        view_count = :view_count,
                        created_at = COALESCE(:created_at, created_at),
                        updated_at = CURRENT_TIMESTAMP,
                        last_viewed_at = CURRENT_TIMESTAMP
                    WHERE id = :history_id
                    """
                ),
                {
                    **params,
                    "history_id": int(keeper["id"]),
                    "view_count": total_view_count,
                    "created_at": min(created_candidates) if created_candidates else None,
                },
            )
            for duplicate in duplicate_rows:
                conn.execute(
                    text(
                        """
                        UPDATE search_histories
                        SET status = 'duplicate',
                            updated_at = CURRENT_TIMESTAMP,
                            last_viewed_at = COALESCE(:last_viewed_at, last_viewed_at)
                        WHERE id = :history_id
                        """
                    ),
                    {
                        "history_id": int(duplicate["id"]),
                        "last_viewed_at": max(viewed_candidates) if viewed_candidates else None,
                    },
                )
            return int(keeper["id"])
        record = conn.execute(
            text(
                """
                INSERT INTO search_histories (
                    user_id,
                    query_text,
                    query_type,
                    country,
                    court_level,
                    legal_type,
                    result_snapshot,
                    case_hash,
                    case_title,
                    case_summary,
                    case_type,
                    key_facts_json,
                    dispute_focus_json,
                    legal_rule_ids_json,
                    legal_rules_json,
                    risk_points_json,
                    model_analysis_summary,
                    prediction_label,
                    prediction_conclusion,
                    prediction_explanation,
                    prediction_confidence,
                    confidence,
                    graph_json,
                    view_count,
                    created_at,
                    updated_at,
                    last_viewed_at,
                    status
                )
                VALUES (
                    :user_id,
                    :query_text,
                    :query_type,
                    :country,
                    :court_level,
                    :legal_type,
                    CAST(:result_snapshot AS jsonb),
                    :case_hash,
                    :case_title,
                    :case_summary,
                    :case_type,
                    CAST(:key_facts_json AS jsonb),
                    CAST(:dispute_focus_json AS jsonb),
                    CAST(:legal_rule_ids_json AS jsonb),
                    CAST(:legal_rules_json AS jsonb),
                    CAST(:risk_points_json AS jsonb),
                    :model_analysis_summary,
                    :prediction_label,
                    :prediction_conclusion,
                    :prediction_explanation,
                    :prediction_confidence,
                    :confidence,
                    CAST(:graph_json AS jsonb),
                    1,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    'active'
                )
                ON CONFLICT (user_id, case_hash)
                WHERE case_hash <> '' AND status = 'active'
                DO UPDATE SET
                    case_title = EXCLUDED.case_title,
                    case_summary = EXCLUDED.case_summary,
                    query_text = EXCLUDED.query_text,
                    query_type = EXCLUDED.query_type,
                    country = EXCLUDED.country,
                    court_level = EXCLUDED.court_level,
                    legal_type = EXCLUDED.legal_type,
                    case_type = EXCLUDED.case_type,
                    key_facts_json = EXCLUDED.key_facts_json,
                    dispute_focus_json = EXCLUDED.dispute_focus_json,
                    legal_rule_ids_json = EXCLUDED.legal_rule_ids_json,
                    legal_rules_json = EXCLUDED.legal_rules_json,
                    risk_points_json = EXCLUDED.risk_points_json,
                    model_analysis_summary = EXCLUDED.model_analysis_summary,
                    prediction_label = EXCLUDED.prediction_label,
                    prediction_conclusion = EXCLUDED.prediction_conclusion,
                    prediction_explanation = EXCLUDED.prediction_explanation,
                    prediction_confidence = EXCLUDED.prediction_confidence,
                    confidence = EXCLUDED.confidence,
                    graph_json = EXCLUDED.graph_json,
                    result_snapshot = EXCLUDED.result_snapshot,
                    view_count = CASE
                        WHEN COALESCE(search_histories.view_count, 0) < 1 THEN 1
                        ELSE COALESCE(search_histories.view_count, 0) + 1
                    END,
                    updated_at = CURRENT_TIMESTAMP,
                    last_viewed_at = CURRENT_TIMESTAMP
                RETURNING id
                """
            ),
            params,
        ).mappings().first()
    return int(record["id"]) if record else 0


def touch_history(history_id: int, *, user_id: int | None = None, admin: bool = False) -> bool:
    conditions = ["id = :history_id", "status = 'active'"]
    params: dict[str, Any] = {"history_id": int(history_id)}
    if not admin:
        conditions.append("user_id = :user_id")
        params["user_id"] = int(user_id or 0)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                f"""
                UPDATE search_histories
                SET last_viewed_at = CURRENT_TIMESTAMP,
                    view_count = COALESCE(view_count, 0) + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE {' AND '.join(conditions)}
                """
            ),
            params,
        )
    return result.rowcount > 0


def record_search_history(
    *,
    user_id: int,
    query_text: str,
    query_type: str,
    country: str = "",
    court_level: str = "",
    legal_type: str = "",
    result_snapshot: dict | None = None,
) -> int:
    title = _derive_case_title(query_text)
    return upsert_case_history(
        user_id=user_id,
        payload={
            "case_hash": build_case_hash(query_text, case_type=query_type, country=country, court_level=court_level),
            "case_title": title,
            "case_summary": repair_text((result_snapshot or {}).get("analysis", {}).get("summary") or query_text[:200]),
            "query_text": query_text,
            "query_type": query_type,
            "country": country,
            "court_level": court_level,
            "legal_type": legal_type,
            "case_type": query_type,
            "key_facts_json": _split_case_sentences(query_text)[:4],
            "dispute_focus_json": [],
            "legal_rule_ids_json": [],
            "legal_rules_json": [],
            "risk_points_json": [],
            "model_analysis_summary": repair_text((result_snapshot or {}).get("analysis", {}).get("summary") or ""),
            "prediction_label": "",
            "prediction_conclusion": "",
            "prediction_explanation": "",
            "prediction_confidence": 0,
            "confidence": 0,
            "graph_json": {},
            "result_snapshot": result_snapshot or {},
        },
    )


def _history_base_sql(extra_conditions: list[str]) -> str:
    conditions = ["sh.status = 'active'"] + extra_conditions
    return f"""
    SELECT
        sh.*,
        u.username
    FROM search_histories sh
    JOIN users u ON u.id = sh.user_id
    WHERE {' AND '.join(conditions)}
    """


def list_user_histories(
    *,
    user_id: int,
    query_type: str = "",
    case_type: str = "",
    country: str = "",
    court_level: str = "",
    legal_type: str = "",
    legal_rule: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 50,
) -> list[dict]:
    conditions = ["sh.user_id = :user_id"]
    params: dict[str, Any] = {"user_id": int(user_id), "limit": max(1, min(int(limit), 200))}
    if query_type:
        conditions.append("LOWER(sh.query_type) = LOWER(:query_type)")
        params["query_type"] = repair_text(query_type)
    if case_type:
        conditions.append("LOWER(sh.case_type) LIKE LOWER(:case_type)")
        params["case_type"] = f"%{repair_text(case_type)}%"
    if country:
        conditions.append("LOWER(sh.country) = LOWER(:country)")
        params["country"] = repair_text(country)
    if court_level:
        conditions.append("LOWER(sh.court_level) LIKE LOWER(:court_level)")
        params["court_level"] = f"%{repair_text(court_level)}%"
    if legal_type:
        conditions.append("LOWER(sh.legal_type) LIKE LOWER(:legal_type)")
        params["legal_type"] = f"%{repair_text(legal_type)}%"
    if legal_rule:
        conditions.append("CAST(sh.legal_rules_json AS TEXT) ILIKE :legal_rule")
        params["legal_rule"] = f"%{repair_text(legal_rule)}%"
    if start_date:
        conditions.append("sh.created_at >= :start_date")
        params["start_date"] = repair_text(start_date)
    if end_date:
        conditions.append("sh.created_at <= :end_date")
        params["end_date"] = repair_text(end_date)
    sql = _history_base_sql(conditions) + """
    ORDER BY COALESCE(sh.last_viewed_at, sh.updated_at, sh.created_at) DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def get_history(history_id: int, *, user_id: int | None = None, admin: bool = False, touch: bool = True) -> dict | None:
    conditions = ["sh.id = :history_id"]
    params: dict[str, Any] = {"history_id": int(history_id)}
    if not admin:
        conditions.append("sh.user_id = :user_id")
        params["user_id"] = int(user_id or 0)
    sql = _history_base_sql(conditions) + " LIMIT 1"
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).mappings().first()
    if not row:
        return None
    history = dict(row)
    if touch:
        touch_history(int(history_id), user_id=user_id, admin=admin)
        history["view_count"] = int(history.get("view_count") or 0) + 1
    return history


def _normalize_rule_entry(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = repair_text(item.get("title"))
    if not title:
        return None
    return {
        "id": item.get("rule_id") or item.get("id") or "",
        "title": title,
        "article_no": repair_text(item.get("article_no")),
        "summary": repair_text(item.get("summary") or item.get("article_summary")),
        "country": repair_text(item.get("country")),
        "legal_type": repair_text(item.get("legal_type")),
        "source_url": repair_text(item.get("source_url")),
        "detail_url": repair_text(item.get("detail_url")),
    }


def _normalize_risk_entry(item: Any) -> dict | None:
    def _is_meta_risk_text(text_value: str) -> bool:
        normalized = repair_text(text_value)
        if not normalized:
            return True
        noise_tokens = [
            "当前结果基于",
            "本地案例库",
            "规则化推理",
            "不替代正式法律意见",
            "国家/联邦层面的裁判",
            "省级/地方",
            "先区分应优先",
        ]
        return any(token in normalized for token in noise_tokens)

    if isinstance(item, dict):
        text_value = repair_text(item.get("description") or item.get("name"))
        if not text_value or _is_meta_risk_text(text_value):
            return None
        return {
            "name": repair_text(item.get("name")) or text_value[:24],
            "level": repair_text(item.get("level")) or _guess_risk_level(text_value) or "中风险",
            "description": text_value,
        }
    text_value = repair_text(item)
    if not text_value or _is_meta_risk_text(text_value):
        return None
    return {
        "name": text_value[:24],
        "level": _guess_risk_level(text_value) or "中风险",
        "description": text_value,
    }


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _history_case_scope(case_row: dict) -> str:
    scope = repair_text(case_row.get("scope"))
    if scope in {"national_federal", "provincial_local", "other"}:
        return scope
    rank = _to_int(case_row.get("court_rank"))
    if rank >= 3:
        return "national_federal"
    if rank >= 1:
        return "provincial_local"
    return "other"


def _history_scope_label(scope: str) -> str:
    normalized = repair_text(scope)
    if normalized == "national_federal":
        return "国家 / 联邦"
    if normalized == "provincial_local":
        return "地区 / 地方"
    return "其他"


def _normalize_support_case_entry(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = repair_text(item.get("title"))
    case_id = _to_int(item.get("case_id"))
    if not title and not case_id:
        return None
    court_rank = _to_int(item.get("court_rank"))
    scope = _history_case_scope({"scope": item.get("scope"), "court_rank": court_rank})
    return {
        "case_id": case_id,
        "title": title or f"案例 {case_id}",
        "court_level": repair_text(item.get("court_level")),
        "court_rank": court_rank,
        "case_type": repair_text(item.get("case_type")),
        "judgment_date": str(item.get("judgment_date") or "")[:10],
        "summary": repair_text(item.get("summary") or item.get("facts")),
        "judgment_result": repair_text(item.get("judgment_result")),
        "source_url": repair_text(item.get("source_url")),
        "scope": scope,
        "scope_label": _history_scope_label(scope),
        "match_score": _to_float(item.get("match_score")),
        "match_reason": repair_text(item.get("match_reason")),
        "linked_law_titles": _safe_list(item.get("linked_law_titles") or [], limit=4),
    }


def _normalize_support_group_entry(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = repair_text(item.get("title"))
    rule_id = _to_int(item.get("rule_id"))
    if not title and not rule_id:
        return None
    cases: list[dict] = []
    seen_cases: set[str] = set()
    for raw_case in item.get("cases") or []:
        normalized_case = _normalize_support_case_entry(raw_case)
        if not normalized_case:
            continue
        case_key = str(normalized_case.get("case_id") or normalized_case.get("title")).lower()
        if case_key in seen_cases:
            continue
        seen_cases.add(case_key)
        cases.append(normalized_case)
    cases = sorted(
        cases,
        key=lambda entry: (
            _to_float(entry.get("match_score")),
            _to_int(entry.get("court_rank")),
            entry.get("judgment_date") or "",
        ),
        reverse=True,
    )
    return {
        "rule_id": rule_id,
        "title": title or f"法规 {rule_id}",
        "article_no": repair_text(item.get("article_no")),
        "article_summary": repair_text(item.get("article_summary") or item.get("summary")),
        "country": repair_text(item.get("country")),
        "legal_type": repair_text(item.get("legal_type")),
        "detail_url": repair_text(item.get("detail_url")),
        "source_url": repair_text(item.get("source_url")),
        "linked_case_count": _to_int(item.get("linked_case_count")) or len(cases),
        "cases": cases,
    }


def _query_supporting_case_groups_from_rules(rules: list[dict], limit_laws: int = 4, cases_per_law: int = 3) -> list[dict]:
    groups: list[dict] = []
    for rule in rules[:limit_laws]:
        rule_id = _to_int(rule.get("rule_id"))
        if not rule_id:
            continue
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        lc.id AS case_id,
                        lc.title,
                        lc.court_level,
                        lc.court_rank,
                        lc.case_type,
                        lc.summary,
                        lc.judgment_result,
                        lc.judgment_date,
                        lc.source_url,
                        crr.match_score,
                        crr.match_reason
                    FROM case_rule_relations crr
                    JOIN legal_cases lc ON lc.id = crr.case_id
                    WHERE crr.rule_id = :rule_id
                    ORDER BY crr.match_score DESC, lc.court_rank DESC, lc.judgment_date DESC NULLS LAST, lc.updated_at DESC
                    LIMIT :limit_count
                    """
                ),
                {"rule_id": rule_id, "limit_count": max(1, cases_per_law)},
            ).mappings().all()
        cases = []
        for row in rows:
            normalized_case = _normalize_support_case_entry(dict(row))
            if not normalized_case:
                continue
            normalized_case["linked_law_titles"] = [repair_text(rule.get("title"))] if repair_text(rule.get("title")) else []
            cases.append(normalized_case)
        if not cases:
            continue
        groups.append(
            {
                "rule_id": rule_id,
                "title": repair_text(rule.get("title")),
                "article_no": repair_text(rule.get("article_no")),
                "article_summary": repair_text(rule.get("summary")),
                "country": repair_text(rule.get("country")),
                "legal_type": repair_text(rule.get("legal_type")),
                "detail_url": repair_text(rule.get("detail_url")),
                "source_url": repair_text(rule.get("source_url")),
                "linked_case_count": len(cases),
                "cases": cases,
            }
        )
    return groups


def _history_supporting_case_groups(history: dict) -> list[dict]:
    snapshot = history.get("result_snapshot") or {}
    groups: list[dict] = []
    for item in snapshot.get("supporting_case_groups") or []:
        normalized = _normalize_support_group_entry(item)
        if normalized:
            groups.append(normalized)
    if groups:
        return groups
    return _query_supporting_case_groups_from_rules(_history_rules(history))


def _history_supporting_case_rows(history: dict, groups: list[dict] | None = None) -> list[dict]:
    case_map: dict[str, dict] = {}
    for group in (groups or _history_supporting_case_groups(history)):
        law_ref = {
            "rule_id": _to_int(group.get("rule_id")),
            "title": repair_text(group.get("title")),
            "article_no": repair_text(group.get("article_no")),
            "article_summary": repair_text(group.get("article_summary")),
            "country": repair_text(group.get("country")),
            "legal_type": repair_text(group.get("legal_type")),
            "detail_url": repair_text(group.get("detail_url")),
            "source_url": repair_text(group.get("source_url")),
        }
        for case in group.get("cases") or []:
            case_key = str(case.get("case_id") or case.get("title")).lower()
            if case_key not in case_map:
                case_map[case_key] = {
                    "case_id": _to_int(case.get("case_id")),
                    "title": repair_text(case.get("title")),
                    "court_level": repair_text(case.get("court_level")),
                    "court_rank": _to_int(case.get("court_rank")),
                    "case_type": repair_text(case.get("case_type")),
                    "judgment_date": repair_text(case.get("judgment_date")),
                    "summary": repair_text(case.get("summary")),
                    "judgment_result": repair_text(case.get("judgment_result")),
                    "source_url": repair_text(case.get("source_url")),
                    "scope": repair_text(case.get("scope")) or _history_case_scope(case),
                    "match_score": _to_float(case.get("match_score")),
                    "match_reason": repair_text(case.get("match_reason")),
                    "rules": [],
                }
            existing_rule_ids = {
                str(_to_int(existing.get("rule_id")) or repair_text(existing.get("title")).lower())
                for existing in case_map[case_key]["rules"]
            }
            current_rule_key = str(_to_int(law_ref.get("rule_id")) or repair_text(law_ref.get("title")).lower())
            if current_rule_key not in existing_rule_ids:
                case_map[case_key]["rules"].append(law_ref)
    return sorted(
        case_map.values(),
        key=lambda entry: (
            1 if repair_text(entry.get("scope")) == "national_federal" else 0,
            _to_float(entry.get("match_score")),
            _to_int(entry.get("court_rank")),
            repair_text(entry.get("judgment_date")),
        ),
        reverse=True,
    )


def _history_has_prediction(history: dict) -> bool:
    snapshot = history.get("result_snapshot") or {}
    conclusion = repair_text(history.get("prediction_conclusion")) or repair_text((snapshot.get("prediction") or {}).get("conclusion"))
    explanation = repair_text(history.get("prediction_explanation")) or repair_text((snapshot.get("prediction") or {}).get("explanation"))
    return bool(conclusion or explanation or _history_prediction_confidence(history) > 0)


@lru_cache(maxsize=1)
def _active_rule_lookup() -> tuple[set[int], set[str]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, title FROM legal_rules")).mappings().all()
    return (
        {int(row.get("id") or 0) for row in rows if int(row.get("id") or 0)},
        {repair_text(row.get("title")).lower() for row in rows if repair_text(row.get("title"))},
    )


def _history_rule_is_active(rule: dict, valid_rule_ids: set[int], valid_rule_titles: set[str]) -> bool:
    rule_id = _to_int(rule.get("rule_id"))
    title = repair_text(rule.get("title")).lower()
    if rule_id and rule_id in valid_rule_ids:
        return True
    if title and title in valid_rule_titles:
        return True
    return False


def _history_rules(history: dict) -> list[dict]:
    valid_rule_ids, valid_rule_titles = _active_rule_lookup()
    rules = []
    for item in history.get("legal_rules_json") or []:
        normalized = _normalize_rule_entry(item)
        if normalized and _history_rule_is_active(normalized, valid_rule_ids, valid_rule_titles):
            rules.append(normalized)
    if rules:
        return rules
    snapshot = history.get("result_snapshot") or {}
    for item in snapshot.get("rules") or []:
        normalized = _normalize_rule_entry(item)
        if normalized and _history_rule_is_active(normalized, valid_rule_ids, valid_rule_titles):
            rules.append(normalized)
    return rules


def _history_risk_points(history: dict) -> list[dict]:
    points = []
    for item in history.get("risk_points_json") or []:
        normalized = _normalize_risk_entry(item)
        if normalized:
            points.append(normalized)
    if points:
        return points
    snapshot = history.get("result_snapshot") or {}
    source_items = snapshot.get("risk_points") or (snapshot.get("prediction") or {}).get("risk_points") or []
    for item in source_items:
        normalized = _normalize_risk_entry(item)
        if normalized:
            points.append(normalized)
    return points


def _history_key_facts(history: dict) -> list[str]:
    facts = _safe_list(history.get("key_facts_json") or [], limit=6)
    if facts:
        return facts
    snapshot = history.get("result_snapshot") or {}
    return _safe_list(_split_case_sentences((snapshot.get("analysis") or {}).get("facts") or ""), limit=6)


def _history_dispute_focus(history: dict) -> list[str]:
    focus = _safe_list(history.get("dispute_focus_json") or [], limit=6)
    if focus:
        return focus
    snapshot = history.get("result_snapshot") or {}
    return _safe_list((snapshot.get("analysis") or {}).get("disputed_issues") or [], limit=6)


def _history_case_summary(history: dict) -> str:
    snapshot = history.get("result_snapshot") or {}
    return (
        repair_text(history.get("case_summary"))
        or repair_text(snapshot.get("case_summary"))
        or repair_text((snapshot.get("analysis") or {}).get("summary"))
        or repair_text(history.get("model_analysis_summary"))
        or repair_text(history.get("query_text"))[:220]
    )


def _history_prediction_confidence(history: dict) -> float:
    try:
        return float(history.get("prediction_confidence") or history.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def _history_risk_level(history: dict) -> str:
    snapshot = history.get("result_snapshot") or {}
    snapshot_level = repair_text(snapshot.get("risk_level"))
    if snapshot_level:
        return _readable_risk_level(snapshot_level)
    points = _history_risk_points(history)
    if points:
        levels = [repair_text(item.get("level")) for item in points]
        if "高风险" in levels:
            return "高风险"
        if "中风险" in levels:
            return "中风险"
        if "低风险" in levels:
            return "低风险"
    return _guess_risk_level(
        repair_text(history.get("prediction_conclusion")),
        repair_text(history.get("prediction_explanation")),
        repair_text(history.get("model_analysis_summary")),
    ) or "未知风险"


def _history_node_label(history: dict) -> str:
    snapshot = history.get("result_snapshot") or {}
    snapshot_label = repair_text(snapshot.get("node_label"))
    if snapshot_label and not re.fullmatch(r"\d{2,4}[-/]\d{1,2}([-/]\d{1,2})?", snapshot_label) and not (
        re.fullmatch(r"[A-Za-z ]+", snapshot_label) and len(snapshot_label.strip()) > 4
    ):
        return snapshot_label[:6]
    display_case_type = _history_display_case_type(history)
    return _build_history_node_label(
        display_case_type or repair_text(history.get("case_type")),
        _history_risk_points(history),
        repair_text(history.get("prediction_conclusion")) or repair_text(history.get("prediction_explanation")),
        repair_text(history.get("case_title")),
    )


def _history_display_case_type(history: dict) -> str:
    direct = _compact_case_type(repair_text(history.get("case_type")))
    if direct and not (re.fullmatch(r"[A-Za-z0-9 ._/-]+", direct) and len(direct.strip()) > 3):
        return direct
    snapshot = history.get("result_snapshot") or {}
    probe_text = " ".join(
        [
            repair_text(history.get("case_title")),
            repair_text(history.get("query_text")),
            repair_text(history.get("case_summary")),
            repair_text(history.get("model_analysis_summary")),
            repair_text((snapshot.get("analysis") or {}).get("summary")),
            " ".join(rule.get("title", "") for rule in _history_rules(history)[:4]),
        ]
    )
    inferred = _compact_case_type(probe_text)
    if inferred:
        return inferred
    fallback = _compact_case_type(_derive_case_type_label(probe_text))
    return fallback or "综合"


def _history_display_country(history: dict) -> str:
    raw = repair_text(history.get("country"))
    lowered = raw.lower()
    if lowered in {"canada", "ca", "加拿大"}:
        return "加拿大"
    if lowered in {"united states", "u.s.", "u.s", "usa", "us", "美国"}:
        return "美国"
    return raw or "未标注国家"


def _history_display_court_level(history: dict, supporting_case_rows: list[dict] | None = None) -> str:
    raw = repair_text(history.get("court_level"))
    country = _history_display_country(history)
    lowered = raw.lower()
    if raw and lowered not in {"canada", "united states", "usa", "us", "u.s.", "u.s"} and raw != country:
        return raw
    scopes = {repair_text(item.get("scope")) for item in (supporting_case_rows or []) if repair_text(item.get("scope"))}
    if scopes == {"national_federal"}:
        return "国家 / 联邦"
    if scopes == {"provincial_local"}:
        return "地区 / 地方"
    if "national_federal" in scopes and "provincial_local" in scopes:
        return "多层级案例"
    return "未标注级别"


def _history_graph_risk_points(history_item: dict) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for risk in history_item.get("risk_points", []) or []:
        label = repair_text(risk.get("name") or risk.get("description"))
        if not label:
            continue
        normalized_label = label
        if any(token in label for token in ["时间线", "主体关系", "书面证据", "证据"]):
            normalized_label = "证据链不足"
        elif any(token in label for token in ["合同条款", "条款", "约定不明"]):
            normalized_label = "条款解释风险"
        elif any(token in label for token in ["损失", "损害金额", "赔偿金额"]):
            normalized_label = "损失证明不足"
        elif any(token in label for token in ["程序", "期限", "逾期"]):
            normalized_label = "程序风险"
        lowered = label.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(normalized_label[:12])
        if len(result) >= 4:
            break
    return result


def _history_prediction_brief(history: dict) -> str:
    return (
        repair_text(history.get("prediction_conclusion"))
        or repair_text((history.get("result_snapshot") or {}).get("prediction", {}).get("conclusion"))
        or repair_text(history.get("model_analysis_summary"))
    )


def _history_legal_relations(history: dict) -> list[str]:
    snapshot = history.get("result_snapshot") or {}
    relations = _safe_list(
        (snapshot.get("legal_relations") or (snapshot.get("analysis") or {}).get("legal_relations") or []),
        limit=6,
    )
    if relations:
        return relations
    rules = _history_rules(history)
    return _build_legal_relation_entries(
        case_type=repair_text(history.get("case_type")),
        legal_topics=_safe_list((snapshot.get("analysis") or {}).get("legal_topics") or [], limit=4),
        rules=rules,
        module_code=repair_text(snapshot.get("module_code")),
        requested_relief=repair_text((snapshot.get("analysis") or {}).get("requested_relief")),
    )


def _history_evidence_focus(history: dict) -> list[str]:
    snapshot = history.get("result_snapshot") or {}
    focus = _safe_list(
        (snapshot.get("evidence_focus") or (snapshot.get("analysis") or {}).get("evidence_focus") or []),
        limit=6,
    )
    if focus:
        return focus
    return _build_evidence_focus_entries(
        case_type=repair_text(history.get("case_type")),
        query_text=repair_text(history.get("query_text")),
        disputed_issues=_history_dispute_focus(history),
        risk_points=_history_risk_points(history),
    )


def _history_suggested_actions(history: dict) -> list[str]:
    snapshot = history.get("result_snapshot") or {}
    actions = _safe_list(
        (snapshot.get("suggested_actions") or (snapshot.get("prediction") or {}).get("suggested_actions") or []),
        limit=6,
    )
    if actions:
        return actions
    return _build_suggested_actions(
        case_type=repair_text(history.get("case_type")),
        risk_level=_history_risk_level(history),
        risk_points=_history_risk_points(history),
        rules=_history_rules(history),
    )


def build_history_display_payload(history: dict) -> dict:
    snapshot = history.get("result_snapshot") or {}
    rules = _history_rules(history)
    risk_points = _history_risk_points(history)
    risk_level = _history_risk_level(history)
    supporting_case_groups = _history_supporting_case_groups(history)
    supporting_case_rows = _history_supporting_case_rows(history, supporting_case_groups)
    display_case_type = _history_display_case_type(history)
    display_country = _history_display_country(history)
    display_court_level = _history_display_court_level(history, supporting_case_rows)
    has_prediction = _history_has_prediction(history)
    module_code = repair_text(snapshot.get("module_code")) or ("us_sanctions" if repair_text(history.get("country")) == "United States" else "canada")
    prediction_label = (
        repair_text(history.get("prediction_label"))
        or repair_text((snapshot.get("prediction") or {}).get("label"))
        or _prediction_short_label(repair_text(history.get("prediction_conclusion")))
    )
    prediction_conclusion = (
        repair_text(history.get("prediction_conclusion"))
        or repair_text((snapshot.get("prediction") or {}).get("conclusion"))
        or repair_text(history.get("model_analysis_summary"))
    )
    prediction_explanation = (
        repair_text(history.get("prediction_explanation"))
        or repair_text((snapshot.get("prediction") or {}).get("explanation"))
        or _history_prediction_brief(history)
    )
    return {
        "id": int(history.get("id") or 0),
        "case_title": repair_text(history.get("case_title")) or "未命名案件",
        "case_summary": _history_case_summary(history),
        "query_text": repair_text(history.get("query_text")),
        "query_type": repair_text(history.get("query_type") or "analysis"),
        "case_type": display_case_type,
        "country": display_country,
        "court_level": display_court_level,
        "key_facts": _history_key_facts(history),
        "dispute_focus": _history_dispute_focus(history),
        "legal_relations": _history_legal_relations(history),
        "evidence_focus": _history_evidence_focus(history),
        "legal_rules": rules,
        "supporting_case_groups": supporting_case_groups,
        "supporting_case_rows": supporting_case_rows,
        "risk_points": risk_points,
        "risk_level": risk_level,
        "analysis_summary": repair_text(history.get("model_analysis_summary")) or _history_case_summary(history),
        "prediction": {
            "label": prediction_label,
            "conclusion": prediction_conclusion,
            "confidence": _history_prediction_confidence(history),
            "explanation": prediction_explanation,
            "risk_level": risk_level,
            "suggested_actions": _history_suggested_actions(history),
        },
        "created_at": history.get("created_at"),
        "updated_at": history.get("updated_at"),
        "last_viewed_at": history.get("last_viewed_at") or history.get("updated_at") or history.get("created_at"),
        "view_count": int(history.get("view_count") or 0),
        "history_id": int(history.get("id") or 0),
        "node_label": _history_node_label(history),
        "has_prediction": has_prediction,
        "module_code": module_code,
    }


def _token_set(values: list[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        normalized = normalize_case_text(value)
        for token in normalized.split():
            if len(token) >= 2:
                tokens.add(token)
    return tokens


def _overlap_ratio(left: list[str], right: list[str]) -> float:
    left_set = {item.lower() for item in _safe_list(left)}
    right_set = {item.lower() for item in _safe_list(right)}
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / max(len(left_set | right_set), 1)


def _history_similarity(left: dict, right: dict) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    if repair_text(left.get("case_type")).lower() and repair_text(left.get("case_type")).lower() == repair_text(right.get("case_type")).lower():
        score += 0.25
        reasons.append("案件类型相同")
    if repair_text(left.get("country")).lower() and repair_text(left.get("country")).lower() == repair_text(right.get("country")).lower():
        score += 0.15
        reasons.append("国家相同")
    if repair_text(left.get("court_level")).lower() and repair_text(left.get("court_level")).lower() == repair_text(right.get("court_level")).lower():
        score += 0.10
        reasons.append("法院级别相近")

    rule_overlap = _overlap_ratio(
        [item.get("title", "") for item in left.get("legal_rules", [])],
        [item.get("title", "") for item in right.get("legal_rules", [])],
    )
    if rule_overlap > 0:
        score += 0.30 * rule_overlap
        reasons.append("涉及相似法律法规")

    risk_overlap = _overlap_ratio(
        [item.get("name", "") for item in left.get("risk_points", [])],
        [item.get("name", "") for item in right.get("risk_points", [])],
    )
    if risk_overlap > 0:
        score += 0.20 * risk_overlap
        reasons.append("风险点相近")

    if repair_text(left.get("prediction", {}).get("label")).lower() and repair_text(left.get("prediction", {}).get("label")).lower() == repair_text(right.get("prediction", {}).get("label")).lower():
        score += 0.10
        reasons.append("预测标签一致")

    text_overlap = _overlap_ratio(
        list(_token_set([left.get("case_summary", ""), left.get("case_title", "")])),
        list(_token_set([right.get("case_summary", ""), right.get("case_title", "")])),
    )
    if text_overlap > 0:
        score += 0.10 * min(text_overlap, 1.0)
        reasons.append("案情摘要语义接近")

    return max(0.0, min(round(score, 4), 1.0)), reasons[:3]


def _graph_similarity(left: dict, right: dict) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    left_case = repair_text(left.get("case_type")).lower()
    right_case = repair_text(right.get("case_type")).lower()
    if left_case and left_case == right_case:
        score += 0.30
        reasons.append("案件类型相同")

    rule_overlap = _overlap_ratio(
        [item.get("title", "") for item in left.get("legal_rules", [])],
        [item.get("title", "") for item in right.get("legal_rules", [])],
    )
    if rule_overlap > 0:
        score += 0.42 * rule_overlap
        reasons.append("涉及相似法律法规")

    text_overlap = _overlap_ratio(
        list(_token_set([left.get("case_summary", ""), left.get("case_title", ""), left.get("query_text", "")])),
        list(_token_set([right.get("case_summary", ""), right.get("case_title", ""), right.get("query_text", "")])),
    )
    if text_overlap > 0:
        score += 0.22 * min(text_overlap, 1.0)
        reasons.append("案情摘要语义接近")

    left_risks = [item.get("name", "") for item in left.get("risk_points", []) if item.get("name") not in {"证据链不足", "条款解释风险", "损失证明不足", "程序风险"}]
    right_risks = [item.get("name", "") for item in right.get("risk_points", []) if item.get("name") not in {"证据链不足", "条款解释风险", "损失证明不足", "程序风险"}]
    risk_overlap = _overlap_ratio(left_risks, right_risks)
    if risk_overlap > 0:
        score += 0.12 * risk_overlap
        reasons.append("风险点相近")

    if not reasons:
        return 0.0, []
    return max(0.0, min(round(score, 4), 1.0)), reasons[:3]


def build_user_history_graph(
    *,
    user_id: int,
    case_type: str = "",
    country: str = "",
    court_level: str = "",
    legal_rule: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 200,
) -> dict:
    histories = list_user_histories(
        user_id=user_id,
        case_type=case_type,
        country=country,
        court_level=court_level,
        legal_rule=legal_rule,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )
    details = [build_history_display_payload(item) for item in histories]

    nodes = []
    for item in details:
        graph_risk_points = _history_graph_risk_points(item)
        nodes.append(
            {
                "id": f"history_{item['id']}",
                "historyId": item["id"],
                "nodeLabel": item["node_label"],
                "date": str(item.get("created_at") or "")[:10],
                "caseTitle": item["case_title"],
                "caseType": item["case_type"],
                "country": item["country"],
                "courtLevel": item["court_level"],
                "riskLevel": item["risk_level"],
                "prediction": item["prediction"]["conclusion"],
                "confidence": item["prediction"]["confidence"],
                "laws": [rule.get("title", "") for rule in item["legal_rules"][:4]],
                "riskPoints": graph_risk_points,
                "createdAt": str(item["created_at"] or ""),
                "lastViewedAt": str(item["last_viewed_at"] or ""),
                "visitCount": item["view_count"],
            }
        )

    candidate_links = []
    for left_index in range(len(details)):
        for right_index in range(left_index + 1, len(details)):
            similarity, reasons = _graph_similarity(details[left_index], details[right_index])
            if similarity < 0.28:
                continue
            candidate_links.append(
                {
                    "source": f"history_{details[left_index]['id']}",
                    "target": f"history_{details[right_index]['id']}",
                    "similarity": similarity,
                    "relation_reason": "，".join(reasons) or "案情存在一定关联",
                }
            )

    link_quota: dict[str, int] = {}
    links = []
    for link in sorted(candidate_links, key=lambda item: item["similarity"], reverse=True):
        source = str(link["source"])
        target = str(link["target"])
        if link_quota.get(source, 0) >= 4 or link_quota.get(target, 0) >= 4:
            continue
        link_quota[source] = link_quota.get(source, 0) + 1
        link_quota[target] = link_quota.get(target, 0) + 1
        links.append(link)

    return {"nodes": nodes, "links": links}


def get_history_graph(history_id: int, *, user_id: int | None = None, admin: bool = False) -> dict:
    history = get_history(history_id, user_id=user_id, admin=admin, touch=False)
    if not history:
        return {"nodes": [], "links": []}
    if admin:
        owner_id = int(history.get("user_id") or 0)
    else:
        owner_id = int(user_id or 0)
    return build_user_history_graph(user_id=owner_id, limit=200)


def delete_history(history_id: int, *, user_id: int | None = None, admin: bool = False) -> bool:
    conditions = ["id = :history_id"]
    params: dict[str, Any] = {"history_id": int(history_id)}
    if not admin:
        conditions.append("user_id = :user_id")
        params["user_id"] = int(user_id or 0)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                f"""
                UPDATE search_histories
                SET status = 'deleted',
                    updated_at = CURRENT_TIMESTAMP
                WHERE {' AND '.join(conditions)}
                """
            ),
            params,
        )
    return result.rowcount > 0


def list_all_users(limit: int = 200) -> list[dict]:
    sql = """
    SELECT
        u.id,
        u.username,
        u.role,
        u.email,
        u.phone,
        u.organization,
        u.created_at,
        u.updated_at,
        u.last_login_at,
        u.status,
        p.real_name,
        p.country_preference,
        p.legal_type_preference,
        p.note,
        COUNT(sh.id) FILTER (WHERE sh.status = 'active') AS history_count
    FROM users u
    LEFT JOIN user_profiles p ON p.user_id = u.id
    LEFT JOIN search_histories sh ON sh.user_id = u.id
    GROUP BY u.id, p.user_id
    ORDER BY u.created_at DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"limit": max(1, min(int(limit), 500))}).mappings().all()
    return [sanitize_user(dict(row)) or {} for row in rows]


def list_all_histories(
    *,
    user_id: int | None = None,
    query_type: str = "",
    country: str = "",
    court_level: str = "",
    legal_type: str = "",
    rule_keyword: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 200,
) -> list[dict]:
    conditions = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if user_id:
        conditions.append("sh.user_id = :user_id")
        params["user_id"] = int(user_id)
    if query_type:
        conditions.append("LOWER(sh.query_type) = LOWER(:query_type)")
        params["query_type"] = repair_text(query_type)
    if country:
        conditions.append("LOWER(sh.country) = LOWER(:country)")
        params["country"] = repair_text(country)
    if court_level:
        conditions.append("LOWER(sh.court_level) LIKE LOWER(:court_level)")
        params["court_level"] = f"%{repair_text(court_level)}%"
    if legal_type:
        conditions.append("LOWER(sh.legal_type) LIKE LOWER(:legal_type)")
        params["legal_type"] = f"%{repair_text(legal_type)}%"
    if rule_keyword:
        conditions.append("CAST(sh.legal_rule_ids_json AS TEXT) ILIKE :rule_keyword")
        params["rule_keyword"] = f"%{repair_text(rule_keyword)}%"
    if start_date:
        conditions.append("sh.created_at >= :start_date")
        params["start_date"] = repair_text(start_date)
    if end_date:
        conditions.append("sh.created_at <= :end_date")
        params["end_date"] = repair_text(end_date)
    sql = _history_base_sql(conditions) + """
    ORDER BY COALESCE(sh.last_viewed_at, sh.updated_at, sh.created_at) DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def update_user_status(user_id: int, status: str) -> dict:
    normalized = "disabled" if str(status or "").strip().lower() in {"disabled", "disable", "0"} else "active"
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE users
                SET status = :status,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :user_id
                """
            ),
            {"user_id": int(user_id), "status": normalized},
        )
    return get_user_by_id(user_id) or {}


def build_history_snapshot(
    *,
    page_id: str,
    restore_url: str,
    module_code: str,
    query_text: str,
    query_params: dict | None,
    result_payload: dict | None,
) -> dict:
    payload = result_payload or {}
    return {
        "page_id": repair_text(page_id),
        "restore_url": repair_text(restore_url),
        "module_code": repair_text(module_code),
        "query_text": repair_text(query_text),
        "query_params": query_params or {},
        "analysis": payload.get("analysis", {}),
        "prediction": payload.get("prediction", {}),
        "module_packet": payload.get("module_packet", {}),
    }
