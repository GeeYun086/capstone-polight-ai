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

    # MMR 재순위에 필요한 정규화된 임베딩. 청크 간 유사도를 계산해야 하므로
    # 점수(질문과의 유사도)만으로는 부족하다. 저장소가 채워주며, 재순위를 쓰지 않는
    # 경로(get_by_ids 등)에서는 None으로 둔다.
    embedding: list[float] | None = None

    # policy_chunks 컬럼. 파일 저장소에는 없고 pgvector 저장소에서만 채워진다.
    # chunk_index는 면책 페어링을 조회 시점에 재계산할 때 쓴다
    # (DDL에 related_chunk_id 컬럼이 없어 인접 인덱스로 짝을 찾는다).
    chunk_index: int | None = None
    clause_path: str | None = None


# DB 경계선. 파이프라인에서 저장소를 만지는 단계는 save(색인)와 search/get_by_ids(질의)뿐이고,
# 전부 이 인터페이스 안에 격리된다. 구현체를 갈아끼워도 호출하는 쪽 코드는 바뀌지 않는다.
#   지금  : FileVectorRepository  (data/chunks + data/embeddings JSON + numpy 코사인)
#   이후  : PgVectorRepository    (policy_chunks INSERT + embedding <=> 검색)
#   향후  : OpenSearchRepository
class VectorRepository(Protocol):
    def save(
        self,
        chunks: list[dict],
        embeddings: dict[str, list[float]],
        analysis_result_id: str | None = None,
        scope: "ChunkScope | None" = None,
    ) -> None:
        """청크와 임베딩을 저장한다.

        analysis_result_id와 scope는 policy_chunks의 NOT NULL 컬럼이자 FK라
        저장 시점에 반드시 필요하다. 파일 저장소는 파일명만으로 구분되므로 무시한다.
        """
        ...

    def search(
        self,
        query_vector: list[float],
        policy_id: str | None = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        """질문 벡터와 가장 가까운 청크를 반환한다. policy_id가 주어지면 해당 계약으로 제한한다."""
        ...

    def search_text(
        self,
        query: str,
        policy_id: str | None = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        """키워드 기반 검색. 임베딩이 놓치는 고유 표현("구조송환비용" 등)을 잡는다.

        pgvector 구현체에서는 PostgreSQL 전문검색(tsvector)으로 대체된다.
        """
        ...

    def get_by_ids(self, chunk_ids: list[str]) -> list[ChunkHit]:
        """chunk_id로 직접 조회한다. 보상 조항에 딸린 면책 조항(related_chunk_id)을 끌어올 때 쓴다."""
        ...
