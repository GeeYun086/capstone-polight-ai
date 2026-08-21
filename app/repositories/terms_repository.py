"""공용 약관을 policy_terms 계열 테이블에 적재한다.

증권 분석(process_analysis)과 달리 이건 배치성 이관이다. 사용자가 올리는 게 아니라
우리가 준비한 약관 8건을 미리 넣어 모두가 공유한다(BACKEND_REPLY_2 1-7).

권한: rag_service에 8개 테이블 SELECT/INSERT/DELETE. UPDATE는 없다 - 재적재는
DELETE 후 INSERT로 한다(PR #36 5절). 그래서 UPSERT가 아니라 지우고 다시 넣는다.

FK 삭제 순서가 강제된다:
    자식 4종 + sources -> policy_terms_coverages -> policy_terms_chunks -> policy_terms
"""

import logging
from uuid import UUID

import psycopg2
from pgvector.psycopg2 import register_vector
from psycopg2.extras import execute_values, register_uuid

register_uuid()

from app.repositories import terms_mapper

logger = logging.getLogger(__name__)


_CHUNK_INSERT = f"""
INSERT INTO policy_terms_chunks ({", ".join(terms_mapper.CHUNK_COLUMNS)})
VALUES %s
"""

_COVERAGE_INSERT = """
INSERT INTO policy_terms_coverages
    (id, terms_id, title, subtitle, category, limit_label, conditions, sort_order, created_at, updated_at)
VALUES %s
"""

_DETAIL_INSERT = """
INSERT INTO coverage_detail_items (id, terms_coverage_id, title, subtitle, is_covered, sort_order)
VALUES %s
"""

_SUBLIMIT_INSERT = """
INSERT INTO sub_coverage_limits
    (id, terms_coverage_id, label, value, limit_amount, limit_currency, description, sort_order)
VALUES %s
"""

_DOC_INSERT = """
INSERT INTO required_documents (id, terms_coverage_id, document_name, is_mandatory, sort_order)
VALUES %s
"""

_EXCLUSION_INSERT = """
INSERT INTO exclusion_conditions
    (id, terms_coverage_id, title, description, source_text, severity, sort_order, created_at, updated_at)
VALUES %s
"""

# 재적재 시 기존 약관 트리를 지운다. FK 때문에 자식부터.
# 규칙의 자식(sources·4종)은 terms_coverage_id로 매이므로, 이 약관에 속한 규칙 id를
# 먼저 골라 지운다.
_DELETE_SQL = """
DELETE FROM policy_terms_coverage_sources
 WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
DELETE FROM coverage_detail_items
 WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
DELETE FROM sub_coverage_limits
 WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
DELETE FROM required_documents
 WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
DELETE FROM exclusion_conditions
 WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
DELETE FROM policy_terms_coverages WHERE terms_id = %(tid)s;
DELETE FROM policy_terms_chunks    WHERE terms_id = %(tid)s;
DELETE FROM policy_terms           WHERE id = %(tid)s;
"""


