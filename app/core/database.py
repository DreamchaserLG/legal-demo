'''

@-*- coding: utf-8 -*-

@ python：python 3.9

@ 创建人员：lg

@ 创建时间：2026/3/30

'''
from contextlib import contextmanager

from sqlalchemy import create_engine, text

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True
)


@contextmanager
def get_conn(begin: bool = False):
    if begin:
        with engine.begin() as conn:
            yield conn
    else:
        with engine.connect() as conn:
            yield conn


def fetch_all(sql: str, params: dict | None = None) -> list[dict]:
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]


def execute(sql: str, params: dict | None = None):
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})