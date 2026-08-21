"""약관 관련 dict/객체를 공용 약관 테이블 행으로 변환한다.

DB 접속 없이 검증할 수 있도록 순수 함수로 분리했다(pg_mapper와 같은 규칙).
백엔드 PR #36 스키마에 맞춘다 - docs/BACKEND_REPLY_4.md 참고.

  policy_terms                    약관 한 건
  policy_terms_chunks             본문 청크 (policy_chunks에서 주인 컬럼 빼고 terms_id)
  policy_terms_coverages          약관상 보장 규칙
  자식 4종 + policy_terms_coverage_sources
"""

from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from app.schemas import db_enums

# ── policy_terms_chunks ─────────────────────────────────────
# policy_chunks에서 주인 컬럼(analysis_result_id/user_id/trip_id/policy_id/document_id)을
# 빼고 terms_id를 넣은 형태. 나머지는 같다.
CHUNK_COLUMNS = (
    "id",
    "terms_id",
    "chunk_index",
    "source_content_type",
    "clause_type",
    "content",
    "char_count",
    "page_start",
    "page_end",
    "section_title",
    "clause_path",
    "coverage_category",
    "summary",
    "embedding",
    "created_at",
    "updated_at",
)

MAX_LENGTHS = {
    "source_content_type": 30,
    "clause_type": 30,
    "section_title": 500,
    "clause_path": 300,
    "coverage_category": 100,
    # policy_terms_coverages
    "title": 200,
    "subtitle": 500,
    "category": 100,
    "limit_label": 100,
    "limit_currency": 10,
    # 자식
    "document_name": 200,
    "sub_limit_label": 100,
    "sub_limit_value": 200,
    "description": 500,
}


def _cut(value: str | None, column: str) -> str | None:
    if value is None:
        return None
    limit = MAX_LENGTHS.get(column)
    return value[:limit] if limit else value


def terms_row(
    terms_id: UUID,
    insurer_name: str,
    product_name: str,
    revision: str | None,
    effective_date: date | None,
    verification_status: str = "VERIFIED",
    source: str = "OFFICIAL",
    owner_user_id: UUID | None = None,
    file_hash: str | None = None,
    now: datetime | None = None,
) -> dict:
    """policy_terms 1행. 공용 약관은 VERIFIED/OFFICIAL/owner=NULL."""
    now = now or datetime.now(timezone.utc)
    return {
        "id": terms_id,
        "insurer_name": _cut(insurer_name, "title"),  # 200
        "product_name": _cut(product_name, "title"),
        "revision": revision,
        "effective_date": effective_date,
        "verification_status": verification_status,
        "source": source,
        "owner_user_id": owner_user_id,
        "file_hash": file_hash,
        "created_at": now,
        "updated_at": now,
    }


def chunk_rows(
    terms_id: UUID,
    chunks: list[dict],
    embeddings: dict[str, list[float]],
    now: datetime | None = None,
) -> list[tuple]:
    """청크 목록을 policy_terms_chunks 행으로. chunk_index는 입력 순서로 0부터.

    임베딩이 없는 청크도 넣는다(embedding NULL). 검색에서 걸러지지만, 청크 자체는
    근거 인용(sources)의 대상이 될 수 있어 남겨야 한다.
    """
    now = now or datetime.now(timezone.utc)
    rows: list[tuple] = []
    for index, chunk in enumerate(chunks):
        rows.append(
            (
                uuid4(),
                terms_id,
                index,
                _cut(db_enums.source_content_type(chunk.get("source_content_type")), "source_content_type"),
                _cut(db_enums.clause_type(chunk.get("coverage_type")), "clause_type"),
                chunk.get("text") or "",
                chunk.get("char_count") or len(chunk.get("text") or ""),
                chunk.get("page_start"),
                chunk.get("page_end"),
                _cut(chunk.get("section_title"), "section_title"),
                _cut(chunk.get("clause_path"), "clause_path"),
                _cut(chunk.get("matched_category"), "coverage_category"),
                chunk.get("summary"),
                embeddings.get(chunk["chunk_id"]),
                now,
                now,
            )
        )
    return rows


# ── policy_terms_coverages + 자식 4종 ───────────────────────


def coverage_rows(
    terms_id: UUID,
    items: list,
    now: datetime | None = None,
) -> dict:
    """CoverageItem 목록을 규칙+자식 행들로 변환한다.

    반환: {"coverages": [...], "detail_items": [...], "sub_limits": [...],
           "required_documents": [...], "exclusions": [...]}
    각 자식은 terms_coverage_id로 부모에 매인다.

    title은 약관 인쇄명 그대로 넣는다(수식 없이). 백엔드가 이 값으로 증권 담보를
    매칭한다 - PR #36 6-2. limit_amount(정수)는 넣지 않는다. 약관 예시값이라
    limit_label에 원문만 남긴다.
    """
    now = now or datetime.now(timezone.utc)
    out = {
        "coverages": [],
        "detail_items": [],
        "sub_limits": [],
        "required_documents": [],
        "exclusions": [],
    }

    for sort_order, item in enumerate(items):
        cov_id = uuid4()
        out["coverages"].append(
            (
                cov_id,
                terms_id,
                _cut(item.title, "title"),
                _cut(item.subtitle, "subtitle"),
                _cut(item.category, "category"),
                _cut(item.limit_label, "limit_label"),
                item.conditions,  # TEXT, 제한 없음
                sort_order,
                now,
                now,
            )
        )

        for i, d in enumerate(item.detail_items):
            out["detail_items"].append(
                (uuid4(), cov_id, _cut(d.title, "title"), _cut(d.subtitle, "subtitle"), d.is_covered, i)
            )
        for i, s in enumerate(item.sub_limits):
            out["sub_limits"].append(
                (
                    uuid4(), cov_id,
                    _cut(s.label, "sub_limit_label"),
                    _cut(s.value, "sub_limit_value"),
                    s.limit_amount,
                    _cut(s.limit_currency, "limit_currency"),
                    _cut(s.description, "description"),
                    i,
                )
            )
        for i, r in enumerate(item.required_documents):
            out["required_documents"].append(
                (uuid4(), cov_id, _cut(r.document_name, "document_name"), r.is_mandatory, i)
            )
        for i, e in enumerate(item.exclusions):
            out["exclusions"].append(
                (
                    uuid4(), cov_id,
                    _cut(e.title, "title"),
                    e.description,   # TEXT
                    e.source_text,   # TEXT
                    db_enums.severity(e.severity),
                    i,
                    now,
                    now,
                )
            )

    return out
