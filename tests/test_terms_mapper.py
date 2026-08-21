"""공용 약관 테이블 매퍼 회귀 테스트.

DB 없이 순수 변환만 검증한다. 실제 INSERT는 로컬 pgvector로 따로 확인했다.
백엔드 PR #36 스키마에 맞춘다 - docs/BACKEND_REPLY_4.md.
"""

from datetime import date
from uuid import uuid4

from app.repositories import terms_mapper
from app.schemas.coverage import (
    CoverageItem,
    CoverageDetailItem,
    ExclusionCondition,
    RequiredDocument,
    SubCoverageLimit,
)


def _item(**over):
    base = dict(title="휴대품손해", category="baggage", is_covered=True, coverage_status="COVERED")
    base.update(over)
    return CoverageItem(**base)


# ── policy_terms ────────────────────────────────────────────


# 공용 약관은 VERIFIED/OFFICIAL/owner=NULL. 이 조합이 CHECK를 통과한다.
def test_terms_row_defaults_to_official_verified():
    row = terms_mapper.terms_row(uuid4(), "현대해상", "다이렉트 해외여행보험", "2025-06-30", date(2025, 6, 30))

    assert row["verification_status"] == "VERIFIED"
    assert row["source"] == "OFFICIAL"
    assert row["owner_user_id"] is None
    assert row["created_at"] == row["updated_at"]


# ── 청크 ────────────────────────────────────────────────────


def test_chunk_rows_index_is_sequential():
    chunks = [
        {"chunk_id": "a", "text": "본문1", "coverage_type": "included"},
        {"chunk_id": "b", "text": "본문2", "coverage_type": "excluded"},
    ]
    rows = terms_mapper.chunk_rows(uuid4(), chunks, {"a": [0.1] * 1536, "b": [0.2] * 1536})

    # (id, terms_id, chunk_index, ...) — chunk_index는 세 번째
    assert [r[2] for r in rows] == [0, 1]
    # clause_type 번역: included -> COVERAGE, excluded -> EXCLUSION (네 번째 다음, 5번째)
    assert rows[0][4] == "COVERAGE"
    assert rows[1][4] == "EXCLUSION"


# 임베딩이 없는 청크도 넣는다. 근거 인용 대상이 될 수 있어서다.
def test_chunk_without_embedding_is_kept_with_null():
    chunks = [{"chunk_id": "a", "text": "본문", "coverage_type": "included"}]
    rows = terms_mapper.chunk_rows(uuid4(), chunks, {})  # 임베딩 없음

    assert len(rows) == 1
    assert rows[0][-3] is None  # embedding 자리 (created_at, updated_at 앞)


def test_chunk_clause_path_is_truncated():
    chunks = [{"chunk_id": "a", "text": "x", "coverage_type": "included", "clause_path": "가" * 400}]
    rows = terms_mapper.chunk_rows(uuid4(), chunks, {})

    # clause_path는 열 순서상 11번째(0-based 10)
    assert len(rows[0][10]) == 300


# ── 보장 규칙 ───────────────────────────────────────────────


# title은 매칭 키다. 약관 인쇄명 그대로, 200자로 자른다.
def test_coverage_title_is_cut_to_200():
    tree = terms_mapper.coverage_rows(uuid4(), [_item(title="가" * 300)])

    assert len(tree["coverages"][0][2]) == 200  # (id, terms_id, title, ...)


# 자식이 부모(terms_coverage_id)에 매인다. 같은 규칙의 자식은 같은 부모 id.
def test_children_share_parent_id():
    item = _item(
        detail_items=[CoverageDetailItem(title="세부1", is_covered=True)],
        exclusions=[ExclusionCondition(title="면책1", severity="HIGH")],
        required_documents=[RequiredDocument(document_name="청구서", is_mandatory=True)],
        sub_limits=[SubCoverageLimit(label="한도", value="20만원")],
    )
    tree = terms_mapper.coverage_rows(uuid4(), [item])

    parent_id = tree["coverages"][0][0]
    assert tree["detail_items"][0][1] == parent_id
    assert tree["exclusions"][0][1] == parent_id
    assert tree["required_documents"][0][1] == parent_id
    assert tree["sub_limits"][0][1] == parent_id


# severity 변환: 내부 HIGH/MEDIUM/LOW -> DB GENERAL/WARNING/CRITICAL
def test_exclusion_severity_is_translated():
    item = _item(exclusions=[
        ExclusionCondition(title="a", severity="HIGH"),
        ExclusionCondition(title="b", severity="MEDIUM"),
        ExclusionCondition(title="c", severity="LOW"),
    ])
    tree = terms_mapper.coverage_rows(uuid4(), [item])

    # (id, terms_coverage_id, title, description, source_text, severity, ...) — severity 6번째(idx 5)
    assert [e[5] for e in tree["exclusions"]] == ["CRITICAL", "WARNING", "GENERAL"]


# sort_order가 규칙 순서대로 부여된다. UNIQUE(terms_id, sort_order) 위반을 막는다.
def test_coverage_sort_order_is_sequential():
    tree = terms_mapper.coverage_rows(uuid4(), [_item(title="A"), _item(title="B"), _item(title="C")])

    # (id, terms_id, title, subtitle, category, limit_label, conditions, sort_order, ...)
    assert [c[7] for c in tree["coverages"]] == [0, 1, 2]


# 통화는 VARCHAR(10). limit_label(100)로 잘못 자르면 초과할 수 있다.
def test_sub_limit_currency_uses_own_limit():
    item = _item(sub_limits=[SubCoverageLimit(label="한도", value="5만달러", limit_currency="USD")])
    tree = terms_mapper.coverage_rows(uuid4(), [item])

    # (id, terms_coverage_id, label, value, limit_amount, limit_currency, ...) — currency idx 5
    assert tree["sub_limits"][0][5] == "USD"
