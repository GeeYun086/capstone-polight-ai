from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
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
