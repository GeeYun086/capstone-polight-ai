"""Upstage 파싱 기반 청킹 파이프라인.

기존 run_pipeline.py는 pymupdf 경로(추출 -> 청킹)이고, 이쪽은
Upstage Document Parse로 파싱한 뒤 요소를 조항 단위로 재조립한다.

파싱 결과는 data/parsed_results/{stem}_upstage.json에 캐시되므로,
청킹 로직만 고쳐서 재실행할 때는 API 비용이 다시 들지 않는다.

사용법
    python scripts/run_upstage_pipeline.py                    # raw_pdfs 전체
    python scripts/run_upstage_pipeline.py --pdf db_travel.pdf
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.upstage_parser import parse_pdf  # noqa: E402
from scripts.chunk_policy import (  # noqa: E402
    DEFAULT_MAPPING_PATH,
    build_mapping_entries,
    create_chunks_from_elements,
    load_json,
    save_json,
)

RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw_pdfs"
CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"


def process(pdf_path: Path, mapping_entries: list[dict]) -> dict:
    elements = parse_pdf(pdf_path)
    chunks = create_chunks_from_elements(elements, pdf_path.name, mapping_entries)

    output_path = CHUNKS_DIR / f"{pdf_path.stem}_chunks.json"
    save_json(chunks, output_path)

    matched = sum(1 for c in chunks if c["matched_category"])
    return {
        "pdf": pdf_path.name,
        "elements": len(elements),
        "chunks": len(chunks),
        "matched": matched,
        "matched_pct": matched / len(chunks) * 100 if chunks else 0,
        "linked": sum(1 for c in chunks if c["related_chunk_id"]),
        "max_chars": max((c["char_count"] for c in chunks), default=0),
        "with_path": sum(1 for c in chunks if c.get("clause_path")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Upstage 파싱 + 조항 단위 청킹")
    parser.add_argument("--pdf", type=str, default=None, help="특정 파일만. 생략하면 전체")
    args = parser.parse_args()

    pdf_paths = [RAW_PDF_DIR / args.pdf] if args.pdf else sorted(RAW_PDF_DIR.glob("*.pdf"))
    if not pdf_paths:
        raise FileNotFoundError(f"처리할 PDF가 없습니다: {RAW_PDF_DIR}")

    mapping_entries = build_mapping_entries(load_json(DEFAULT_MAPPING_PATH))

    stats = []
    for pdf_path in pdf_paths:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF를 찾을 수 없습니다: {pdf_path}")
        print(f"=== {pdf_path.name} ===", flush=True)
        stats.append(process(pdf_path, mapping_entries))
        print(f"  청크 {stats[-1]['chunks']}개", flush=True)

    print()
    print(f'{"pdf":24} {"요소":>6} {"청크":>6} {"matched":>9} {"linked":>7} {"path":>6} {"max자":>6}')
    print("-" * 72)
    for s in stats:
        print(
            f'{s["pdf"][:24]:24} {s["elements"]:>6} {s["chunks"]:>6} '
            f'{s["matched"]:>4}({s["matched_pct"]:>4.1f}%) {s["linked"]:>7} {s["with_path"]:>6} {s["max_chars"]:>6}'
        )


if __name__ == "__main__":
    main()
