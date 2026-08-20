from dataclasses import dataclass
from typing import Protocol


# 분석 요청 1건이 갖는 스코프. AnalysisStartRequest에서 그대로 넘어와 청크에 실린다.
# pgvector에서는 policy_chunks의 컬럼이 되고, 검색 시 "이 약관만" 필터링하는 조건이 된다.
#
# policy_id가 optional인 이유: 백엔드에 policies 테이블 행을 만드는 코드가 없어
# 실제로는 항상 null이 온다. DDL도 nullable이다. 그래서 검색 필터로 쓸 수 없고,
# NOT NULL인 document_id를 스코프 키로 쓴다.
@dataclass(frozen=True)
class ChunkScope:
    user_id: str
    trip_id: str
    document_id: str
    policy_id: str | None = None

    def as_fields(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "trip_id": self.trip_id,
            "policy_id": self.policy_id,
            "document_id": self.document_id,
        }


# 검색 범위. 어느 약관을 뒤질지 지정한다.
#
# document_id 하나면 그 약관만, trip_id면 그 여행에 딸린 약관 전부를 본다.
# 둘 다 받아두는 이유는 제품 방향이 아직 안 정해졌기 때문이다. 여행자보험은 보통
# 하나만 가입하므로 document_id가 기본이지만, 여러 개 가입한 사용자를 다루기로
# 하면 trip_id로 바꾸면 되고 그때 코드를 고칠 필요가 없다.
#
# clause_paths는 증권에서 온 선택적 필터다.
#
# 증권에 적힌 담보명("해외여행중 휴대품손해(분실제외)")은 같은 상품의 약관 특약명과
# 어휘가 일치하므로, clause_path로 바로 좁힐 수 있다. 8개 표준 카테고리로 번역해
# 좁히는 것보다 훨씬 세밀하다 - db_travel 기준 medical_expense는 76청크지만
# "비급여 도수치료…실손의료비 특별약관"은 7청크다. 증권에 그 담보만 있는 사용자에게
# 나머지 69청크는 방해물이다.
#
# 증권이 없으면 None이고, 그때는 이 필터가 아예 붙지 않아 기존과 동일하게 동작한다.
# 프로즌 데이터클래스라 리스트 대신 튜플을 쓴다.
@dataclass(frozen=True)
class SearchScope:
    document_id: str | None = None
    trip_id: str | None = None
    clause_paths: tuple[str, ...] | None = None

    def is_empty(self) -> bool:
        """약관 범위(문서/여행)가 비었는지. clause_paths는 그 안에서 더 좁히는 조건이라 세지 않는다."""
        return not self.document_id and not self.trip_id

    def has_clause_filter(self) -> bool:
        return bool(self.clause_paths)


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

    # policy_chunks 컬럼.
    # chunk_index는 면책 페어링을 조회 시점에 재계산할 때 쓴다
    # (DDL에 related_chunk_id 컬럼이 없어 인접 인덱스로 짝을 찾는다). pgvector 전용이다.
    #
    # clause_path는 두 저장소가 모두 채운다. 특약 필터의 폴백 판정에 쓰이는데,
    # 한쪽만 채우면 평가(파일 저장소)와 서비스(pgvector)가 다르게 동작한다.
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
        scope: "SearchScope | None" = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        """질문 벡터와 가장 가까운 청크를 반환한다. scope가 주어지면 해당 약관으로 제한한다."""
        ...

    def search_text(
        self,
        query: str,
        scope: "SearchScope | None" = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        """키워드 기반 검색. 임베딩이 놓치는 고유 표현("구조송환비용" 등)을 잡는다.

        pgvector 구현체에서는 PostgreSQL 전문검색(tsvector)으로 대체된다.
        """
        ...

    def get_by_ids(self, chunk_ids: list[str]) -> list[ChunkHit]:
        """chunk_id로 직접 조회한다. 보상 조항에 딸린 면책 조항(related_chunk_id)을 끌어올 때 쓴다."""
        ...
