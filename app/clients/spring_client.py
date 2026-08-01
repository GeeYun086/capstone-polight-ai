import logging

import httpx

from app.core.config import get_settings
from app.schemas.analysis import AnalysisCompleteCallback, AnalysisFailCallback

logger = logging.getLogger(__name__)


class SpringCallbackError(Exception):
    pass


def _post(path: str, payload: dict) -> None:
    settings = get_settings()

    # 아직 백엔드 주소가 확정되지 않은 로컬 개발 단계에서는 호출을 건너뛰고 로그만 남긴다.
    if not settings.spring_base_url:
        logger.warning("SPRING_BASE_URL이 설정되지 않아 콜백을 건너뜁니다: %s %s", path, payload)
        return

    url = f"{settings.spring_base_url.rstrip('/')}{path}"
    try:
        response = httpx.post(url, json=payload, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.error("Spring 콜백 호출 실패 (%s): %s", url, e)
        raise SpringCallbackError(f"{url} 호출 실패: {e}") from e


def notify_complete(callback: AnalysisCompleteCallback) -> None:
    path = f"/internal/analysis-results/{callback.analysis_result_id}/complete"
    _post(path, callback.model_dump(by_alias=True))


def notify_fail(callback: AnalysisFailCallback) -> None:
    path = f"/internal/analysis-results/{callback.analysis_result_id}/fail"
    _post(path, callback.model_dump(by_alias=True))
