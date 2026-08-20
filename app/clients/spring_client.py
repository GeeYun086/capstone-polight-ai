import logging
import time

import httpx

from app.core.auth import HEADER_NAME
from app.core.config import get_settings
from app.schemas.analysis import AnalysisCompleteCallback, AnalysisFailCallback

logger = logging.getLogger(__name__)


class SpringCallbackError(Exception):
    pass


# 콜백 재시도 설정.
#
# 재시도가 필요한 이유는 콜백 실패가 조용한 고장을 만들기 때문이다.
# Spring의 청크 조회 쿼리는 analysis_results.status = COMPLETED로 필터하는데,
# 그 상태를 COMPLETED로 바꾸는 것이 이 콜백이다. 즉 콜백이 실패하면
# policy_chunks에 데이터가 다 들어가 있는데도 검색은 영원히 0건이 된다.
# 사용자에게는 "분석은 끝났다는데 챗봇이 아무것도 못 찾는" 상태로 보인다.
#
# 지수 백오프를 쓴다. Spring 재배포처럼 몇 초 뒤 살아나는 상황이 대부분이라,
# 간격을 벌리면 성공 확률이 올라간다. 백엔드가 콜백 수신을 멱등으로 만들기로
# 했으므로 중복 전송은 안전하다.
CALLBACK_MAX_ATTEMPTS = 3
CALLBACK_BACKOFF_SECONDS = 2.0

# 다시 보내도 결과가 같은 응답은 재시도하지 않는다.
# 400(스키마 불일치)이나 401(인증 실패)은 기다려도 해결되지 않는다.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _should_retry(error: httpx.HTTPError) -> bool:
    # 연결 실패·타임아웃은 응답이 없으므로 재시도 대상이다
    if not isinstance(error, httpx.HTTPStatusError):
        return True
    return error.response.status_code in RETRYABLE_STATUS


def _post(path: str, payload: dict) -> None:
    settings = get_settings()

    # 아직 백엔드 주소가 확정되지 않은 로컬 개발 단계에서는 호출을 건너뛰고 로그만 남긴다.
    if not settings.spring_base_url:
        logger.warning("SPRING_BASE_URL이 설정되지 않아 콜백을 건너뜁니다: %s %s", path, payload)
        return

    url = f"{settings.spring_base_url.rstrip('/')}{path}"

    # 콜백도 인증이 필요하다. Spring이 같은 키로 검증하므로,
    # 빠뜨리면 분석은 성공했는데 콜백이 401로 거절돼 상태가 PROCESSING에 남는다.
    headers = {HEADER_NAME: settings.internal_api_key} if settings.internal_api_key else {}
    last_error: httpx.HTTPError | None = None

    for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=10.0)
            response.raise_for_status()
            if attempt > 1:
                logger.info("Spring 콜백 %d번째 시도에 성공: %s", attempt, url)
            return
        except httpx.HTTPError as e:
            last_error = e
            if not _should_retry(e):
                logger.error("Spring 콜백 실패 (재시도해도 같은 결과, %s): %s", url, e)
                break
            if attempt < CALLBACK_MAX_ATTEMPTS:
                delay = CALLBACK_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Spring 콜백 실패 (%d/%d), %.0f초 후 재시도: %s",
                    attempt, CALLBACK_MAX_ATTEMPTS, delay, e,
                )
                time.sleep(delay)

    logger.error("Spring 콜백 호출 실패 (%s): %s", url, last_error)
    raise SpringCallbackError(f"{url} 호출 실패: {last_error}") from last_error


def notify_complete(callback: AnalysisCompleteCallback) -> None:
    path = get_settings().callback_complete_path.format(id=callback.analysis_result_id)
    _post(path, callback.model_dump(by_alias=True))


def notify_fail(callback: AnalysisFailCallback) -> None:
    path = get_settings().callback_fail_path.format(id=callback.analysis_result_id)
    _post(path, callback.model_dump(by_alias=True))
