"""
[파일 개요 - 코드리뷰]
chunk_policy.py가 만든 청크 결과(*_chunks.json)를 사람이 눈으로 검토하기 위한
디버깅/검수용 스크립트. 실제 파이프라인 처리 결과물을 만들지는 않고, 콘솔에
통계(매칭률, 카테고리별 개수)와 샘플 청크 몇 개를 출력해서 "청킹/카테고리 매핑이
잘 됐는지"를 빠르게 확인할 수 있게 해준다.
파일명은 parse_coverage지만 실제로는 파싱이 아니라 "결과 리포팅" 역할을 한다.
"""

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


# [코드리뷰] load_json
# 역할: JSON 파일을 읽어오는 단순 유틸. 파일이 없으면 명확한 한글 에러 메시지로 실패.
def load_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# [코드리뷰] print_chunk_summary  (핵심 로직)
# 역할: 청크 리스트를 받아 콘솔에 4개 섹션으로 요약을 출력한다.
#   1) 전체 개수 / 카테고리 매칭 성공(matched) / 실패(unmatched) 개수와 매칭률
#   2) Counter.most_common()으로 카테고리별 청크 개수 집계 (많이 나온 순)
#   3) 매칭된 청크 상위 top_n개를 미리보기(제목, 카테고리, 본문 앞 200자)
#   4) 매칭 안 된 청크 상위 10개를 미리보기 (카테고리 매핑 config를 보강할 때 참고용)
# 인터뷰 포인트: chunk.get(...)을 사용해 KeyError 없이 안전하게 필드를 조회한다
#   (일부 청크에 특정 필드가 없을 가능성을 방어).
def print_chunk_summary(chunks: list[dict], top_n: int = 20) -> None:
    total = len(chunks)
    matched = [chunk for chunk in chunks if chunk.get("matched_category")]
    unmatched = [chunk for chunk in chunks if not chunk.get("matched_category")]

    category_counter = Counter(
        chunk.get("matched_category") for chunk in matched
    )

    print("=== Chunk Summary ===")
    print(f"Total chunks: {total}")
    print(f"Matched chunks: {len(matched)}")
    print(f"Unmatched chunks: {len(unmatched)}")

    if total:
        print(f"Match ratio: {len(matched) / total:.2%}")

    print("\n=== Category Counts ===")
    for category, count in category_counter.most_common():
        print(f"{category}: {count}")

    print("\n=== Sample Matched Chunks ===")
    for chunk in matched[:top_n]:
        print("-" * 80)
        print(f"chunk_id: {chunk.get('chunk_id')}")
        print(f"pages: {chunk.get('page_start')} - {chunk.get('page_end')}")
        print(f"title: {chunk.get('section_title')}")
        print(f"category: {chunk.get('matched_category')}")
        print(f"keyword: {chunk.get('matched_keyword')}")
        print(f"text_preview: {chunk.get('text', '')[:200].replace(chr(10), ' ')}")

    print("\n=== Sample Unmatched Chunks ===")
    for chunk in unmatched[:10]:
        print("-" * 80)
        print(f"chunk_id: {chunk.get('chunk_id')}")
        print(f"pages: {chunk.get('page_start')} - {chunk.get('page_end')}")
        print(f"title: {chunk.get('section_title')}")
        print(f"text_preview: {chunk.get('text', '')[:200].replace(chr(10), ' ')}")


# [코드리뷰] main (CLI 진입점)
# 역할: chunks JSON 경로를 인자로 받아 load_json -> print_chunk_summary로 이어주는
#       얇은 진입점. --top-n으로 미리보기 개수를 조절 가능.
# 실행 예: python scripts/parse_coverage.py data/chunks/example_chunks.json --top-n 10
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect chunking and category mapping results."
    )
    parser.add_argument(
        "chunks_json",
        type=str,
        help="Path to chunks JSON. Example: data/chunks/kakao_travel_2025_chunks.json",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Number of matched chunks to preview.",
    )

    args = parser.parse_args()

    chunks_path = Path(args.chunks_json)
    chunks = load_json(chunks_path)

    print_chunk_summary(chunks, top_n=args.top_n)


if __name__ == "__main__":
    main()
