"""백엔드와 어긋날 수 있는 지점을 우리 쪽에서 흡수하는지 검증.

물어보고 기다리는 대신 양쪽 다 받아들이게 만든 것들이다.
합의가 늦어져도 연동이 막히지 않고, 나중에 바뀌어도 깨지지 않는다.
"""

import httpx
import pytest

from app.core.config import Settings
from app.schemas.analysis import AnalysisStartRequest
from app.services import analysis_service

BASE = {"analysisResultId": "a-1", "documentId": "d-1", "userId": "u-1", "tripId": "t-1"}


# 백엔드 답변서 예시는 fileUrl인데 우리 스키마는 downloadUrl이었다.
# 이름 하나 때문에 첫 요청이 422로 튕기고 원인을 찾는 시간이 아깝다.
@pytest.mark.parametrize("field", ["downloadUrl", "fileUrl", "download_url"])
def test_accepts_either_url_field_name(field):
    request = AnalysisStartRequest.model_validate({**BASE, field: "https://x.test/p.pdf"})

    assert request.download_url == "https://x.test/p.pdf"


def test_missing_url_still_rejected():
    with pytest.raises(Exception):
        AnalysisStartRequest.model_validate(BASE)


# 콜백 경로는 Spring에 아직 엔드포인트가 없어 우리가 정한 값이다.
# 다르면 404가 나고 상태가 PROCESSING에 고착되므로, 재배포 없이 맞출 수 있어야 한다.
def test_callback_paths_are_configurable(monkeypatch):
    from app.clients import spring_client

    sent = []
    monkeypatch.setattr(spring_client, "_post", lambda path, payload: sent.append(path))
    monkeypatch.setattr(
        spring_client, "get_settings",
        lambda: Settings(callback_complete_path="/api/v2/analysis/{id}/done"),
    )

    from app.schemas.analysis import AnalysisCompleteCallback
    spring_client.notify_complete(
        AnalysisCompleteCallback(analysisResultId="ABC", summary="s", coverageItems=[])
    )

    assert sent == ["/api/v2/analysis/ABC/done"]


# 주소가 S3 presigned인지 Spring 엔드포인트인지 확정되지 않았다.
# 키가 있으면 항상 실어 보낸다. presigned URL은 서명에 없는 헤더를 무시한다.
def test_download_carries_api_key(monkeypatch, tmp_path):
    captured = {}

    class FakeStream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_bytes(self): return [b"%PDF-1.4"]

    def fake_stream(method, url, headers=None, **kwargs):
        captured["headers"] = headers
        return FakeStream()

    monkeypatch.setattr(analysis_service.httpx, "stream", fake_stream)
    monkeypatch.setattr(analysis_service, "RAW_PDF_DIR", tmp_path)
    monkeypatch.setattr(
        analysis_service, "get_settings", lambda: Settings(internal_api_key="k-1")
    )

    analysis_service._download_pdf("https://x.test/p.pdf", "doc-1")

    assert captured["headers"]["X-Internal-Api-Key"] == "k-1"


# 일시적인 네트워크 문제로 약관 하나를 통째로 날리는 것은 아깝다.
def test_download_retries_transient_failure(monkeypatch, tmp_path):
    attempts = []

    class FakeStream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_bytes(self): return [b"%PDF-1.4"]

    def fake_stream(method, url, **kwargs):
        attempts.append(url)
        if len(attempts) < 2:
            raise httpx.ConnectError("일시적 실패")
        return FakeStream()

    monkeypatch.setattr(analysis_service.httpx, "stream", fake_stream)
    monkeypatch.setattr(analysis_service.time, "sleep", lambda s: None)
    monkeypatch.setattr(analysis_service, "RAW_PDF_DIR", tmp_path)
    monkeypatch.setattr(analysis_service, "get_settings", lambda: Settings())

    analysis_service._download_pdf("https://x.test/p.pdf", "doc-1")

    assert len(attempts) == 2


# 주소가 틀렸거나 만료됐으면 다시 받아도 같다. 재시도하면 실패를 늦게 알게 된다.
def test_download_does_not_retry_4xx(monkeypatch, tmp_path):
    attempts = []

    def fake_stream(method, url, **kwargs):
        attempts.append(url)
        raise httpx.HTTPStatusError(
            "만료", request=httpx.Request("GET", url),
            response=httpx.Response(403, request=httpx.Request("GET", url)),
        )

    monkeypatch.setattr(analysis_service.httpx, "stream", fake_stream)
    monkeypatch.setattr(analysis_service.time, "sleep", lambda s: None)
    monkeypatch.setattr(analysis_service, "RAW_PDF_DIR", tmp_path)
    monkeypatch.setattr(analysis_service, "get_settings", lambda: Settings())

    with pytest.raises(RuntimeError, match="내려받지 못했습니다"):
        analysis_service._download_pdf("https://x.test/p.pdf", "doc-1")

    assert len(attempts) == 1
