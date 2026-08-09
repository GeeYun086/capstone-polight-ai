import logging
from contextlib import contextmanager

import numpy as np
import psycopg2
from pgvector.psycopg2 import register_vector
from psycopg2.extras import execute_values, register_uuid

# psycopg2는 uuid.UUID를 기본으로 어댑트하지 못한다("can't adapt type 'UUID'").
# 모듈 전역 등록이라 한 번만 호출하면 된다.
register_uuid()

from app.repositories.base import ChunkHit, ChunkScope
from app.repositories.pg_mapper import COLUMNS, to_rows
from app.services.bm25 import BM25Index

logger = logging.getLogger(__name__)


def _normalize(vector) -> list[float] | None:
    """읽어온 임베딩을 MMR이 쓸 수 있게 정규화한다.

    register_vector를 쓰면 pgvector가 리스트가 아니라 Vector 객체를 돌려주므로
    numpy로 바꿔야 한다. 그리고 MMR은 정규화된 벡터의 내적을 코사인 유사도로 쓰는데
    pgvector에는 원본이 저장되므로 여기서 정규화한다.
    """
    if vector is None:
        return None

    array = vector.to_numpy() if hasattr(vector, "to_numpy") else np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return (array / norm).tolist() if norm else array.tolist()

# 검색 결과를 ChunkHit으로 만들 때 필요한 컬럼들.
# id를 chunk_id로 쓴다 - 콜백의 sources[].chunkId가 이 UUID여야 Spring이
# coverage_item_sources를 채울 수 있기 때문이다.
# embedding까지 가져오는 이유: MMR 재순위가 청크 간 유사도를 계산하려면 벡터가 필요하다.
# 빠뜨리면 mmr_select가 "임베딩이 없다"며 재순위를 건너뛰어, 반복되는 표준 조항이
# top-k를 잠식하는 문제가 그대로 돌아온다(파일 저장소에서 Recall@8 83%->92% 차이를 만든 부분).
SELECT_FIELDS = """
    c.id, c.document_id, c.chunk_index, c.page_start, c.page_end,
    c.section_title, c.clause_path, c.coverage_category, c.clause_type, c.content,
    c.embedding
"""

# 면책 짝을 함께 붙인다.
#
# DDL에 related_chunk_id 컬럼이 없어 조회 시점에 계산하는데, rag_service는
# hit.related_chunk_id를 보고 면책 조항을 끌어온다. 검색 결과에 짝의 id가 실리지 않으면
# 이 프로젝트의 핵심인 "보장 조항에 딸린 면책 조항 동반 조회"가 통째로 작동하지 않는다.
#
# 규칙은 청킹 때(link_exclusion_pairs)와 같다: included 조항 바로 다음이 같은 카테고리의
# excluded면 짝이다. UNIQUE(analysis_result_id, chunk_index)가 순서를 보장한다.
RELATED_JOIN = """
LEFT JOIN policy_chunks rel
       ON rel.analysis_result_id = c.analysis_result_id
      AND rel.chunk_index = c.chunk_index + 1
      AND c.clause_type = 'included'
      AND rel.clause_type = 'excluded'
      AND rel.coverage_category = c.coverage_category
"""

INSERT_SQL = f"""
INSERT INTO policy_chunks ({", ".join(COLUMNS)})
VALUES %s
ON CONFLICT (analysis_result_id, chunk_index) DO NOTHING
"""

# pgvector의 <=> 는 코사인 거리(0에 가까울수록 유사)라, 유사도로 쓰려면 1에서 뺀다.
SEARCH_SQL = f"""
SELECT {SELECT_FIELDS}, rel.id AS related_id,
       1 - (c.embedding <=> %(vector)s::vector) AS score
FROM policy_chunks c
{RELATED_JOIN}
WHERE c.embedding IS NOT NULL
  {{scope}}
ORDER BY c.embedding <=> %(vector)s::vector
LIMIT %(top_k)s
"""

TEXT_SQL = f"""
SELECT {SELECT_FIELDS}, rel.id AS related_id, 0.0 AS score
FROM policy_chunks c
{RELATED_JOIN}
{{scope}}
"""

# ChunkHit.chunk_id는 문자열이라 UUID 컬럼과 직접 비교되지 않는다
# ("operator does not exist: uuid = text"). 명시적으로 캐스팅한다.
BY_IDS_SQL = f"""
SELECT {SELECT_FIELDS}, rel.id AS related_id, 0.0 AS score
FROM policy_chunks c
{RELATED_JOIN}
WHERE c.id = ANY(%(ids)s::uuid[])
"""


