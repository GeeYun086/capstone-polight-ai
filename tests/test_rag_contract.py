import pytest

from app.services import rag_service
from tests.conftest import make_hit

QUERY_URL = "/internal/rag/query"

VALID_BODY = {
    "userId": "user-1",
    "tripId": "trip-1",
    "policyId": "doc-1",
    "question": "항공편이 5시간 지연되면 보상되나요?",
}


# 임베딩과 LLM 호출만 대체한다. 나머지(검색, 컨텍스트 조립, 출처 생성)는 실제 코드가 돈다.
@pytest.fixture(autouse=True)
def stub_openai(monkeypatch):
    monkeypatch.setattr(rag_service, "embed_query", lambda q, client=None: [0.1] * 8)
    monkeypatch.setattr(
        rag_service, "_call_llm", lambda msg, client=None: "테스트 답변 [근거 1]"
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# Spring과의 JSON 계약: 요청은 camelCase로 받고 응답도 camelCase로 준다.
def test_response_uses_camel_case(client, fake_repo):
    fake_repo.hits = [make_hit("c1")]

    response = client.post(QUERY_URL, json=VALID_BODY)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"answer", "sources"}
    assert set(body["sources"][0]) == {"chunkId", "documentId", "page", "quote"}


def test_missing_required_field_returns_422(client):
    response = client.post(QUERY_URL, json={"question": "질문만 있음"})
    assert response.status_code == 422


# 근거가 없으면 LLM을 호출하지 않고 "확인할 수 없다"고 답해야 한다.
# 근거 없이 답을 만들면 그게 곧 환각이다.
def test_no_evidence_returns_explicit_message(client, fake_repo):
    fake_repo.hits = []

    body = client.post(QUERY_URL, json=VALID_BODY).json()

    assert body["sources"] == []
    assert body["answer"] == rag_service.NO_EVIDENCE_ANSWER


# policy_id로 스코프가 걸려야 한다. 다른 계약의 약관이 섞이면 오답이 된다.
def test_search_is_scoped_by_policy_id(client, fake_repo):
    fake_repo.hits = [
        make_hit("mine", document_id="doc-1"),
        make_hit("other", document_id="doc-2"),
    ]

    body = client.post(QUERY_URL, json=VALID_BODY).json()

    assert [s["chunkId"] for s in body["sources"]] == ["mine"]


# 이 프로젝트 RAG의 핵심: 보장 조항이 검색되면 짝지어진 면책 조항이 근거에 따라와야 한다.
# 따라오지 않으면 LLM이 예외를 모른 채 "보상됩니다"라고 답한다.
def test_related_exclusion_chunk_is_attached(client, fake_repo):
    fake_repo.hits = [
        make_hit("covered", coverage_type="included", related_chunk_id="excluded-1"),
        make_hit("excluded-1", coverage_type="excluded"),
    ]
    # 검색 자체는 보장 조항만 반환하도록 top_k를 1로 만드는 대신,
    # search가 두 건 다 주더라도 중복 추가가 없어야 한다는 것까지 확인한다.
    body = client.post(QUERY_URL, json=VALID_BODY).json()

    chunk_ids = [s["chunkId"] for s in body["sources"]]
    assert "excluded-1" in chunk_ids
    assert len(chunk_ids) == len(set(chunk_ids)), "면책 조항이 중복으로 붙었다"


def test_related_chunk_not_duplicated_when_already_present(fake_repo):
    hits = [
        make_hit("covered", coverage_type="included", related_chunk_id="exc"),
        make_hit("exc", coverage_type="excluded"),
    ]
    fake_repo.hits = hits

    result = rag_service.attach_related_chunks(hits, fake_repo)

    assert len(result) == 2


# 출처 인용은 LLM 출력이 아니라 검색된 원문에서 잘라내야 한다.
# LLM이 인용문을 만들면 원문에 없는 문장이 근거로 제시될 수 있다.
def test_quote_comes_from_source_text_not_llm():
    long_text = "가" * 500
    hit = make_hit("c1", text=long_text)

    sources = rag_service.build_sources([hit])

    assert sources[0].quote.startswith("가")
    assert sources[0].quote.endswith("...")
    assert len(sources[0].quote) == rag_service.QUOTE_MAX_CHARS + 3
    assert sources[0].page == hit.page_start
