"""내부 API 인증.

Spring과 공유하는 시크릿을 헤더로 검증한다. 백엔드와 합의한 방식이다.

없으면 배포 즉시 다음이 가능해진다.
  주소만 알면 누구나 분석을 걸어 OpenAI·Upstage 크레딧을 소진시킬 수 있다
  documentId만 알면 타인의 약관 질의 결과를 받아볼 수 있다

보안그룹으로 8000 인바운드를 Spring EC2에서만 열어도, 그건 네트워크 계층
한 겹이다. 애플리케이션에서도 막아 2중 방어를 만든다.

키가 설정되지 않았을 때 통과시키는 이유:
  로컬 개발과 테스트에서 매번 키를 넣게 하면 개발이 번거로워진다. 대신 통과할
  때마다 경고를 남기고, 배포 시 키가 없으면 기동 자체를 막는다(app/main.py).
  "설정을 잊었는데 조용히 열려 있는" 상태가 가장 위험하다.
"""

import logging
import secrets

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

HEADER_NAME = "X-Internal-Api-Key"


async def verify_internal_api_key(request: Request) -> None:
    """/internal/* 라우터에 의존성으로 걸린다."""
    from app.core.config import get_settings

    expected = get_settings().internal_api_key

    if not expected:
        logger.warning(
            "INTERNAL_API_KEY가 없어 %s 요청을 인증 없이 처리합니다. 배포 환경에서는 반드시 설정하십시오.",
            request.url.path,
        )
        return

    provided = request.headers.get(HEADER_NAME)

    # secrets.compare_digest로 비교한다. == 로 비교하면 앞부분이 맞을수록 오래 걸려서,
    # 응답 시간 차이로 키를 한 글자씩 추측할 수 있다(타이밍 공격).
    #
    # bytes로 바꿔 비교한다. compare_digest는 str을 받으면 ASCII만 허용해서,
    # non-ASCII 문자가 든 헤더가 오면 TypeError로 500이 난다. Starlette이 헤더를
    # latin-1로 디코딩하므로 실제로 그런 값이 들어올 수 있고, 인증 실패를
    # 서버 오류로 보여주면 원인 파악만 어려워진다.
    if not provided or not secrets.compare_digest(
        provided.encode("utf-8", "surrogateescape"), expected.encode("utf-8")
    ):
        # 어느 쪽이 틀렸는지(헤더 없음 / 값 불일치) 알려주지 않는다.
        # 공격자에게 단서를 주지 않기 위해서다. 원인은 서버 로그에만 남긴다.
        logger.warning(
            "%s 인증 실패 (헤더 %s)",
            request.url.path,
            "없음" if not provided else "불일치",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효한 내부 API 키가 필요합니다.",
        )
