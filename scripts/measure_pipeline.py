"""분석 파이프라인의 단계별 소요 시간과 API 호출 수를 잰다 (측정 D층).

왜 필요한가: 증권 기반으로 바꾸면 "빨라졌다"를 말해야 하는데, 지금 무엇이 얼마나
걸리는지 아무도 정확히 모른다. extraction_comparison.json에 카테고리당 6.32초라는
기록이 있지만 그건 추출 단계만이고, 파싱·임베딩까지 합친 전체 대기 시간은 잰 적이 없다.

이 층은 정답이 필요 없어서 가장 이견이 없는 수치다. 그래서 발표에서 제일 강하다.

주의: --no-cache로 돌리면 Upstage 파싱 API가 실제로 호출된다(페이지 단위 과금).
캐시가 있으면 파싱은 0초로 나오므로, 진짜 대기 시간을 재려면 캐시를 꺼야 한다.

사용법
    python scripts/measure_pipeline.py --pdf data/raw_pdfs/db_travel.pdf --no-cache \
        --out data/eval/pipeline_terms.json --note "약관 기반 (증권 전환 전)"

    # 파싱만 캐시로 건너뛰고 나머지만 재고 싶을 때
    python scripts/measure_pipeline.py --pdf data/raw_pdfs/db_travel.pdf
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import fitz  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.upstage_parser import parse_pdf  # noqa: E402
from scripts.chunk_policy import (  # noqa: E402
    DEFAULT_MAPPING_PATH,
    build_mapping_entries,
    create_chunks_from_elements,
    load_json,
)
from scripts.embed_chunks import BATCH_SIZE  # noqa: E402

DEFAULT_PDF = PROJECT_ROOT / "data" / "raw_pdfs" / "db_travel.pdf"


class Stopwatch:
    """단계별 시간을 재고 바로 출력한다. 오래 걸리는 단계에서 멈춘 것처럼 보이지 않게."""

    def __init__(self) -> None:
        self.stages: dict[str, float] = {}

    def run(self, name: str, fn):
        print(f"  {name} ...", end="", flush=True)
        start = time.perf_counter()
        result = fn()
        elapsed = time.perf_counter() - start
        self.stages[name] = elapsed
        print(f" {elapsed:.1f}초")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="파이프라인 단계별 시간 측정")
    parser.add_argument("--pdf", type=str, default=str(DEFAULT_PDF))
    parser.add_argument("--no-cache", action="store_true", help="Upstage 파싱을 실제로 호출한다 (과금)")
    parser.add_argument("--skip-extract", action="store_true", help="LLM 추출 단계를 건너뛴다")
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--note", type=str, default="")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"PDF가 없습니다: {pdf_path}")

    with fitz.open(pdf_path) as doc:
        pages = doc.page_count

    settings = get_settings()
    print(f"\n대상: {pdf_path.name} ({pages}페이지)")
    print(f"임베딩: {settings.embedding_provider} / 캐시: {'끔' if args.no_cache else '켬'}\n")

    watch = Stopwatch()

    elements = watch.run(
        "1. 파싱 (Upstage)", lambda: parse_pdf(pdf_path, use_cache=not args.no_cache)
    )

    mapping = build_mapping_entries(load_json(DEFAULT_MAPPING_PATH))
    chunks = watch.run(
        "2. 청킹 + 라벨링",
        lambda: create_chunks_from_elements(elements, pdf_path.name, mapping),
    )

    # 임베딩은 여기서 import한다. 위에서 하면 파싱만 재고 싶을 때도 모듈이 로드된다.
    from app.services.embedding_service import embed_chunks

    embeddings = watch.run("3. 임베딩", lambda: embed_chunks(chunks))

    items = []
    if not args.skip_extract:
        from app.services.coverage_extractor import extract_all

        items, warnings = watch.run("4. 담보 추출 (LLM)", lambda: extract_all(chunks))
        for w in warnings[:3]:
            print(f"     경고: {w}")

    total = sum(watch.stages.values())

    # API 호출 수. 시간과 함께 봐야 어디에 돈이 드는지 보인다.
    parse_calls = -(-pages // 50)  # PAGES_PER_REQUEST
    embed_calls = -(-len(chunks) // BATCH_SIZE)
    llm_calls = len(items) if items else 0
    labeled = sum(1 for c in chunks if c.get("matched_category"))

    print("\n" + "=" * 56)
    print(f"총 소요 시간: {total:.1f}초")
    print("=" * 56)
    for name, seconds in watch.stages.items():
        bar = "#" * max(1, round(seconds / total * 40)) if total else ""
        print(f"  {name:20} {seconds:7.1f}초 {seconds / total:5.1%} {bar}")

    print(f"\n  페이지 {pages} / 요소 {len(elements)} / 청크 {len(chunks)}"
          f" (라벨 있음 {labeled}, {labeled / len(chunks):.0%})")
    print(f"  API 호출: 파싱 {parse_calls}회 · 임베딩 {embed_calls}회 · LLM {llm_calls}회")

    # 사전 배치로 옮겼을 때의 이점.
    # 약관 처리를 사용자 요청 경로에서 빼면 이 시간이 통째로 사라진다.
    print(f"\n  이 중 약관 처리(1~4단계 전부)를 사전 배치로 옮기면")
    print(f"  사용자 대기 시간에서 {total:.1f}초가 통째로 사라진다.")
    print(f"  N명이 같은 상품을 쓰면 1인당 분담 시간은 {total:.1f}/N 초로 줄어든다.")
    for n in (1, 10, 100, 1000):
        print(f"    N={n:<5} 1인당 {total / n:8.2f}초")

    if args.out:
        payload = {
            "note": args.note,
            "pdf": pdf_path.name,
            "pages": pages,
            "cache_disabled": args.no_cache,
            "embedding_provider": settings.embedding_provider,
            "total_seconds": total,
            "stages": watch.stages,
            "counts": {
                "elements": len(elements),
                "chunks": len(chunks),
                "labeled_chunks": labeled,
                "embeddings": len(embeddings),
                "coverage_items": len(items),
                "parse_api_calls": parse_calls,
                "embed_api_calls": embed_calls,
                "llm_calls": llm_calls,
            },
            "git_commit": _git_commit(),
        }
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n측정 결과 저장: {out_path}")


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except Exception:
        return None


if __name__ == "__main__":
    main()
