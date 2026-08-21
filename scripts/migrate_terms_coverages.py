"""약관에서 보장 규칙을 추출해 policy_terms_coverages 계열에 적재한다.

보장 상세 화면(면책·청구서류·세부한도·세부항목)의 데이터다. 증권 표에는 없고
약관에 있어, 약관 청크에서 LLM으로 뽑는다. 백엔드가 증권 담보와 이 규칙을
title로 매칭해 연결한다(coverage_items.terms_coverage_id, PR #36 6-2).

전제: 약관 청크가 이미 policy_terms_chunks에 있어야 한다(migrate_terms_to_db 먼저).
      규칙은 그 약관(terms_id)에 매달린다.

LLM을 카테고리마다 부른다(약관 1건당 5~8회). 8건이면 수십 회다. 크레딧이 없으면
Gemini 무료로도 되지만 RPM 제한에 걸려 느리다. 한 번만 하면 되는 배치라
재실행이 안전하도록 설계했다(규칙만 DELETE 후 INSERT, 청크는 안 건드림).

실행: AI 컨테이너 안에서(DATABASE_URL 필요).

    docker exec polight-ai python scripts/migrate_terms_coverages.py --provider gemini-flash
    docker exec polight-ai python scripts/migrate_terms_coverages.py --terms hyundai_travel_2025 --provider gemini-flash
    docker exec polight-ai python scripts/migrate_terms_coverages.py --dry-run --provider gemini-flash
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.repositories import terms_mapper  # noqa: E402
from app.repositories.terms_repository import TermsRepository  # noqa: E402
from app.services.coverage_extractor import extract_all  # noqa: E402

CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"
REGISTRY_PATH = PROJECT_ROOT / "config" / "terms_registry.json"


def _registry() -> list[dict]:
    with REGISTRY_PATH.open(encoding="utf-8") as f:
        return json.load(f)["terms"]


def process_one(repo: TermsRepository, entry: dict, provider: str | None, dry_run: bool) -> dict:
    stem = entry["id"]
    insurer, product, revision = entry["insurer"], entry["product"], entry.get("revision")

    # 규칙은 이미 이관된 약관(terms_id)에 매달린다. 청크가 안 들어가 있으면 건너뛴다.
    terms_id = repo.find_verified_terms_id(insurer, product, revision)
    if terms_id is None:
        return {"terms": stem, "skipped": "약관 미이관 (migrate_terms_to_db 먼저)"}

    chunks_path = CHUNKS_DIR / f"{stem}_chunks.json"
    if not chunks_path.exists():
        return {"terms": stem, "skipped": "청크 파일 없음"}
    chunks = json.load(chunks_path.open(encoding="utf-8"))

    # 카테고리별로 규칙 추출. 하나 실패해도 나머지는 살린다(extract_all 내부).
    items, warnings = extract_all(chunks, provider=provider)
    for w in warnings:
        print(f"    경고: {w}")

    tree = terms_mapper.coverage_rows(terms_id, items)
    summary = {
        "terms": stem, "terms_id": str(terms_id),
        "rules": len(tree["coverages"]),
        "detail": len(tree["detail_items"]),
        "sub": len(tree["sub_limits"]),
        "doc": len(tree["required_documents"]),
        "excl": len(tree["exclusions"]),
    }
    if dry_run:
        summary["dry_run"] = True
        return summary

    repo.save_coverages(terms_id, tree)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="약관 보장 규칙 -> policy_terms_coverages 적재")
    parser.add_argument("--terms", default=None, help="특정 약관만 (기본: 전체)")
    parser.add_argument("--provider", default=None, help="추출 provider (예: gemini-flash)")
    parser.add_argument("--dry-run", action="store_true", help="넣지 않고 개수만")
    args = parser.parse_args()

    dsn = get_settings().database_url
    if not dsn:
        raise SystemExit(
            "DATABASE_URL이 없습니다. AI 컨테이너 안에서 실행하세요.\n"
            "  docker exec polight-ai python scripts/migrate_terms_coverages.py --provider gemini-flash"
        )

    entries = _registry()
    if args.terms:
        entries = [e for e in entries if e["id"] == args.terms]
        if not entries:
            raise SystemExit(f"레지스트리에 없는 약관: {args.terms}")

    repo = TermsRepository(dsn)
    print(f"{'약관':22} {'규칙':>5} {'세부':>5} {'한도':>5} {'서류':>5} {'면책':>5}")
    print("-" * 62)
    total = {"rules": 0, "detail": 0, "sub": 0, "doc": 0, "excl": 0}
    for entry in entries:
        r = process_one(repo, entry, args.provider, args.dry_run)
        if r.get("skipped"):
            print(f"{r['terms']:22} 건너뜀 - {r['skipped']}")
            continue
        for k in total:
            total[k] += r[k]
        tag = " (dry-run)" if r.get("dry_run") else ""
        print(f"{r['terms']:22} {r['rules']:>5} {r['detail']:>5} {r['sub']:>5} {r['doc']:>5} {r['excl']:>5}{tag}")

    print("-" * 62)
    print(f"{'합계':22} {total['rules']:>5} {total['detail']:>5} {total['sub']:>5} {total['doc']:>5} {total['excl']:>5}")
    print("\n완료." + (" (dry-run - 넣지 않음)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
