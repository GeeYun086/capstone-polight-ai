"""이미 색인된 공용 약관을 policy_terms 계열 테이블로 옮긴다.

data/chunks + data/embeddings(파일 저장소)에 있는 약관을 실서버 DB의
policy_terms / policy_terms_chunks 로 적재한다. 파싱·임베딩은 이미 파일에 있으므로
재사용한다(과금 없음).

이 스크립트는 청크만 옮긴다(챗봇 검색용). 보장 규칙(policy_terms_coverages,
보장 상세용)은 LLM 추출이 필요해 migrate_terms_coverages.py 로 따로 돌린다.

실행 위치: 실서버 DB는 RDS(VPC 안)라 로컬에서 못 붙는다. AI 컨테이너 안에서
DATABASE_URL(rag_service DSN)로 실행한다.

    docker exec polight-ai python scripts/migrate_terms_to_db.py            # 전체
    docker exec polight-ai python scripts/migrate_terms_to_db.py --terms hyundai_travel_2025
    docker exec polight-ai python scripts/migrate_terms_to_db.py --dry-run  # 넣지 않고 확인만

재실행은 안전하다. 같은 약관은 (보험사·상품·개정판)으로 기존 id를 찾아 DELETE 후
INSERT 한다. UPDATE 권한 없이 재적재하는 방식이다(PR #36 5절).
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.repositories import terms_mapper  # noqa: E402
from app.repositories.terms_repository import TermsRepository  # noqa: E402

CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
EMBEDDINGS_DIR = PROJECT_ROOT / "data" / "embeddings"
REGISTRY_PATH = PROJECT_ROOT / "config" / "terms_registry.json"


def _registry() -> list[dict]:
    with REGISTRY_PATH.open(encoding="utf-8") as f:
        return json.load(f)["terms"]


def _parse_effective_date(revision: str | None) -> date | None:
    """revision이 YYYY-MM-DD 형태면 effective_date로. 아니면 None.

    개정 시점이 곧 시행일이다. db_travel 2건은 revision이 없어 None으로 둔다 -
    상품명이 달라 매칭이 끊기지 않는다(BACKEND_REPLY_4 2-4).
    """
    if not revision:
        return None
    try:
        return date.fromisoformat(revision)
    except ValueError:
        return None


def migrate_one(repo: TermsRepository, entry: dict, dry_run: bool) -> dict:
    stem = entry["id"]
    insurer = entry["insurer"]
    product = entry["product"]
    revision = entry.get("revision")

    chunks_path = CHUNKS_DIR / f"{stem}_chunks.json"
    emb_path = EMBEDDINGS_DIR / f"{stem}_embeddings.json"
    if not chunks_path.exists() or not emb_path.exists():
        return {"terms": stem, "skipped": "청크/임베딩 없음"}

    chunks = json.load(chunks_path.open(encoding="utf-8"))
    embeddings = json.load(emb_path.open(encoding="utf-8"))

    # 재적재면 기존 id 재사용, 아니면 새로. 부분 유니크 위반 방지.
    terms_id = repo.find_verified_terms_id(insurer, product, revision) or uuid4()

    terms_row = terms_mapper.terms_row(
        terms_id, insurer, product, revision, _parse_effective_date(revision)
    )
    chunk_rows = terms_mapper.chunk_rows(terms_id, chunks, embeddings)
    embedded = sum(1 for r in chunk_rows if r[terms_mapper.CHUNK_COLUMNS.index("embedding")] is not None)

    if dry_run:
        return {
            "terms": stem, "terms_id": str(terms_id),
            "chunks": len(chunk_rows), "embedded": embedded, "dry_run": True,
        }

    repo.save_terms(terms_row, chunk_rows, coverage_tree=None)
    return {
        "terms": stem, "terms_id": str(terms_id),
        "chunks": len(chunk_rows), "embedded": embedded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="공용 약관 -> policy_terms 계열 적재")
    parser.add_argument("--terms", default=None, help="특정 약관 id만 (기본: 전체)")
    parser.add_argument("--dry-run", action="store_true", help="넣지 않고 확인만")
    args = parser.parse_args()

    dsn = get_settings().database_url
    if not dsn:
        raise SystemExit(
            "DATABASE_URL이 없습니다. 실서버 DB는 AI 컨테이너 안에서만 접속됩니다.\n"
            "  docker exec polight-ai python scripts/migrate_terms_to_db.py"
        )

    entries = _registry()
    if args.terms:
        entries = [e for e in entries if e["id"] == args.terms]
        if not entries:
            raise SystemExit(f"레지스트리에 없는 약관: {args.terms}")

    repo = TermsRepository(dsn)
    print(f"{'약관':22} {'청크':>6} {'임베딩':>6}  terms_id")
    print("-" * 78)
    for entry in entries:
        result = migrate_one(repo, entry, args.dry_run)
        if result.get("skipped"):
            print(f"{result['terms']:22} 건너뜀 - {result['skipped']}")
            continue
        tag = " (dry-run)" if result.get("dry_run") else ""
        print(f"{result['terms']:22} {result['chunks']:>6} {result['embedded']:>6}  {result['terms_id']}{tag}")

    print("\n완료." + (" (실제로는 넣지 않았습니다 - dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
