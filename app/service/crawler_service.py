'''

@-*- coding: utf-8 -*-

@ python：python 3.9

@ 创建人员：lg

@ 创建时间：2026/3/30

'''

from app.service.ofac_service import sync_ofac_demo
from app.service.canlii_service import sync_canlii_demo


def sync_all_sources():
    return {
        "status": "done",
        "sources": {
            "ofac": sync_ofac_demo(),
            "canlii": sync_canlii_demo()
        }
    }