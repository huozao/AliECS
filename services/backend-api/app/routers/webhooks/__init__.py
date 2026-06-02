from __future__ import annotations

from fastapi import APIRouter

from app.routers.webhooks.chanjet import router as chanjet_router
from app.routers.webhooks.feishu import router as feishu_router
from app.routers.webhooks.wecom import router as wecom_router


router = APIRouter()
router.include_router(chanjet_router)
router.include_router(wecom_router)
router.include_router(feishu_router)
