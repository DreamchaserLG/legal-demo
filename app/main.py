'''

@-*- coding: utf-8 -*-

@ python：python 3.9

@ 创建人员：lg

@ 创建时间：2026/3/30

'''
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import settings

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=settings.app_name)

app.include_router(router)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}