class TermsRepository:
    """공용 약관 적재 전용. 검색은 PgVectorRepository가 따로 맡는다."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self):
        conn = psycopg2.connect(self._dsn)
        register_vector(conn)
        return conn

    def save_terms(
        self,
        terms_row: dict,
        chunk_rows: list[tuple],
        coverage_tree: dict | None = None,
    ) -> None:
        """약관 한 건을 통째로 적재한다. 이미 있으면 지우고 다시 넣는다.

        하나의 트랜잭션으로 처리한다 - 중간에 실패하면 청크만 들어가고 규칙은
        빠진 어정쩡한 상태가 남으면 안 된다.
        """
        terms_id = terms_row["id"]
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    # 1. 기존 트리 삭제 (재적재 안전)
                    cur.execute(_DELETE_SQL, {"tid": terms_id})

                    # 2. policy_terms
                    cols = list(terms_row.keys())
                    cur.execute(
                        f"INSERT INTO policy_terms ({', '.join(cols)}) VALUES ({', '.join('%s' for _ in cols)})",
                        [terms_row[c] for c in cols],
                    )

                    # 3. policy_terms_chunks
                    if chunk_rows:
                        execute_values(cur, _CHUNK_INSERT, chunk_rows)

                    # 4. 보장 규칙 + 자식 (있을 때만)
                    if coverage_tree:
                        self._insert_coverage_tree(cur, coverage_tree)

            logger.info(
                "약관 적재 완료: terms_id=%s, 청크 %d, 규칙 %d",
                terms_id, len(chunk_rows),
                len(coverage_tree["coverages"]) if coverage_tree else 0,
            )
        finally:
            conn.close()

    @staticmethod
    def _insert_coverage_tree(cur, tree: dict) -> None:
        if tree["coverages"]:
            execute_values(cur, _COVERAGE_INSERT, tree["coverages"])
        if tree["detail_items"]:
            execute_values(cur, _DETAIL_INSERT, tree["detail_items"])
        if tree["sub_limits"]:
            execute_values(cur, _SUBLIMIT_INSERT, tree["sub_limits"])
        if tree["required_documents"]:
            execute_values(cur, _DOC_INSERT, tree["required_documents"])
        if tree["exclusions"]:
            execute_values(cur, _EXCLUSION_INSERT, tree["exclusions"])

    # 규칙만 교체한다. 청크(policy_terms_chunks)는 건드리지 않는다.
    #
    # 약관 이관(migrate_terms_to_db)이 청크를 넣고, 규칙 적재(migrate_terms_coverages)가
    # 나중에 규칙을 넣는다. 규칙 적재를 다시 돌려도 청크를 재삽입하지 않도록 분리했다.
    # LLM 추출이라 재실행이 잦은데 청크까지 매번 지우고 넣으면 낭비다.
    _DELETE_COVERAGES = """
    DELETE FROM policy_terms_coverage_sources
     WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
    DELETE FROM coverage_detail_items
     WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
    DELETE FROM sub_coverage_limits
     WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
    DELETE FROM required_documents
     WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
    DELETE FROM exclusion_conditions
     WHERE terms_coverage_id IN (SELECT id FROM policy_terms_coverages WHERE terms_id = %(tid)s);
    DELETE FROM policy_terms_coverages WHERE terms_id = %(tid)s;
    """

    def save_coverages(self, terms_id: UUID, coverage_tree: dict) -> None:
        """약관 보장 규칙만 교체한다(청크는 그대로).

        재적재 안전: 기존 규칙 트리를 지우고 다시 넣는다. 자식부터 삭제해야
        FK 위반이 안 난다.
        """
        conn = self._connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(self._DELETE_COVERAGES, {"tid": terms_id})
                if coverage_tree:
                    self._insert_coverage_tree(cur, coverage_tree)
            logger.info(
                "보장 규칙 적재: terms_id=%s, 규칙 %d건",
                terms_id, len(coverage_tree["coverages"]) if coverage_tree else 0,
            )
        finally:
            conn.close()

    def find_verified_terms_id(self, insurer_name: str, product_name: str, revision: str | None) -> UUID | None:
        """이미 적재된 VERIFIED 약관의 id를 찾는다. 없으면 None.

        재적재 때 새 UUID를 만들면 (insurer, product, revision) 부분 유니크에 걸려
        INSERT가 실패한다. 기존 id를 찾아 그 id로 DELETE 후 INSERT 하면 안전하다.
        NULL revision은 ''로 접어 비교한다(인덱스와 같은 규칙).
        """
        conn = self._connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM policy_terms
                     WHERE insurer_name = %s AND product_name = %s
                       AND COALESCE(revision, '') = COALESCE(%s, '')
                       AND verification_status = 'VERIFIED'
                    """,
                    (insurer_name, product_name, revision),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def delete_terms(self, terms_id: UUID) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(_DELETE_SQL, {"tid": terms_id})
        finally:
            conn.close()
