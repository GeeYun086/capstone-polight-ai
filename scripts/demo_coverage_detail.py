"""증권 담보 카드에 약관 세부정보를 붙여 '보장 상세 화면'을 조립해 본다.

지금 증권 콜백은 담보 카드(제목·금액·상태)만 채우고 detailItems·subLimits·
exclusions·requiredDocuments는 빈 배열로 보낸다. 이 네 가지는 증권 표에 없고
약관에 있다. 이 스크립트는 부품이 다 있고 '연결'만 없다는 것을 실물로 확인한다.

    증권 담보  --카테고리--> 약관 청크 --extract_coverage_item--> 세부정보
                                                              detailItems
                                                              subLimits
                                                              requiredDocuments
                                                              exclusions

운영에서 "어느 약관인가"는 백엔드가 정한다(docs/BACKEND_INTERFACE.md 3-1).
여기서는 로컬 약관 청크(data/chunks)로 대신한다. 실제 파이프라인 통합 전에
결합 결과가 목업의 보장 상세 화면과 맞는지 눈으로 확인하려는 목적이다.

사용법
    python scripts/demo_coverage_detail.py --terms hyundai_travel_2025
    python scripts/demo_coverage_detail.py --terms hyundai_travel_2025 --provider gemini-flash
    python scripts/demo_coverage_detail.py --terms hyundai_travel_2025 --category baggage

주의: 카테고리마다 LLM을 한 번씩 부른다. 약관 하나면 5~8회다.
"""

import argparse
import glob
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.coverage_extractor import extract_coverage_item, load_categories  # noqa: E402

CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"


def load_chunks(terms_id: str) -> list[dict]:
    matches = glob.glob(str(CHUNKS_DIR / f"{terms_id}*_chunks.json"))
    if not matches:
        raise SystemExit(
            f"약관 청크를 찾지 못했습니다: {terms_id}\n"
            f"사용 가능: {[Path(p).stem.replace('_chunks','') for p in glob.glob(str(CHUNKS_DIR / '*_chunks.json'))]}"
        )
    return json.load(open(matches[0], encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="증권 담보 + 약관 세부정보 결합 데모")
    parser.add_argument("--terms", default="hyundai_travel_2025", help="약관 id (data/chunks 기준)")
    parser.add_argument("--provider", default=None, help="추출 provider (예: gemini-flash). 없으면 기본값")
    parser.add_argument("--category", default=None, help="한 카테고리만 돌려본다")
    args = parser.parse_args()

    chunks = load_chunks(args.terms)
    cats = load_categories()

    # 약관 청크에 matched_category가 붙어 있다(색인 시점에 분류). 카테고리별로 모은다.
    by_cat: dict[str, list[dict]] = {}
    for c in chunks:
        cat = c.get("matched_category")
        if cat:
            by_cat.setdefault(cat, []).append(c)

    targets = [args.category] if args.category else sorted(
        by_cat, key=lambda c: cats.get(c, {}).get("ui_priority", 99)
    )

    print("=" * 78)
    print(f"약관: {args.terms} / 청크 {len(chunks)}개 / 카테고리 {len(by_cat)}종")
    print("=" * 78)

    total = {"detail": 0, "sub": 0, "doc": 0, "excl": 0}
    for cat in targets:
        src = by_cat.get(cat, [])
        if not src:
            print(f"\n[{cat}] 해당 청크 없음 — 건너뜀")
            continue

        display = cats.get(cat, {}).get("display_name", cat)
        print(f"\n[{cat}] {display} — 약관 청크 {len(src)}개")
        item, warns = extract_coverage_item(cat, display, src, provider=args.provider)
        if not item:
            print(f"  추출 실패: {warns[:2]}")
            continue

        total["detail"] += len(item.detail_items)
        total["sub"] += len(item.sub_limits)
        total["doc"] += len(item.required_documents)
        total["excl"] += len(item.exclusions)

        if item.detail_items:
            print("  ✓ 세부항목 :", ", ".join(d.title for d in item.detail_items[:6]))
        if item.sub_limits:
            print("  · 세부한도 :", ", ".join(f"{s.label}={s.value}" for s in item.sub_limits[:4]))
        if item.required_documents:
            print("  📄 청구서류:", ", ".join(r.document_name for r in item.required_documents[:5]))
        if item.exclusions:
            print("  ✗ 면책     :", ", ".join(e.title for e in item.exclusions[:5]))
        if warns:
            print("  ⚠ 경고     :", warns[:2])

    print("\n" + "=" * 78)
    print(
        f"합계  세부항목 {total['detail']} / 세부한도 {total['sub']} / "
        f"청구서류 {total['doc']} / 면책 {total['excl']}"
    )
    print("이 네 가지가 증권 담보 카드에 붙으면 보장 상세 화면이 채워진다.")


if __name__ == "__main__":
    main()
