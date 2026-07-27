import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


# JSON 로드 유틸: 파일 없으면 명확한 에러 메시지
def load_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# 청크 리스트를 받아 매칭 통계 + 카테고리별 개수 + 샘플 청크를 콘솔에 출력 (검수용)
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

    # 카테고리별 개수를 많은 순으로 출력
    print("\n=== Category Counts ===")
    for category, count in category_counter.most_common():
        print(f"{category}: {count}")

    # 매칭된 청크 상위 top_n개 미리보기
    print("\n=== Sample Matched Chunks ===")
    for chunk in matched[:top_n]:
        print("-" * 80)
        print(f"chunk_id: {chunk.get('chunk_id')}")
        print(f"pages: {chunk.get('page_start')} - {chunk.get('page_end')}")
        print(f"title: {chunk.get('section_title')}")
        print(f"category: {chunk.get('matched_category')}")
        print(f"keyword: {chunk.get('matched_keyword')}")
        print(f"text_preview: {chunk.get('text', '')[:200].replace(chr(10), ' ')}")

    # 매칭 안 된 청크 상위 10개 미리보기 (카테고리 매핑 보강 참고용)
    print("\n=== Sample Unmatched Chunks ===")
    for chunk in unmatched[:10]:
        print("-" * 80)
        print(f"chunk_id: {chunk.get('chunk_id')}")
        print(f"pages: {chunk.get('page_start')} - {chunk.get('page_end')}")
        print(f"title: {chunk.get('section_title')}")
        print(f"text_preview: {chunk.get('text', '')[:200].replace(chr(10), ' ')}")


# CLI 진입점: chunks JSON 경로를 받아 요약 출력
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
