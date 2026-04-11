'''

@-*- coding: utf-8 -*-

@ python：python 3.9

@ 创建人员：lg

@ 创建时间：2026/3/30

'''
import os
from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


class Settings:
    def __init__(self):
        self.app_name = os.getenv("APP_NAME", "Legal Demo MVP")
        self.app_host = os.getenv("APP_HOST", "127.0.0.1")
        self.app_port = int(os.getenv("APP_PORT", "8000"))

        self.database_url = os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg2://postgres:123456@127.0.0.1:5432/legal_ai"
        )

        self.request_timeout = int(os.getenv("REQUEST_TIMEOUT", "30"))
        self.default_search_limit = int(os.getenv("DEFAULT_SEARCH_LIMIT", "30"))

        self.canlii_database_pages = _split_csv(os.getenv("CANLII_DATABASE_PAGES", ""))

        self.ofac_sdn_csv_url = os.getenv("OFAC_SDN_CSV_URL", "").strip()
        self.ofac_add_csv_url = os.getenv("OFAC_ADD_CSV_URL", "").strip()
        self.ofac_alt_csv_url = os.getenv("OFAC_ALT_CSV_URL", "").strip()
        self.ofac_sdn_comments_csv_url = os.getenv("OFAC_SDN_COMMENTS_CSV_URL", "").strip()


settings = Settings()