"""FastAPI 应用装配：创建 app、CORS 与请求日志中间件，并挂载各业务域路由（业务逻辑见 app/core.py 与 app/routers/）。"""

from __future__ import annotations

import os
import time
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core import _request_logger
from app.logging_utils import log_event
from app.routers.auth_admin import router as auth_admin_router
from app.routers.auth_oidc import router as auth_oidc_router
from app.routers.couple import router as couple_router
from app.routers.exports import router as exports_router
from app.routers.formula_colors import router as formula_colors_router
from app.routers.gold_spread_alerts import router as gold_spread_alerts_router
from app.routers.miniapp_accounts import router as miniapp_accounts_router
from app.routers.ops import router as ops_router
from app.routers.backups import router as backups_router
from app.routers.versions import router as versions_router
from app.routers.recipes import router as recipes_router
from app.routers.quality_reports import router as quality_reports_router
from app.routers.system_config import router as system_config_router
from app.routers.tplus_bom import router as tplus_bom_router
from app.routers.wecom_assistant import router as wecom_assistant_router
from app.routers.webhooks import router as webhooks_router


app = FastAPI(title="AliECS Backend API", version="0.4.0")
app.include_router(webhooks_router)
app.include_router(ops_router)
app.include_router(backups_router)
app.include_router(versions_router)
app.include_router(gold_spread_alerts_router)
app.include_router(auth_admin_router)
app.include_router(auth_oidc_router)
app.include_router(recipes_router)
app.include_router(formula_colors_router)
app.include_router(quality_reports_router)
app.include_router(tplus_bom_router)
app.include_router(exports_router)
app.include_router(miniapp_accounts_router)
app.include_router(couple_router)
app.include_router(system_config_router)
app.include_router(wecom_assistant_router)

def _cors_origins() -> list[str]:
    defaults = [
        "http://localhost:8080",
        "http://localhost:8081",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8081",
        "https://localhost:8080",
        "https://localhost:8081",
        "https://127.0.0.1:8080",
        "https://127.0.0.1:8081",
    ]
    origins = {x.strip() for x in defaults if x.strip()}

    app_base = os.getenv("APP_BASE_URL", "").strip()
    if app_base:
        origins.add(app_base.rstrip("/"))

    extra = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
    if extra:
        origins.update({x.strip().rstrip("/") for x in extra.split(",") if x.strip()})

    return sorted(origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _log_requests(request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - started) * 1000, 2)
    log_event(
        _request_logger,
        "request completed",
        request_id=request.headers.get("x-request-id", uuid.uuid4().hex),
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response
