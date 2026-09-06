"""FastAPI 应用装配：创建 app、CORS 与请求日志中间件，并挂载各业务域路由（业务逻辑见 app/core.py 与 app/routers/）。"""

from __future__ import annotations

import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.core import _request_logger
from app.logging_utils import log_event
from app.routers.auth_admin import router as auth_admin_router
from app.routers.auth_oidc import router as auth_oidc_router
from app.routers.clash_profile import router as clash_profile_router
from app.routers.couple import router as couple_router
from app.routers.exports import router as exports_router
from app.routers.formula_colors import router as formula_colors_router
from app.routers.gold_spread_alerts import router as gold_spread_alerts_router
from app.routers.market_snapshot import router as market_snapshot_router
from app.routers.miniapp_accounts import router as miniapp_accounts_router
from app.routers.notify import router as notify_router
from app.routers.ops import router as ops_router
from app.routers.backups import router as backups_router
from app.routers.versions import router as versions_router
from app.routers.recipes import router as recipes_router
from app.routers.quality_reports import router as quality_reports_router
from app.routers.system_config import router as system_config_router
from app.routers.sync import router as sync_router
from app.routers.tplus_bom import router as tplus_bom_router
from app.routers.wecom_assistant import router as wecom_assistant_router
from app.routers.webhooks import router as webhooks_router


app = FastAPI(title="AliECS Backend API", version="0.4.0")
app.include_router(webhooks_router)
app.include_router(ops_router)
app.include_router(backups_router)
app.include_router(clash_profile_router)
app.include_router(versions_router)
app.include_router(gold_spread_alerts_router)
app.include_router(market_snapshot_router)
app.include_router(notify_router)
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
app.include_router(sync_router)

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


@app.exception_handler(RequestValidationError)
async def _log_validation_errors(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 至少要在服务端留下「哪个字段不合法」。

    422 的响应体只回给调用方，服务端日志里原来只有一行 `status_code: 422`，
    定位靠猜。gold-spread-monitor 每轮固定 3 个 422 + 1 个 200 这件事就是这么
    卡住的：`_try_post` 把 422 当可重试错误重试 3 次（4xx 是确定性错误，重试必然
    全失败，只是把一次失败放大成三条），而服务端查不出被拒的是哪个字段。

    ⚠️ 只记 `type` 和 `loc`（字段路径），**不记 `input` 和 `msg`**——那两个字段
    会把请求体的值抄进日志。行情数字无所谓，但这个处理器对全站所有端点生效，
    登录、密钥、回调都会经过它。

    响应体保持 FastAPI 默认形状不变，否则调用方的错误处理会跟着变。
    """
    log_event(
        _request_logger,
        "request validation failed",
        request_id=request.headers.get("x-request-id", uuid.uuid4().hex),
        method=request.method,
        path=request.url.path,
        status_code=422,
        fields=[
            {"type": item.get("type"), "loc": ".".join(str(part) for part in item.get("loc", ()))}
            for item in exc.errors()[:20]
        ],
    )
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


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
