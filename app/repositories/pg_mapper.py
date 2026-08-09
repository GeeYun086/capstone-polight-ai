"""청크 dict를 policy_chunks 행으로 변환한다.

DB 접속 없이 검증할 수 있도록 순수 함수로 분리했다.
확정 DDL과 어긋나는 부분이 여기 모여 있다.

  chunk_id "db_travel_0249_2" (문자열)  ->  chunk_index 250 (정수)
  id                                    ->  Python이 UUID 생성
  text / coverage_type / matched_category -> content / clause_type / coverage_category
  created_at, updated_at                ->  DDL에 DEFAULT가 없어 직접 채움
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.repositories.base import ChunkScope

# policy_chunks의 컬럼 순서. INSERT 문과 행 튜플이 어긋나지 않도록 한 곳에서 관리한다.
COLUMNS = (
    "id",
    "analysis_result_id",
    "user_id",
    "trip_id",
    "policy_id",
    "document_id",
    "chunk_index",
    "source_content_type",
    "page_start",
    "page_end",
    "section_title",
    "clause_path",
    "coverage_category",
    "clause_type",
    "content",
    "summary",
    "embedding",
    "char_count",
    "created_at",
    "updated_at",
)

# DDL의 VARCHAR 길이. 초과하면 INSERT가 실패하므로 미리 자른다.
MAX_LENGTHS = {
    "source_content_type": 30,
    "section_title": 500,
    "clause_path": 300,
    "coverage_category": 100,
    "clause_type": 30,
}


def _truncate(value: str | None, column: str) -> str | None:
    if value is None:
        return None
    limit = MAX_LENGTHS.get(column)
    return value[:limit] if limit else value


# 청크 목록을 policy_chunks 행으로 변환한다.
#
# chunk_index를 새로 매기는 이유: 우리 chunk_id는 "db_travel_0249_2"처럼 분할 접미사가
# 붙은 문자열인데, DDL은 정수 chunk_index에 UNIQUE(analysis_result_id, chunk_index)를 건다.
# 입력 순서가 곧 문서 내 순서이므로 그대로 0부터 번호를 매긴다.
#
# id를 Python이 만드는 이유: policy_chunks INSERT를 Python이 직접 하는데 DDL에 DEFAULT가
# 없다. 그리고 완료 콜백의 sources[].chunkId로 이 값을 그대로 실어야 Spring이
# coverage_item_sources를 채울 수 있다. INSERT 후 회수하는 방식은 순서가 꼬인다.
def to_rows(
    chunks: list[dict],
    embeddings: dict[str, list[float]],
    analysis_result_id: str,
    scope: ChunkScope,
    now: datetime | None = None,
) -> tuple[list[tuple], dict[str, UUID]]:
    """(INSERT용 행 목록, chunk_id -> 생성된 UUID) 를 돌려준다."""
    now = now or datetime.now(timezone.utc)
    rows: list[tuple] = []
    id_map: dict[str, UUID] = {}

    for index, chunk in enumerate(chunks):
        chunk_uuid = uuid4()
        id_map[chunk["chunk_id"]] = chunk_uuid

        rows.append(
            (
                chunk_uuid,
                analysis_result_id,
                scope.user_id,
                scope.trip_id,
                scope.policy_id,
                scope.document_id,
                index,
                _truncate(chunk.get("source_content_type") or "paragraph", "source_content_type"),
                chunk.get("page_start"),
                chunk.get("page_end"),
                _truncate(chunk.get("section_title"), "section_title"),
                _truncate(chunk.get("clause_path") or None, "clause_path"),
                _truncate(chunk.get("matched_category"), "coverage_category"),
                _truncate(chunk.get("coverage_type") or "included", "clause_type"),
                chunk["text"],
                chunk.get("summary"),
                embeddings.get(chunk["chunk_id"]),
                chunk["char_count"],
                now,
                now,
            )
        )

    return rows, id_map


# 저장 후 라우터/콜백이 참조할 수 있도록, 청크에 생성된 UUID를 실어 돌려준다.
def attach_ids(chunks: list[dict], id_map: dict[str, UUID]) -> list[dict]:
    return [{**c, "policy_chunk_id": str(id_map[c["chunk_id"]])} for c in chunks if c["chunk_id"] in id_map]


def row_to_dict(row: tuple[Any, ...]) -> dict:
    return dict(zip(COLUMNS, row))
