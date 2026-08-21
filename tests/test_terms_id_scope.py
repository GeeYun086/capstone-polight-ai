"""terms_id 스코프 회귀 테스트.

A안(개인 약관도 policy_terms)으로 가면서 검색이 공용 약관(policy_terms_chunks)을
terms_id로 좁히게 된다. 공용 약관은 사용자·문서에 매이지 않아 document_id/trip_id로는
못 찾는다 - terms_id가 유일한 스코프 키다.

여기서는 요청 스키마가 termsId를 받는지, SearchScope가 그것을 우선하는지,
file 저장소 매칭이 terms_id로 좁히는지를 고정한다. pg 저장소의 policy_terms_chunks
전환은 권한 개방 후 별도로 다룬다(docs/BACKEND_REPLY_5.md).
"""

from app.repositories.base import SearchScope
from app.repositories.file_repository import _matches, _scope_key
from app.schemas.rag import RagQueryRequest


# 백엔드가 termsId를 실어 보낸다. 받는 필드가 없으면 무시되고 검색이 약관에 닿지 않는다.
def test_request_accepts_terms_id():
    req = RagQueryRequest.model_validate({
        "userId": "u", "tripId": "t", "question": "q",
        "termsId": "11111111-1111-1111-1111-111111111111",
    })

    assert req.terms_id == "11111111-1111-1111-1111-111111111111"


# null이면 그 여행에 약관이 없다는 뜻. 필드는 optional이라 안 보내도 동작한다.
def test_terms_id_is_optional():
    req = RagQueryRequest.model_validate({"userId": "u", "tripId": "t", "question": "q"})

    assert req.terms_id is None


# terms_id가 있으면 그것으로 좁힌다. document_id/trip_id는 개인 청크용이라 뒤로 밀린다.
def test_terms_id_takes_priority_in_matching():
    scope = SearchScope(terms_id="T", document_id="D", trip_id="TR")

    assert _matches({"terms_id": "T"}, scope) is True
    assert _matches({"terms_id": "other"}, scope) is False
    # terms_id가 안 맞으면 document_id가 맞아도 제외 (공용 약관 스코프가 우선)
    assert _matches({"terms_id": "other", "document_id": "D"}, scope) is False


# terms_id가 없으면 기존 document_id 스코프로 동작한다(하위 호환).
def test_falls_back_to_document_id_without_terms_id():
    scope = SearchScope(document_id="D")

    assert _matches({"document_id": "D"}, scope) is True
    assert _matches({"document_id": "other"}, scope) is False


# 스코프 캐시 키도 terms_id를 우선해야 서로 다른 약관이 같은 키로 섞이지 않는다.
def test_scope_key_prefers_terms_id():
    assert _scope_key(SearchScope(terms_id="T", document_id="D")) == "T"
    assert _scope_key(SearchScope(document_id="D")) == "D"
    assert _scope_key(SearchScope()) is None


# is_empty는 terms_id도 범위로 센다. terms_id만 있는 스코프가 "비었다"로 판정되면
# 필터가 안 붙어 전체 약관이 검색된다.
def test_terms_id_alone_is_not_empty():
    assert SearchScope(terms_id="T").is_empty() is False
    assert SearchScope().is_empty() is True


# ── terms_id 없을 때 검색 건너뛰기 (pgvector 경로) ──────────

def test_no_terms_id_skips_search_on_pg(monkeypatch):
    """약관 못 찾은 여행(terms_id 없음)이면 검색·임베딩을 건너뛰고 근거 없음으로 답한다.

    공용 약관은 terms_id로만 검색되므로, 없으면 임베딩 비용을 헛되이 쓰거나 엉뚱한
    약관을 끌어올 이유가 없다. docs/BACKEND_REPLY_5.md 1-2 약속.
    """
    from app.services import rag_service
    from app.schemas.rag import RagQueryRequest

    # DATABASE_URL이 있는 상황(pgvector)을 흉내낸다
    class S:
        database_url = "postgresql://x"
        top_k = 8
        mmr_candidate_multiplier = 4
    monkeypatch.setattr(rag_service, "get_settings", lambda: S())

    called = {"embed": False}
    monkeypatch.setattr(rag_service, "embed_query",
                        lambda *a, **k: called.__setitem__("embed", True) or [0.0])

    req = RagQueryRequest.model_validate({"userId": "u", "tripId": "t", "question": "q"})  # terms_id 없음
    resp = rag_service.answer_question(req, repository=None)

    assert resp.sources == []
    assert called["embed"] is False, "terms_id 없으면 임베딩도 하지 않아야 한다"
