from pathlib import Path

import pytest

from app.schemas.analysis import AnalysisStartRequest
from app.services import analysis_service
from tests.conftest import FakeVectorRepository

REQUEST = AnalysisStartRequest(
    analysisResultId="analysis-1",
    documentId="doc-1",
    downloadUrl="https://example.test/policy.pdf",
    userId="user-1",
    tripId="trip-1",
    policyId="policy-1",
)


# is_toc_page가 평균 줄 길이 28자 미만인 페이지를 목차로 보고 제외하므로,
# 테스트 입력도 실제 약관처럼 충분히 긴 줄로 만들어야 청크가 생성된다.
SAMPLE_PAGES = [
    {
        "page": 1,
        "text": "제1조(보상하는 손해)\n"
        + "\n".join(
            f"{i}. 회사는 피보험자가 해외여행 중 입은 상해에 대하여 보험금을 지급합니다."
            for i in range(6)
        ),
    }
]


@pytest.fixture
def stub_pipeline_io(monkeypatch):
    """PDF 다운로드와 텍스트 추출만 대체한다. 청킹과 스코프 주입은 실제 코드가 돈다."""
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: Path("policy.pdf"))
    monkeypatch.setattr(analysis_service, "extract_pages", lambda path: SAMPLE_PAGES)


@pytest.fixture
def captured_callbacks(monkeypatch):
    """Spring 콜백을 가로채 payload를 기록한다. 실제 HTTP 호출은 하지 않는다."""
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        analysis_service.spring_client,
        "notify_complete",
        lambda payload: calls.append(("complete", payload.model_dump(by_alias=True))),
    )
    monkeypatch.setattr(
        analysis_service.spring_client,
        "notify_fail",
        lambda payload: calls.append(("fail", payload.model_dump(by_alias=True))),
    )
    return calls


def test_start_analysis_returns_202_immediately(client):
    response = client.post("/internal/analysis", json=REQUEST.model_dump(by_alias=True))

    assert response.status_code == 202
    assert response.json() == {"analysisResultId": "analysis-1", "status": "ACCEPTED"}


# 스코프 필드(user/trip/policy/document)가 청크에 실려야 한다.
# 실리지 않으면 검색 시 계약별 필터링이 불가능해 챗봇이 성립하지 않는다.
def test_pipeline_injects_scope_into_chunks(monkeypatch, stub_pipeline_io, captured_callbacks):
    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {c["chunk_id"]: [0.1] for c in chunks})

    repo = FakeVectorRepository()
    analysis_service.process_analysis(REQUEST, repository=repo)

    assert repo.saved, "repository.save()가 호출되지 않았다"
    chunks, _ = repo.saved[0]
    assert chunks
    for chunk in chunks:
        assert chunk["user_id"] == "user-1"
        assert chunk["trip_id"] == "trip-1"
        assert chunk["policy_id"] == "policy-1"
        assert chunk["document_id"] == "doc-1"


# 저장이 완료 콜백보다 먼저 일어나야 한다. Spring의 coverage_item_sources가 chunk_id를
# FK로 참조하므로, 청크 없이 완료를 통지하면 Spring 쪽 INSERT가 실패한다.
def test_chunks_are_saved_before_complete_callback(monkeypatch, stub_pipeline_io, captured_callbacks):
    order: list[str] = []

    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})

    class OrderTrackingRepo(FakeVectorRepository):
        def save(self, chunks, embeddings):
            order.append("save")
            super().save(chunks, embeddings)

    monkeypatch.setattr(
        analysis_service.spring_client,
        "notify_complete",
        lambda payload: order.append("complete"),
    )

    analysis_service.process_analysis(REQUEST, repository=OrderTrackingRepo())

    assert order == ["save", "complete"]


# 파이프라인이 실패하면 실패 콜백이 나가야 한다. 조용히 끝나면 Spring은 영원히 PROCESSING이다.
def test_failure_sends_fail_callback(monkeypatch, captured_callbacks):
    def boom(url, doc_id):
        raise RuntimeError("다운로드 실패")

    monkeypatch.setattr(analysis_service, "_download_pdf", boom)

    analysis_service.process_analysis(REQUEST, repository=FakeVectorRepository())

    assert len(captured_callbacks) == 1
    kind, payload = captured_callbacks[0]
    assert kind == "fail"
    assert payload["status"] == "FAILED"
    assert payload["analysisResultId"] == "analysis-1"
    assert "다운로드 실패" in payload["errorMessage"]


# 콜백 전송 실패는 분석 성공/실패와 별개 문제이므로 파이프라인을 되돌리지 않는다.
def test_callback_transport_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        analysis_service.spring_client,
        "notify_fail",
        lambda payload: (_ for _ in ()).throw(analysis_service.SpringCallbackError("Spring 다운")),
    )
    monkeypatch.setattr(
        analysis_service, "_download_pdf", lambda url, doc_id: (_ for _ in ()).throw(RuntimeError("실패"))
    )

    # 예외가 밖으로 새지 않아야 한다
    analysis_service.process_analysis(REQUEST, repository=FakeVectorRepository())