# pgvector 기반 저장소.
#
# FileVectorRepository와 같은 인터페이스를 구현하므로, 이 클래스를 쓰도록 바꿔도
# rag_service와 라우터는 수정할 필요가 없다.
class PgVectorRepository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def _cursor(self, commit: bool = False):
        connection = psycopg2.connect(self._dsn)
        # vector 타입은 확장이 정의한 것이라 연결마다 등록해야 한다.
        # 등록하면 파이썬 리스트를 그대로 넣고 읽을 수 있다.
        register_vector(connection)
        try:
            with connection.cursor() as cursor:
                yield cursor
            if commit:
                connection.commit()
        finally:
            connection.close()

    # ── 색인 ─────────────────────────────────────────────────

    # 검증 스크립트처럼 save()에 컨텍스트를 넘기기 번거로운 경우를 위해 남겨둔다.
    # 애플리케이션 경로에서는 save()의 인자로 직접 전달한다.
    def bind(self, analysis_result_id: str, scope: ChunkScope) -> "PgVectorRepository":
        self._analysis_result_id = analysis_result_id
        self._scope = scope
        return self

    def save(
        self,
        chunks: list[dict],
        embeddings: dict[str, list[float]],
        analysis_result_id: str | None = None,
        scope: ChunkScope | None = None,
    ) -> None:
        if not chunks:
            return

        analysis_result_id = analysis_result_id or getattr(self, "_analysis_result_id", None)
        scope = scope or getattr(self, "_scope", None)
        if not analysis_result_id or scope is None:
            raise RuntimeError(
                "policy_chunks는 analysis_result_id와 스코프가 NOT NULL이라 "
                "save()에 함께 넘기거나 bind()로 미리 지정해야 합니다."
            )

        rows, id_map = to_rows(chunks, embeddings, analysis_result_id, scope)

        with self._cursor(commit=True) as cursor:
            execute_values(cursor, INSERT_SQL, rows, page_size=200)

        # 생성한 UUID를 청크에 직접 실어 돌려준다.
        #
        # 저장소를 인스턴스 변수(last_id_map)에 담아두면 안 된다.
        # get_vector_repository()가 lru_cache라 저장소는 싱글턴이고, 두 건의 분석이
        # 동시에 돌면 나중 것이 앞의 것을 덮어써 콜백에 엉뚱한 UUID가 실린다.
        # 청크는 요청마다 별개의 객체라 이런 문제가 없다.
        for chunk in chunks:
            uuid = id_map.get(chunk["chunk_id"])
            if uuid:
                chunk["policy_chunk_id"] = str(uuid)

        logger.info("policy_chunks 저장 완료: %d행", len(rows))

    # ── 질의 ─────────────────────────────────────────────────

    def search(
        self,
        query_vector: list[float],
        policy_id: str | None = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        scope = "AND c.policy_id = %(policy_id)s" if policy_id else ""
        sql = SEARCH_SQL.format(scope=scope)

        with self._cursor() as cursor:
            cursor.execute(sql, {"vector": query_vector, "policy_id": policy_id, "top_k": top_k})
            rows = cursor.fetchall()

        return [self._to_hit(row) for row in rows]

    # 키워드 검색은 계약 범위의 청크를 읽어 파이썬 BM25로 돌린다.
    #
    # PostgreSQL 전문검색(tsvector)을 쓰지 않는 이유: 한국어 사전이 없으면 조사가 붙은
    # 어절을 분리하지 못해 품질이 크게 떨어진다. 우리는 이미 조사 변화에 강한
    # 접두 n-gram 토크나이저를 만들어 검증했고, 한 계약의 청크는 수백 개 수준이라
    # 메모리에 올려 계산해도 부담이 없다.
    def search_text(
        self,
        query: str,
        policy_id: str | None = None,
        top_k: int = 8,
    ) -> list[ChunkHit]:
        scope = "WHERE c.policy_id = %(policy_id)s" if policy_id else ""
        sql = TEXT_SQL.format(scope=scope)

        with self._cursor() as cursor:
            cursor.execute(sql, {"policy_id": policy_id})
            rows = cursor.fetchall()

        if not rows:
            return []

        hits = [self._to_hit(row) for row in rows]
        # 색인 대상은 파일 저장소와 같아야 한다. 임베딩할 때 쓴 텍스트
        # (특약명 + 조항 제목 + 본문)와 일치시켜야 두 저장소가 같은 결과를 낸다.
        index = BM25Index([f"{h.clause_path or ''}\n{h.section_title}\n{h.text}" for h in hits])
        scores = index.scores(query)

        ranked = sorted(zip(hits, scores), key=lambda pair: -pair[1])
        result = []
        for hit, score in ranked[:top_k]:
            if score <= 0:
                break
            hit.score = float(score)
            result.append(hit)
        return result

    # 면책 페어링. FileVectorRepository는 related_chunk_id를 그대로 읽지만,
    # DDL에 그 컬럼이 없으므로 여기서는 chunk_index 인접성으로 다시 찾는다.
    def get_by_ids(self, chunk_ids: list[str]) -> list[ChunkHit]:
        if not chunk_ids:
            return []

        with self._cursor() as cursor:
            cursor.execute(BY_IDS_SQL, {"ids": list(chunk_ids)})
            return [self._to_hit(row) for row in cursor.fetchall()]


    @staticmethod
    def _to_hit(row: tuple) -> ChunkHit:
        (
            chunk_id,
            document_id,
            chunk_index,
            page_start,
            page_end,
            section_title,
            clause_path,
            coverage_category,
            clause_type,
            content,
            embedding,
            related_id,
            score,
        ) = row

        hit = ChunkHit(
            chunk_id=str(chunk_id),
            document_id=str(document_id),
            page_start=page_start or 0,
            page_end=page_end or 0,
            section_title=section_title or "",
            coverage_type=clause_type,
            text=content,
            matched_category=coverage_category,
            related_chunk_id=str(related_id) if related_id else None,
            score=float(score),
            # MMR은 정규화된 벡터의 내적을 코사인 유사도로 쓴다.
            # pgvector에는 원본 벡터가 저장되므로 읽을 때 정규화한다.
            embedding=_normalize(embedding),
        )
        hit.chunk_index = chunk_index
        hit.clause_path = clause_path
        return hit
