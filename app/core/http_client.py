'''

@-*- coding: utf-8 -*-

@ python：python 3.9

@ 创建人员：lg

@ 创建时间：2026/3/30

'''
import ssl
import certifi
import requests

from requests.adapters import HTTPAdapter
from requests.exceptions import SSLError
from urllib3.util.retry import Retry


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-CA,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Connection": "close",
    "Cache-Control": "no-cache",
}


class TLSHttpAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(*args, **kwargs)


def build_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )

    adapter = TLSHttpAdapter(
        max_retries=retry,
        pool_connections=20,
        pool_maxsize=20,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(DEFAULT_HEADERS)
    session.verify = certifi.where()

    # 很关键：避免系统里错误的 HTTPS_PROXY / ALL_PROXY 影响 requests
    session.trust_env = False
    return session


http_session = build_session()


def http_get(url: str, timeout: int, headers: dict | None = None) -> requests.Response:
    try:
        merged_headers = {}
        if headers:
            merged_headers.update(headers)

        resp = http_session.get(
            url,
            timeout=timeout,
            headers=merged_headers,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp

    except SSLError as e:
        raise RuntimeError(
            f"SSL/TLS 握手失败: {url}. "
            f"这通常是本机 Python/OpenSSL/代理兼容问题。原始错误: {e}"
        ) from e