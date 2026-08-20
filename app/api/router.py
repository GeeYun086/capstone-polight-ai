from fastapi import APIRouter, Depends

from app.api.routes import analysis, health, rag
from app.core.auth import verify_internal_api_key
from app.core.config import get_settings

settings = get_settings()

api_router = APIRouter()
api_router.include_router(health.router)  # /health — 인프라 헬스체크용, prefix 없이 루트에 둠

# 인증은 라우터 단위로 건다. 엔드포인트마다 붙이면 새 엔드포인트를 추가할 때
# 빠뜨리기 쉽고, 빠뜨려도 아무 에러가 나지 않아 알 방법이 없다.
# /health는 이 라우터 밖에 있어 인증 없이 열려 있다(로드밸런서 헬스체크용).
internal_router = APIRouter(
    prefix=settings.api_prefix,
    dependencies=[Depends(verify_internal_api_key)],
)
internal_router.include_router(rag.router)
internal_router.include_router(analysis.router)

api_router.include_router(internal_router)
