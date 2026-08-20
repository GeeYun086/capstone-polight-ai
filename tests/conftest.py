import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories import get_vector_repository
from app.repositories.base import ChunkHit


# 가짜 저장소. VectorRepository Protocol만 만족하면 되므로 DB도 파일도 필요 없다.
# 이 덕분에 아래 테스트들은 pgvector 없이, OPENAI_API_KEY 없이 전부 실행된다.
class FakeVectorRepository:
    def __init__(self, hits: list[ChunkHit] | None = None) -> None:
        self.hits = hits or []
        self.text_hits: list[ChunkHit] = []
        self.saved: list[tuple[list[dict], dict]] = []

    def save(self, chunks, embeddings, analysis_result_id=None, scope=None):
        self.saved.append((chunks, embeddings))
        self.saved_context = (analysis_result_id, scope)

    def search(self, query_vector, scope=None, top_k=8):
        if scope is None or scope.is_empty():
            return self.hits[:top_k]
        return [h for h in self.hits if h.document_id == scope.document_id][:top_k]

    # 키워드 검색은 기본적으로 비워둔다. 비어 있으면 hybrid_search가 벡터 결과를
    # 그대로 쓰므로, 계약 테스트는 검색 방식과 무관하게 유지된다.
    # 융합 동작 자체는 test_hybrid_search.py에서 따로 검증한다.
    def search_text(self, query, scope=None, top_k=8):
        return self.text_hits[:top_k]

    def get_by_ids(self, chunk_ids):
        by_id = {h.chunk_id: h for h in self.hits}
        return [by_id[c] for c in chunk_ids if c in by_id]


def make_hit(
    chunk_id: str,
    coverage_type: str = "included",
    related_chunk_id: str | None = None,
    document_id: str = "doc-1",
    text: str = "약관 본문 예시입니다.",
    section_title: str = "제1조(보상하는 손해)",
) -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk_id,
        document_id=document_id,
        page_start=10,
        page_end=10,
        section_title=section_title,
        coverage_type=coverage_type,
        text=text,
        matched_category="flight_delay",
        related_chunk_id=related_chunk_id,
        score=0.9,
    )


@pytest.fixture
def fake_repo():
    return FakeVectorRepository()


@pytest.fixture
def client(fake_repo):
    app.dependency_overrides[get_vector_repository] = lambda: fake_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
