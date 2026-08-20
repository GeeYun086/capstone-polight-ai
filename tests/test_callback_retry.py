"""콜백 재시도 검증.

콜백 실패는 조용한 고장을 만든다. Spring의 청크 조회 쿼리가
analysis_results.status = COMPLETED로 필터하는데 그 상태를 바꾸는 것이 이 콜백이다.
실패하면 policy_chunks에 데이터가 다 들어가 있는데도 검색이 영원히 0건이 되고,
사용자에게는 "분석은 끝났다는데 챗봇이 아무것도 못 찾는" 상태로 보인다.

sleep은 전부 대체한다. 실제로 기다리면 테스트가 6초씩 걸린다.
"""

import httpx
import pytest

from app.clients import spring_client
from app.core.config import Settings
from app.clients.spring_client import SpringCallbackError

PATH = "/internal/analysis-results/a-1/complete"


@pytest.fixture(autouse=True)
def no_sleep_and_base_url(monkeypatch):
    monkeypatch.setattr(spring_client.time, "sleep", lambda s: None)
    # 가짜 객체 대신 실제 Settings를 쓴다. 필드를 추가할 때마다 가짜가
    # 따라가지 못해 AttributeError로 깨지는 것을 막는다.
    monkeypatch.setattr(
        spring_client, "get_settings",
        lambda: Settings(spring_base_url="http://spring.test", internal_api_key="test-key"),
    )


def make_post(responses: list):
    """호출마다 responses에서 하나 꺼내 쓴다. 예외면 raise, 정수면 그 상태코드."""
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, headers))
        outcome = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        response = httpx.Response(outcome, request=httpx.Request("POST", url))
        return response

    return post, calls


def test_succeeds_on_first_try(monkeypatch):
    post, calls = make_post([200])
    monkeypatch.setattr(spring_client.httpx, "post", post)

    spring_client._post(PATH, {})

    assert len(calls) == 1


# Spring 재배포 중에는 연결이 거부된다. 몇 초 뒤 살아나므로 재시도가 값어치를 한다.
def test_retries_connection_error_then_succeeds(monkeypatch):
    post, calls = make_post([httpx.ConnectError("연결 거부"), 200])
    monkeypatch.setattr(spring_client.httpx, "post", post)

    spring_client._post(PATH, {})

    assert len(calls) == 2


def test_retries_server_error(monkeypatch):
    post, calls = make_post([503, 503, 200])
    monkeypatch.setattr(spring_client.httpx, "post", post)

    spring_client._post(PATH, {})

    assert len(calls) == 3


def test_gives_up_after_max_attempts(monkeypatch):
    post, calls = make_post([503])
    monkeypatch.setattr(spring_client.httpx, "post", post)

    with pytest.raises(SpringCallbackError):
        spring_client._post(PATH, {})

    assert len(calls) == spring_client.CALLBACK_MAX_ATTEMPTS


# 스키마가 안 맞거나 인증이 틀린 경우는 기다려도 해결되지 않는다.
# 재시도하면 실패를 늦게 알게 되고 로그만 지저분해진다.
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_does_not_retry_client_errors(monkeypatch, status):
    post, calls = make_post([status])
    monkeypatch.setattr(spring_client.httpx, "post", post)

    with pytest.raises(SpringCallbackError):
        spring_client._post(PATH, {})

    assert len(calls) == 1, f"{status}는 재시도하면 안 된다"


# 간격이 벌어져야 Spring이 살아날 시간을 준다. 같은 간격으로 붙여 쏘면 의미가 적다.
def test_backoff_grows(monkeypatch):
    delays = []
    monkeypatch.setattr(spring_client.time, "sleep", lambda s: delays.append(s))
    post, _ = make_post([503])
    monkeypatch.setattr(spring_client.httpx, "post", post)

    with pytest.raises(SpringCallbackError):
        spring_client._post(PATH, {})

    assert delays == [2.0, 4.0]


# 콜백에도 인증 헤더가 실려야 한다. 빠뜨리면 분석은 성공했는데 콜백이 401로
# 거절돼 Spring 쪽 상태가 PROCESSING에 영원히 남는다.
def test_callback_carries_api_key_header(monkeypatch):
    post, calls = make_post([200])
    monkeypatch.setattr(spring_client.httpx, "post", post)

    spring_client._post(PATH, {})

    _, headers = calls[0]
    assert headers[spring_client.HEADER_NAME] == "test-key"
