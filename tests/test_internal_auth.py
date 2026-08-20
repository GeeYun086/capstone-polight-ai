"""내부 API 인증 검증.

없으면 배포 즉시 주소만 알면 누구나 분석을 걸어 크레딧을 소진시킬 수 있고,
documentId만 알면 타인의 약관 질의 결과를 받아볼 수 있다.

인증이 라우터 단위로 걸려 있는지도 확인한다. 엔드포인트마다 붙이는 방식은
새 엔드포인트를 추가할 때 빠뜨려도 아무 에러가 나지 않아 알 방법이 없다.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.auth import HEADER_NAME
from app.core.config import get_settings
from app.main import app
from app.repositories import get_vector_repository

KEY = "test-secret-key"

ANALYSIS_BODY = {
    "analysisResultId": "a-1",
    "documentId": "doc-1",
    "downloadUrl": "https://example.test/p.pdf",
    "userId": "u-1",
    "tripId": "t-1",
}
QUERY_BODY = {"userId": "u-1", "tripId": "t-1", "documentId": "doc-1", "question": "질문"}

# /internal 아래 모든 엔드포인트. 새로 추가하면 여기에도 넣어야 한다.
INTERNAL_ENDPOINTS = [
    ("/internal/analysis", ANALYSIS_BODY),
    ("/internal/rag/query", QUERY_BODY),
]


@pytest.fixture
def secured_client(monkeypatch, fake_repo):
    """키가 설정된 상태의 클라이언트."""
    get_settings.cache_clear()
    monkeypatch.setenv("INTERNAL_API_KEY", KEY)

    app.dependency_overrides[get_vector_repository] = lambda: fake_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.mark.parametrize("path,body", INTERNAL_ENDPOINTS)
def test_rejects_missing_key(secured_client, path, body):
    response = secured_client.post(path, json=body)

    assert response.status_code == 401


@pytest.mark.parametrize("path,body", INTERNAL_ENDPOINTS)
def test_rejects_wrong_key(secured_client, path, body):
    response = secured_client.post(path, json=body, headers={HEADER_NAME: "wrong-key"})

    assert response.status_code == 401


@pytest.mark.parametrize("path,body", INTERNAL_ENDPOINTS)
def test_accepts_correct_key(secured_client, path, body):
    response = secured_client.post(path, json=body, headers={HEADER_NAME: KEY})

    assert response.status_code != 401


# 헬스체크는 열려 있어야 한다. 로드밸런서와 EC2 상태 확인이 키를 실어 보내지 않는다.
def test_health_needs_no_key(secured_client):
    assert secured_client.get("/health").status_code == 200


# 어느 쪽이 틀렸는지(헤더 없음 / 값 불일치) 응답으로 알려주면 공격자에게 단서가 된다.
def test_error_message_does_not_leak_reason(secured_client):
    without = secured_client.post("/internal/rag/query", json=QUERY_BODY).json()
    wrong = secured_client.post(
        "/internal/rag/query", json=QUERY_BODY, headers={HEADER_NAME: "x"}
    ).json()

    assert without == wrong


# 키가 없으면 통과시킨다. 로컬 개발과 테스트에서 매번 키를 넣게 하면 번거롭다.
# 대신 배포 환경(SPRING_BASE_URL이 있는 상태)에서는 기동을 막는다.
def test_passes_through_when_key_not_configured(client):
    assert client.post("/internal/rag/query", json=QUERY_BODY).status_code != 401


# non-ASCII 헤더가 와도 401이어야 한다. 500이 나면 인증 실패인지 서버 오류인지
# 구분이 안 되고, 공격자에게 "뭔가 터졌다"는 신호를 준다.
#
# secrets.compare_digest는 str에 대해 ASCII만 허용해서, 그대로 비교하면 TypeError가
# 나 500이 된다. Starlette이 헤더를 latin-1로 디코딩하므로 실제로 그런 값이 들어올 수 있다.
#
# TestClient로는 재현할 수 없다. httpx가 서버에 닿기 전에 클라이언트 단에서 거절한다.
# 실제 공격자는 raw HTTP로 임의 바이트를 보낼 수 있으므로, 의존성을 직접 호출해 확인한다.
#
# pytest-asyncio를 넣지 않고 asyncio.run으로 감싼다. 이 한 건을 위해 테스트
# 의존성을 늘릴 이유가 없다.
def test_non_ascii_key_is_rejected_not_crashed(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.core.auth import verify_internal_api_key

    get_settings.cache_clear()
    monkeypatch.setenv("INTERNAL_API_KEY", KEY)

    request = Request({
        "type": "http", "method": "POST", "path": "/internal/rag/query",
        # latin-1로 디코딩되면 non-ASCII 문자가 되는 바이트열
        "headers": [(HEADER_NAME.lower().encode(), b"\xc7\xd1\xb1\xb9")],
        "query_string": b"", "scheme": "http", "server": ("test", 80),
    })

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify_internal_api_key(request))

    assert caught.value.status_code == 401
    get_settings.cache_clear()
