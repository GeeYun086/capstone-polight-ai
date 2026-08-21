from pathlib import Path

import pytest

from app.schemas.analysis import AnalysisStartRequest
from app.schemas.coverage import CoverageItem, CoverageSource
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


# Upstage Document Parse가 돌려주는 요소 형태.
# 조항마다 요소가 나뉘고, category로 문단/표/목록이 구분된다.
SAMPLE_ELEMENTS = [
    {"page": 1, "category": "paragraph", "html": "",
     "text": "제1조(보상하는 손해) 회사는 피보험자가 해외여행 중 입은 상해에 대하여 보험금을 지급합니다."},
    {"page": 1, "category": "list", "html": "",
     "text": "1. 해외의료기관에서 발생한 의료비\n2. 국내 병원 입원 치료비"},
    {"page": 2, "category": "paragraph", "html": "",
     "text": "제2조(보상하지 않는 손해) 회사는 피보험자의 고의로 생긴 손해는 보상하지 않습니다."},
]


@pytest.fixture
def stub_pipeline_io(monkeypatch):
    """외부 호출만 대체한다. 청킹과 스코프 주입은 실제 코드가 돈다.

    보장항목 추출도 막는다. 막지 않으면 테스트가 실제 LLM을 호출해
    느려지고(실측 13초) 비용이 들며 네트워크 없이는 실패한다.
    """
    monkeypatch.setattr(analysis_service, "_download_pdf", lambda url, doc_id: Path("policy.pdf"))
    monkeypatch.setattr(
        "app.services.chunking_service.parse_pdf", lambda path, **kwargs: SAMPLE_ELEMENTS
    )
    monkeypatch.setattr(analysis_service, "extract_all", lambda chunks, **kwargs: ([], []))


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


# policy_chunks.analysis_result_id는 NOT NULL이자 FK다.
# 저장소에 전달되지 않으면 pgvector INSERT가 실패한다.
def test_save_receives_persistence_context(monkeypatch, stub_pipeline_io, captured_callbacks):
    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})

    repo = FakeVectorRepository()
    analysis_service.process_analysis(REQUEST, repository=repo)

    analysis_result_id, scope = repo.saved_context
    assert analysis_result_id == "analysis-1"
    assert scope is not None and scope.policy_id == "policy-1"


# Upstage 요소 기반 청킹이므로 policy_chunks의 NOT NULL 컬럼이 채워져야 한다.
def test_chunks_carry_columns_required_by_ddl(monkeypatch, stub_pipeline_io, captured_callbacks):
    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})

    repo = FakeVectorRepository()
    analysis_service.process_analysis(REQUEST, repository=repo)

    chunks, _ = repo.saved[0]
    for chunk in chunks:
        assert chunk.get("source_content_type"), "source_content_type이 비었다 (NOT NULL)"
        assert chunk.get("coverage_type"), "clause_type이 비었다 (NOT NULL)"
        assert chunk.get("char_count") is not None


# 저장이 완료 콜백보다 먼저 일어나야 한다. Spring의 coverage_item_sources가 chunk_id를
# FK로 참조하므로, 청크 없이 완료를 통지하면 Spring 쪽 INSERT가 실패한다.
def test_chunks_are_saved_before_complete_callback(monkeypatch, stub_pipeline_io, captured_callbacks):
    order: list[str] = []

    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})

    class OrderTrackingRepo(FakeVectorRepository):
        def save(self, chunks, embeddings, analysis_result_id=None, scope=None):
            order.append("save")
            super().save(chunks, embeddings, analysis_result_id, scope)

    monkeypatch.setattr(
        analysis_service.spring_client,
        "notify_complete",
        lambda payload: order.append("complete"),
    )

    analysis_service.process_analysis(REQUEST, repository=OrderTrackingRepo())

    assert order == ["save", "complete"]


# 콜백의 coverageItems는 청크 하나당 하나가 아니라 담보 단위여야 한다.
# 이전에는 청크를 그대로 뒤집어 "제33조(약관의 해석)" 같은 조항 제목이 130개 넘게 나갔고,
# coverage_items 테이블에 넣어도 화면에 쓸 수 없었다.
def test_callback_carries_extracted_coverage_items(monkeypatch, stub_pipeline_io, captured_callbacks):
    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})

    item = CoverageItem(
        title="해외여행중 휴대품손해(분실제외) 특별약관",
        category="baggage",
        isCovered=True,
        coverageStatus="COVERED",
        limitAmount=200000,
        sources=[CoverageSource(chunkId="test_0001", sourceRole="LIMIT", quoteText="…")],
    )
    monkeypatch.setattr(analysis_service, "extract_all", lambda chunks, **kwargs: ([item], []))

    analysis_service.process_analysis(REQUEST, repository=FakeVectorRepository())

    kind, payload = captured_callbacks[0]
    assert kind == "complete"
    assert len(payload["coverageItems"]) == 1
    sent = payload["coverageItems"][0]
    assert sent["title"] == "해외여행중 휴대품손해(분실제외) 특별약관"
    assert sent["coverageStatus"] == "COVERED"
    assert sent["limitAmount"] == 200000


