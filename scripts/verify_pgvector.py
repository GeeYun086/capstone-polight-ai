"""로컬 pgvector에 실제로 넣고 검색해 확정 DDL과의 정합성을 검증한다.

파일 저장소로만 개발하다가 백엔드 DB가 나오는 날 처음 INSERT하면, 그때 제약 위반이
터져 일정이 밀린다. 같은 DDL로 미리 돌려보면 그 위험이 사라진다.

    docker compose up -d
    python scripts/verify_pgvector.py

DATABASE_URL이 없으면 로컬 컨테이너 기본값(rag_service 계정)을 쓴다.
운영과 같은 제한 권한으로 도는지까지 확인하기 위해서다.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.repositories.base import ChunkScope, SearchScope  # noqa: E402
from app.repositories.file_repository import FileVectorRepository  # noqa: E402
from app.repositories.pg_repository import PgVectorRepository  # noqa: E402
from app.schemas import db_enums  # noqa: E402
from app.services.embedding_service import embed_query  # noqa: E402

# 로컬 컨테이너 기본값. 운영 권한 구성을 재현한 제한 계정이다.
LOCAL_DSN = "postgresql://rag_service:rag_local_pw@localhost:5432/polight"

# postgres 계정. rag_service는 policy_chunks 밖의 테이블에 INSERT할 수 없으므로,
# FK 때문에 필요한 상위 행(users/trips/policies/...)은 이 계정으로 넣는다.
# 운영에서는 Spring이 만드는 행들이다.
ADMIN_DSN = "postgresql://postgres:postgres@localhost:5432/polight"

CHUNKS_PATH = PROJECT_ROOT / "data" / "chunks" / "db_travel_chunks.json"
EMBEDDINGS_PATH = PROJECT_ROOT / "data" / "embeddings" / "db_travel_embeddings.json"


def seed_parents(dsn: str, scope: ChunkScope, analysis_result_id: str) -> None:
    """policy_chunks의 FK가 참조하는 상위 행을 만든다 (운영에서는 Spring 몫)."""
    now = datetime.now(timezone.utc)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id,name,provider,provider_id,created_at,updated_at)"
            " VALUES (%s,'테스트','local',%s,%s,%s) ON CONFLICT DO NOTHING",
            (scope.user_id, scope.user_id, now, now),
        )
        cur.execute(
            "INSERT INTO trips (id,user_id,title,country_code,country_name,start_date,end_date,"
            "status,created_at,updated_at)"
            " VALUES (%s,%s,'테스트 여행','TH','태국','2026-08-01','2026-08-10','PLANNED',%s,%s)"
            " ON CONFLICT DO NOTHING",
            (scope.trip_id, scope.user_id, now, now),
        )
        # policies 테이블은 백엔드가 없앴다(V12). 채울 대상이 없어 늘 null이던 것을
        # 정리한 것이다. policy_chunks.policy_id 컬럼은 남아 있고(FK만 제거됨) nullable
        # 이라, scope.policy_id를 그대로 null로 넘기면 저장에 문제가 없다.
        cur.execute(
            "INSERT INTO policy_documents (id,user_id,trip_id,policy_id,original_filename,"
            "stored_file_path,parse_status,uploaded_at,created_at,updated_at)"
            " VALUES (%s,%s,%s,%s,'db_travel.pdf','/local/db_travel.pdf','DONE',%s,%s,%s)"
            " ON CONFLICT DO NOTHING",
            (scope.document_id, scope.user_id, scope.trip_id, scope.policy_id, now, now, now),
        )
        cur.execute(
            "INSERT INTO analysis_results (id,document_id,policy_id,status,started_at,"
            "embedding_model,embedding_dimension,created_at,updated_at)"
            " VALUES (%s,%s,%s,'PROCESSING',%s,%s,1536,%s,%s) ON CONFLICT DO NOTHING",
            (
                analysis_result_id,
                scope.document_id,
                scope.policy_id,
                now,
                get_settings().embedding_provider,
                now,
                now,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="pgvector 정합성 검증")
    parser.add_argument("--dsn", type=str, default=None, help="생략하면 로컬 rag_service 계정")
    parser.add_argument("--admin-dsn", type=str, default=ADMIN_DSN)
    args = parser.parse_args()

    dsn = args.dsn or get_settings().database_url or LOCAL_DSN

    chunks = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
    embeddings = json.loads(EMBEDDINGS_PATH.read_text(encoding="utf-8"))
    print(f"청크 {len(chunks)}개 / 임베딩 {len(embeddings)}개")

    scope = ChunkScope(
        user_id=str(uuid4()), trip_id=str(uuid4()),
        policy_id=str(uuid4()), document_id=str(uuid4()),
    )
    analysis_result_id = str(uuid4())

    print("\n[1] 상위 행 생성 (FK 충족)")
    seed_parents(args.admin_dsn, scope, analysis_result_id)
    print("  완료")

    print("\n[2] policy_chunks INSERT (rag_service 권한)")
    repo = PgVectorRepository(dsn).bind(analysis_result_id, scope)
    started = time.time()
    repo.save(chunks, embeddings)
    print(f"  {len(chunks)}행, {time.time() - started:.1f}초")

    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(embedding), min(chunk_index), max(chunk_index)"
            " FROM policy_chunks WHERE analysis_result_id = %s",
            (analysis_result_id,),
        )
        total, with_vector, lo, hi = cur.fetchone()
    print(f"  저장 {total}행 / 임베딩 {with_vector}행 / chunk_index {lo}~{hi}")

    print("\n[3] 벡터 검색")
    question = "임신이나 출산으로 인한 치료비도 보상되나요?"
    vector = embed_query(question)
    started = time.time()
    hits = repo.search(vector, scope=SearchScope(document_id=scope.document_id), top_k=5)
    elapsed = (time.time() - started) * 1000
    print(f"  '{question}' -> {len(hits)}건, {elapsed:.0f}ms")
    for i, hit in enumerate(hits, 1):
        print(f"    {i}. {hit.score:.4f} [{hit.coverage_type}] {hit.section_title[:44]}")

    print("\n[4] 키워드 검색 (BM25)")
    hits_text = repo.search_text("휴대품 도난", scope=SearchScope(document_id=scope.document_id), top_k=3)
    for i, hit in enumerate(hits_text, 1):
        print(f"    {i}. {hit.score:.2f} {hit.section_title[:50]}")

    print("\n[5] 면책 페어링 SQL 재계산")
    # 검색 상위권이 우연히 전부 excluded면 짝이 0건으로 나와 검증이 되지 않는다.
    # 파일 저장소가 related_chunk_id로 찾아낸 짝 수와 직접 대조한다.
    file_pairs = sum(1 for c in chunks if c.get("related_chunk_id")) // 2
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM policy_chunks a JOIN policy_chunks b"
            "  ON b.analysis_result_id = a.analysis_result_id"
            " AND b.chunk_index = a.chunk_index + 1"
            " WHERE a.analysis_result_id = %s AND a.clause_type = %s"
            "   AND b.clause_type = %s"
            "   AND a.coverage_category = b.coverage_category",
            (analysis_result_id,
             db_enums.CLAUSE_TYPE["included"],
             db_enums.CLAUSE_TYPE["excluded"]),
        )
        sql_pairs = cur.fetchone()[0]
    match = "일치" if sql_pairs == file_pairs else "불일치"
    print(f"  파일 저장소 {file_pairs}쌍 / SQL 재계산 {sql_pairs}쌍 -> {match}")

    # 검색 결과가 짝의 id를 직접 실어오므로, 그게 채워지는지 본다.
    # 이 값이 비면 rag_service의 면책 동반 조회가 통째로 죽는다.
    hits40 = repo.search(vector, scope=SearchScope(document_id=scope.document_id), top_k=40)
    paired = [h for h in hits40 if h.related_chunk_id]
    print(f"  검색 상위 40건 중 면책 짝을 가진 조항: {len(paired)}건")
    if paired:
        related = repo.get_by_ids([paired[0].related_chunk_id])
        if related:
            print(f"  실제 조회 예: [{related[0].coverage_type}] {related[0].section_title[:44]}")

    print("\n[6] 스코프 필터")
    others = repo.search(vector, scope=SearchScope(document_id=str(uuid4())), top_k=5)
    print(f"  다른 document_id로 검색 -> {len(others)}건 (0이어야 정상)")

    # 저장소를 갈아끼워도 검색 결과가 같아야 인터페이스 교체가 의미를 갖는다.
    # 파일 저장소는 numpy 내적, pgvector는 <=> 연산자로 계산하므로
    # 구현이 다른데도 같은 순위가 나오는지 확인한다.
    print("\n[7] 파일 저장소와 결과 대조")
    file_repo = FileVectorRepository()
    questions = [
        "임신이나 출산으로 인한 치료비도 보상되나요?",
        "항공편이 5시간 이상 지연되면 보상되나요?",
        "보험금을 청구하려면 어떤 서류가 필요한가요?",
    ]
    for text in questions:
        vec = embed_query(text)
        pg_hits = repo.search(vec, scope=SearchScope(document_id=scope.document_id), top_k=5)
        file_hits = file_repo.search(vec, scope=SearchScope(document_id="db_travel"), top_k=5)
        # chunk_id 체계가 달라(UUID 대 문자열) 본문 앞부분으로 비교한다
        pg_keys = [h.text[:60] for h in pg_hits]
        file_keys = [h.text[:60] for h in file_hits]
        same = sum(1 for a, b in zip(pg_keys, file_keys) if a == b)
        print(f"  {text[:34]:36} 상위 5건 중 {same}건 순서까지 동일")

    print("\n검증 완료")


if __name__ == "__main__":
    main()
