import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.repositories import get_vector_repository

logger = logging.getLogger(__name__)

settings = get_settings()
configure_logging(settings.log_level)

# 배포 환경에서 인증 키 없이 뜨는 것을 막는다.
#
# 없으면 주소만 알면 누구나 분석을 걸어 크레딧을 소진시키거나, documentId만 알면
# 타인의 약관 질의 결과를 받아볼 수 있다. 가장 위험한 것은 "설정을 잊었는데
# 조용히 열려 있는" 상태이므로, 로그 경고로 끝내지 않고 기동을 실패시킨다.
#
# 판단 기준을 SPRING_BASE_URL로 삼는다. 이 값이 있으면 실제 백엔드와 통신하는
# 환경이라는 뜻이고, 로컬 개발에서는 비어 있어 개발이 막히지 않는다.
if settings.spring_base_url and not settings.internal_api_key:
    raise RuntimeError(
        "SPRING_BASE_URL이 설정된 환경에서는 INTERNAL_API_KEY가 반드시 필요합니다. "
        "openssl rand -base64 32 으로 생성해 Spring과 같은 값을 쓰십시오."
    )

# 종료 시 DB 연결 풀을 정리한다.
#
# 안 닫으면 DB 쪽에 유휴 연결이 남는다. EC2를 재배포할 때마다 쌓이면
# RDS의 max_connections를 밀어붙이게 된다.
#
# 파일 저장소를 쓰는 로컬 개발에서는 close()가 없으므로 확인 후 호출한다.
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    repository = get_vector_repository()
    if hasattr(repository, "close"):
        repository.close()
        logger.info("저장소 정리 완료")


app = FastAPI(
    lifespan=lifespan,
    title=settings.app_name,
    description="Polight AI RAG 서버 - 여행자보험 약관 분석 및 질의응답 내부 API (Spring Boot 연동용)",
    version="0.1.0",
    openapi_tags=[
        {"name": "health", "description": "인프라 헬스체크"},
        {
            "name": "analysis",
            "description": "약관 분석 요청 접수. 비동기로 처리되며 완료/실패는 Spring 콜백으로 통지된다.",
        },
        {
            "name": "rag",
            "description": "약관 질의응답. 현재는 pgvector 연결 전이라 stub 응답을 반환한다.",
        },
    ],
)
app.include_router(api_router)
register_exception_handlers(app)
