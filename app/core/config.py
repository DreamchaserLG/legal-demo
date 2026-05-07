import os

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings:
    def __init__(self):
        self.app_name = os.getenv("APP_NAME", "Legal Demo MVP")
        self.app_env = os.getenv("APP_ENV", "development").strip().lower()
        self.app_host = os.getenv("APP_HOST", "127.0.0.1")
        self.app_port = int(os.getenv("APP_PORT", "8000"))

        self.database_url = os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg2://postgres:change_me@127.0.0.1:5432/legal_demo",
        )

        self.request_timeout = int(os.getenv("REQUEST_TIMEOUT", "30"))
        self.llm_timeout = int(os.getenv("LLM_TIMEOUT", "90"))
        self.llm_retry_count = int(os.getenv("LLM_RETRY_COUNT", "2"))
        self.llm_retry_backoff_ms = int(os.getenv("LLM_RETRY_BACKOFF_MS", "800"))
        self.cache_ttl_seconds = int(os.getenv("CACHE_TTL_SECONDS", "300"))
        self.prediction_use_model_case_comparison = (
            os.getenv("PREDICTION_USE_MODEL_CASE_COMPARISON", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.analysis_local_fast_mode = (
            os.getenv("ANALYSIS_LOCAL_FAST_MODE", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.prediction_local_fast_mode = (
            os.getenv("PREDICTION_LOCAL_FAST_MODE", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.remote_search_enabled = (
            os.getenv("REMOTE_SEARCH_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.async_hydration_enabled = (
            os.getenv("ASYNC_HYDRATION_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.remote_search_trigger_count = int(os.getenv("REMOTE_SEARCH_TRIGGER_COUNT", "1"))
        self.remote_search_max_items_per_source = int(os.getenv("REMOTE_SEARCH_MAX_ITEMS_PER_SOURCE", "12"))
        self.analysis_new_case_hydration_enabled = (
            os.getenv("ANALYSIS_NEW_CASE_HYDRATION_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.analysis_new_case_target_count = int(os.getenv("ANALYSIS_NEW_CASE_TARGET_COUNT", "0"))
        self.bilingual_result_preview_limit = int(os.getenv("BILINGUAL_RESULT_PREVIEW_LIMIT", "6"))
        self.canlii_bulk_sync_enabled = (
            os.getenv("CANLII_BULK_SYNC_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.ofac_full_snapshot_enabled = (
            os.getenv("OFAC_FULL_SNAPSHOT_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.local_archive_enabled = (
            os.getenv("LOCAL_ARCHIVE_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.archive_bootstrap_enabled = (
            os.getenv("ARCHIVE_BOOTSTRAP_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.startup_canada_sync_mode = os.getenv("STARTUP_CANADA_SYNC_MODE", "background").strip().lower()
        self.local_archive_dir = os.getenv("LOCAL_ARCHIVE_DIR", "data_archive").strip()
        self.local_archive_export_dir = os.getenv("LOCAL_ARCHIVE_EXPORT_DIR", "data_archive/exports").strip()
        self.ingestion_worker_poll_seconds = float(os.getenv("INGESTION_WORKER_POLL_SECONDS", "2"))
        self.ingestion_page_poll_seconds = int(os.getenv("INGESTION_PAGE_POLL_SECONDS", "3000"))
        self.ingestion_task_requeue_cooldown_seconds = int(
            os.getenv("INGESTION_TASK_REQUEUE_COOLDOWN_SECONDS", "900")
        )
        self.ingestion_worker_stale_seconds = int(os.getenv("INGESTION_WORKER_STALE_SECONDS", "600"))
        self.ingestion_worker_name = os.getenv("INGESTION_WORKER_NAME", "").strip()
        self.default_search_limit = int(os.getenv("DEFAULT_SEARCH_LIMIT", "30"))
        self.session_secret = os.getenv("SESSION_SECRET", "legal-demo-dev-session-secret").strip()
        self.session_max_age_seconds = int(os.getenv("SESSION_MAX_AGE_SECONDS", "604800"))
        self.demo_history_seed_enabled = (
            os.getenv(
                "DEMO_HISTORY_SEED_ENABLED",
                "true" if self.app_env != "production" else "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.initial_admin_username = os.getenv("INITIAL_ADMIN_USERNAME", "").strip()
        self.initial_admin_password = os.getenv("INITIAL_ADMIN_PASSWORD", "").strip()
        self.initial_admin_email = os.getenv("INITIAL_ADMIN_EMAIL", "").strip()

        self.canlii_database_pages = _split_csv(os.getenv("CANLII_DATABASE_PAGES", ""))
        self.canlii_remote_database_page_limit = int(os.getenv("CANLII_REMOTE_DATABASE_PAGE_LIMIT", "80"))
        self.canlii_database_discovery_ttl_seconds = int(os.getenv("CANLII_DATABASE_DISCOVERY_TTL_SECONDS", "21600"))
        self.canada_federal_sync_max_items = int(os.getenv("CANADA_FEDERAL_SYNC_MAX_ITEMS", "0"))
        self.ontario_legislation_sync_max_items = int(os.getenv("ONTARIO_LEGISLATION_SYNC_MAX_ITEMS", "0"))
        self.ontario_legislation_include_full_text = (
            os.getenv("ONTARIO_LEGISLATION_INCLUDE_FULL_TEXT", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )

        self.ofac_sdn_csv_url = os.getenv("OFAC_SDN_CSV_URL", "").strip()
        self.ofac_add_csv_url = os.getenv("OFAC_ADD_CSV_URL", "").strip()
        self.ofac_alt_csv_url = os.getenv("OFAC_ALT_CSV_URL", "").strip()
        self.ofac_sdn_comments_csv_url = os.getenv("OFAC_SDN_COMMENTS_CSV_URL", "").strip()

        self.llm_provider = os.getenv("LLM_PROVIDER", "spark").strip().lower()

        self.openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.openai_reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "low").strip()

        self.spark_api_password = os.getenv("SPARK_API_PASSWORD", "").strip()
        self.spark_api_key = os.getenv("SPARK_API_KEY", "").strip()
        self.spark_api_secret = os.getenv("SPARK_API_SECRET", "").strip()
        self.spark_app_id = os.getenv("SPARK_APP_ID", "").strip()
        self.spark_model = os.getenv("SPARK_MODEL", "Spark Ultra-32K").strip()
        self.spark_domain = os.getenv("SPARK_DOMAIN", "4.0Ultra").strip()
        self.spark_base_url = os.getenv(
            "SPARK_BASE_URL",
            "wss://spark-api.xf-yun.com/v4.0/chat",
        ).strip()
        self.spark_temperature = float(os.getenv("SPARK_TEMPERATURE", "0.2"))


settings = Settings()
