from fastapi import APIRouter

from app.api.routes import analysis, health, rag
from app.core.config import get_settings

settings = get_settings()

api_router = APIRouter()
api_router.include_router(health.router)  # /health — 인프라 헬스체크용, prefix 없이 루트에 둠

internal_router = APIRouter(prefix=settings.api_prefix)
internal_router.include_router(rag.router)
internal_router.include_router(analysis.router)

api_router.include_router(internal_router)
