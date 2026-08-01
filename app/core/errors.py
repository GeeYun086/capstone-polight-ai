import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


# 처리되지 않은 예외가 FastAPI 기본 plain-text 500 응답으로 새지 않도록,
# Spring 쪽에서도 일관되게 파싱 가능한 JSON 형태로 통일한다.
def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
