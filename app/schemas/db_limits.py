"""콜백 payload의 VARCHAR 길이 한도와, 그 한도로 잘라내는 함수.

db_enums가 CHECK 제약을 모아둔 것과 같은 이유로 여기에 둔다. 다만 이쪽은
어기면 증상이 더 나쁘다.

백엔드는 길이 검증을 아직 넣지 않았다. 한도를 넘기면 400이 아니라 **500**이
오고(DB 제약 위반), 500은 재시도 대상이라 우리 클라이언트가 세 번 다 500을
받고 콜백을 포기한다. 그러면 분석은 성공했는데 analysis_results.status가
PROCESSING에 남고, 문서당 분석은 1회뿐이라(UNIQUE(document_id)) 다시 시도할
수도 없다. 사용자에게는 영원히 로딩 중인 화면으로 보이고 에러도 뜨지 않는다.

약관 경로(callback_mapper)와 증권 경로(certificate_adapter)가 같은 표를 쓴다.
한쪽에만 두면 다른 쪽이 조용히 빠진다 - 실제로 증권 경로가 빠져 있었다.
"""

import logging

logger = logging.getLogger(__name__)

# 백엔드 콜백 규격서에 실린 VARCHAR 길이.
#
# sub_limit_value가 특히 위험하다. 약관 원문("보험가입금액을 한도로 실제 발생한
# 비용 전액…")을 그대로 담으면 200자를 넘기기 쉽다.
#
# limit_label도 위험하다. 증권 경로에서는 에이전트가 뽑은 금액 문자열을 그대로
# 싣기 때문에, 어떤 길이가 올지 우리가 통제하지 못한다.
MAX_LENGTHS = {
    "title": 200,
    "subtitle": 500,
    "category": 100,
    "limit_label": 100,
    "limit_currency": 10,
    "document_name": 200,
    "sub_limit_label": 100,
    "sub_limit_value": 200,
    "description": 500,
}


def cut(value: str | None, key: str) -> str | None:
    if value is None:
        return None
    limit = MAX_LENGTHS[key]
    if len(value) <= limit:
        return value
    logger.info("%s가 %d자를 넘어 잘랐습니다 (%d자)", key, limit, len(value))
    return value[:limit]