# sources[].chunkId는 policy_chunks.id(UUID)여야 Spring이 coverage_item_sources를
# FK로 연결할 수 있다. 저장소가 INSERT하며 만든 UUID로 치환돼야 한다.
def test_source_chunk_ids_are_mapped_to_saved_uuids(monkeypatch, stub_pipeline_io, captured_callbacks):
    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})

    def _item_for(chunks):
        return CoverageItem(
            title="담보", category="baggage", isCovered=True, coverageStatus="COVERED",
            sources=[CoverageSource(chunkId=chunks[0]["chunk_id"], sourceRole="COVERAGE")],
        )

    # 저장소가 INSERT하며 청크에 policy_chunk_id를 실어주는 동작을 흉내낸다.
    # 인스턴스 상태가 아니라 청크로 전달해야 동시 분석에서 서로 덮어쓰지 않는다.
    class UuidStampingRepo(FakeVectorRepository):
        def save(self, chunks, embeddings, analysis_result_id=None, scope=None):
            for c in chunks:
                c["policy_chunk_id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            super().save(chunks, embeddings, analysis_result_id, scope)

    repo = UuidStampingRepo()
    monkeypatch.setattr(
        analysis_service, "extract_all",
        lambda chunks, **kwargs: ([_item_for(chunks)], []),
    )
    analysis_service.process_analysis(REQUEST, repository=repo)

    sent = captured_callbacks[0][1]["coverageItems"][0]
    assert [s["chunkId"] for s in sent["sources"]] == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]


# 파이프라인이 실패하면 실패 콜백이 나가야 한다. 조용히 끝나면 Spring은 영원히 PROCESSING이다.
#
# errorMessage에는 예상하지 못한 예외의 메시지를 싣지 않는다. 그 문자열이
# analysis_results.failure_reason에 영구 저장된 뒤 사용자 화면에 뜨는데,
# 안에 무엇이 들어 있을지(주소·키·원문) 알 수 없다.
def test_failure_sends_fail_callback(monkeypatch, captured_callbacks):
    def boom(url, doc_id):
        raise RuntimeError("스택 어딘가의 내부 문구")

    monkeypatch.setattr(analysis_service, "_download_pdf", boom)

    analysis_service.process_analysis(REQUEST, repository=FakeVectorRepository())

    assert len(captured_callbacks) == 1
    kind, payload = captured_callbacks[0]
    assert kind == "fail"
    assert payload["status"] == "FAILED"
    assert payload["analysisResultId"] == "analysis-1"
    assert payload["errorMessage"]
    assert "내부 문구" not in payload["errorMessage"]


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


# 콜백은 Spring이 파생하는 값을 보내지 않아야 한다.
#
# isCovered를 우리가 보내면 coverageStatus와 어긋날 여지가 생긴다. 특히
# PARTIALLY_COVERED를 false로 잘못 계산하면 부분 보장 담보가 "미보장"으로 표시된다.
# sortOrder도 Spring이 배열 인덱스로 부여하기로 합의했다.
def test_callback_omits_fields_spring_derives(monkeypatch, stub_pipeline_io, captured_callbacks):
    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})
    item = CoverageItem(
        title="담보", category="baggage", isCovered=True, coverageStatus="COVERED",
    )
    monkeypatch.setattr(analysis_service, "extract_all", lambda chunks, **kwargs: ([item], []))

    analysis_service.process_analysis(REQUEST, repository=FakeVectorRepository())

    sent = captured_callbacks[0][1]["coverageItems"][0]
    assert "isCovered" not in sent
    assert "sortOrder" not in sent


# analysis_results에 자리가 있는데 비어 있던 컬럼들. 어떤 모델로 만든 임베딩인지
# 남기지 않으면, 모델을 바꿨을 때 어느 문서를 재색인해야 하는지 알 수 없다.
def test_callback_carries_analysis_metadata(monkeypatch, stub_pipeline_io, captured_callbacks):
    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})

    analysis_service.process_analysis(REQUEST, repository=FakeVectorRepository())

    payload = captured_callbacks[0][1]
    assert payload["embeddingModel"]
    assert payload["embeddingDimension"] == 1536
    assert payload["rawResultJson"] is not None


# summary는 현재 프론트에 노출되는 유일한 분석 텍스트다.
# "청크 268개" 같은 내부 수치는 사용자에게 의미가 없다.
def test_summary_names_coverages_not_internal_counts(monkeypatch, stub_pipeline_io, captured_callbacks):
    monkeypatch.setattr(analysis_service, "embed_chunks", lambda chunks: {})
    items = [
        CoverageItem(title=f"담보{i}", category="baggage", isCovered=True, coverageStatus="COVERED")
        for i in range(5)
    ]
    monkeypatch.setattr(analysis_service, "extract_all", lambda chunks, **kwargs: (items, []))

    analysis_service.process_analysis(REQUEST, repository=FakeVectorRepository())

    summary = captured_callbacks[0][1]["summary"]
    assert "담보0" in summary and "외 2건" in summary
    assert "청크" not in summary
