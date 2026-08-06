from dataclasses import dataclass
from typing import Protocol


# 분석 요청 1건이 갖는 스코프. AnalysisStartRequest에서 그대로 넘어와 청크에 실린다.
# pgvector 연결 후에는 policy_chunks 테이블의 컬럼이 되고, 검색 시 "이 계약의 약관만"
# 필터링하는 조건으로 쓰인다. 이 4개 필드 목록이 Spring에 넘길 DDL 요구사항이다.
@dataclass(frozen=True)
class ChunkScope:
    user_id: str
    trip_id: str
    policy_id: str
    document_id: str

    def as_fields(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "trip_id": self.trip_id,
            "policy_id": self.policy_id,
            "document_id": self.document_id,
        }


# 검색 결과 1건. 저장소가 파일이든 pgvector든 OpenSearch든 이 형태로 반환한다.
# 이 위쪽 코드(rag_service, 라우터)는 저장소가 무엇인지 알지 못한다.
@dataclass
class ChunkHit:
    chunk_id: str
    document_id: str
    page_start: int
    page_end: int
    section_title: str
    coverage_type: str
    text: str
    matched_category: str | None
    related_chunk_id: str | None
    score: float


# DB 경계선. 파이프라인에서 저장소를 만지는 단계는 save(색인)와 search/get_by_ids(질의)뿐이고,
# 전부 이 인터페이스 안에 격리된다. 구현체를 갈아끼워도 호출하는 쪽 코드는 바뀌지 않는다.
#   지금  : FileVectorRepository  (data/chunks + data/embeddings JSON + numpy 코사인)
#   이후  : PgVectorRepository    (policy_chunks INSERT + embedding <=> 검색)
#   향후  : OpenSearchRepository
class VectorRepository(Protocol):
    def save(self, chunks: list[dict], embeddings: dict[str, list[float]]) -> None:
        """청크와 임베딩을 저장한다. chunk_id가 저장 단위의 키."""
        ...

    def search(
        self,
        query_vector: list[float],
        policy_id: str | None = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        """질문 벡터와 가장 가까운 청크를 반환한다. policy_id가 주어지면 해당 계약으로 제한한다."""
        ...

    def get_by_ids(self, chunk_ids: list[str]) -> list[ChunkHit]:
        """chunk_id로 직접 조회한다. 보상 조항에 딸린 면책 조항(related_chunk_id)을 끌어올 때 쓴다."""
        ...
