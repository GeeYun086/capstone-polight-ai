"""청크 -> policy_chunks 행 변환 테스트.

DB 없이 확정 DDL과의 어긋남을 잡는다. 실제 INSERT는 로컬 pgvector가 준비되면
따로 검증하지만, 아래 항목들은 DB 없이도 확정할 수 있고 틀리면 INSERT가 실패한다.
"""

from datetime import datetime, timezone
from uuid import UUID

from app.repositories.base import ChunkScope
from app.repositories.pg_mapper import COLUMNS, attach_ids, row_to_dict, to_rows

SCOPE = ChunkScope(
    user_id="11111111-1111-1111-1111-111111111111",
    trip_id="22222222-2222-2222-2222-222222222222",
    policy_id="33333333-3333-3333-3333-333333333333",
    document_id="44444444-4444-4444-4444-444444444444",
)


def make_chunk(chunk_id: str, **overrides) -> dict:
    chunk = {
        "chunk_id": chunk_id,
        "source_file": "db_travel.pdf",
        "page_start": 12,
        "page_end": 13,
        "section_title": "제1조(보상하는 손해)",
        "clause_path": "해외여행중 휴대품손해 특별약관",
        "source_content_type": "paragraph",
        "coverage_type": "included",
        "text": "회사는 보상합니다.",
        "char_count": 10,
        "matched_category": "baggage",
        "related_chunk_id": None,
    }
    chunk.update(overrides)
    return chunk


def build(chunks, embeddings=None):
    return to_rows(
        chunks,
        embeddings or {},
        analysis_result_id="55555555-5555-5555-5555-555555555555",
        scope=SCOPE,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# chunk_id는 "db_travel_0249_2"처럼 분할 접미사가 붙은 문자열인데
# DDL은 정수 chunk_index에 UNIQUE(analysis_result_id, chunk_index)를 건다.
def test_chunk_index_is_sequential_integer():
    rows, _ = build([make_chunk("db_travel_0001"), make_chunk("db_travel_0249_2"), make_chunk("db_travel_0250")])

    indexes = [row_to_dict(r)["chunk_index"] for r in rows]

    assert indexes == [0, 1, 2]
    assert all(isinstance(i, int) for i in indexes)


# UNIQUE 제약이 걸린 컬럼이라 중복이 나오면 INSERT가 실패한다.
def test_chunk_index_is_unique():
    rows, _ = build([make_chunk(f"c{i}") for i in range(50)])

    indexes = [row_to_dict(r)["chunk_index"] for r in rows]

    assert len(indexes) == len(set(indexes))


# Python이 policy_chunks를 직접 INSERT하는데 DDL에 id DEFAULT가 없다.
# 그리고 이 UUID를 콜백의 sources[].chunkId로 실어야 Spring이
# coverage_item_sources를 채울 수 있으므로, 저장 전에 알고 있어야 한다.
def test_uuid_is_generated_and_returned():
    chunks = [make_chunk("a"), make_chunk("b")]

    rows, id_map = build(chunks)

    assert set(id_map) == {"a", "b"}
    assert all(isinstance(v, UUID) for v in id_map.values())
    assert len({row_to_dict(r)["id"] for r in rows}) == 2, "UUID가 중복 생성됐다"


def test_attach_ids_exposes_uuid_for_callback():
    chunks = [make_chunk("a")]
    _, id_map = build(chunks)

    enriched = attach_ids(chunks, id_map)

    assert enriched[0]["policy_chunk_id"] == str(id_map["a"])


# 필드명이 DDL과 다르다. 매핑이 틀리면 컬럼이 비거나 INSERT가 실패한다.
def test_field_names_are_mapped_to_ddl():
    rows, _ = build([make_chunk("a")])
    row = row_to_dict(rows[0])

    assert row["content"] == "회사는 보상합니다."      # text
    assert row["clause_type"] == "included"           # coverage_type
    assert row["coverage_category"] == "baggage"      # matched_category
    assert row["clause_path"] == "해외여행중 휴대품손해 특별약관"
    assert row["source_content_type"] == "paragraph"


# DDL에 DEFAULT가 없는 NOT NULL 컬럼들.
def test_not_null_columns_are_filled():
    rows, _ = build([make_chunk("a", clause_path="", matched_category=None)])
    row = row_to_dict(rows[0])

    for column in ("id", "analysis_result_id", "user_id", "document_id",
                   "chunk_index", "source_content_type", "clause_type",
                   "content", "char_count", "created_at", "updated_at"):
        assert row[column] is not None, f"{column}이 비어 있다 (NOT NULL 위반)"


# source_content_type은 NOT NULL인데 pymupdf 경로 청크에는 없다.
def test_missing_source_content_type_falls_back():
    chunk = make_chunk("a")
    del chunk["source_content_type"]

    rows, _ = build([chunk])

    assert row_to_dict(rows[0])["source_content_type"] == "paragraph"


# VARCHAR 길이를 넘으면 INSERT가 실패하므로 미리 자른다.
def test_long_values_are_truncated_to_ddl_limits():
    rows, _ = build([make_chunk("a", section_title="가" * 900, clause_path="나" * 500)])
    row = row_to_dict(rows[0])

    assert len(row["section_title"]) == 500
    assert len(row["clause_path"]) == 300


def test_embedding_is_attached_by_chunk_id():
    rows, _ = build([make_chunk("a"), make_chunk("b")], embeddings={"a": [0.1, 0.2]})
    rows_by_index = [row_to_dict(r) for r in rows]

    assert rows_by_index[0]["embedding"] == [0.1, 0.2]
    assert rows_by_index[1]["embedding"] is None, "임베딩이 없으면 null이어야 한다"


def test_row_tuple_matches_column_order():
    rows, _ = build([make_chunk("a")])

    assert len(rows[0]) == len(COLUMNS), "행 길이와 컬럼 수가 어긋나면 INSERT가 밀린다"
