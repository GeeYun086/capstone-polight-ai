"""
[파일 개요 - 코드리뷰]
전체 PDF 처리 파이프라인(추출 -> 청킹)을 한 번에 돌려주는 오케스트레이션 스크립트.
직접 로직을 구현하지 않고, extract_pdf_text.py와 chunk_policy.py를 별도의
파이썬 프로세스(subprocess)로 순서대로 실행시켜주는 "실행기" 역할만 한다.
(임베딩 단계(embed_chunks.py)는 API 비용이 들기 때문에 이 파이프라인에는
포함되지 않고 별도로 수동 실행하도록 분리되어 있다.)
"""

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw_pdfs"
EXTRACTED_DIR = PROJECT_ROOT / "data" / "extracted_text"
CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"


# [코드리뷰] run_command
# 역할: 커맨드 리스트를 subprocess.run으로 실행하고, 실행할 명령을 그대로 콘솔에
#       echo(`$ ...`)해서 어떤 단계가 실행 중인지 보여준다.
# 왜 별도 프로세스로 실행하나: extract_pdf_text.py/chunk_policy.py를 import해서 함수
#   호출로 직접 쓸 수도 있지만, 여기서는 각 스크립트를 "독립 CLI 도구"로 유지하면서
#   그대로 재사용하는 방식을 택함(각 스크립트가 단독 실행도 가능해야 하므로).
# 실패 처리: returncode가 0이 아니면 RuntimeError를 던져서, 파이프라인이 조용히
#   다음 단계로 넘어가지 않고 즉시 중단되게 한다(fail-fast).
def run_command(command: list[str]) -> None:
    print(f"\n$ {' '.join(command)}")
    result = subprocess.run(command, cwd=PROJECT_ROOT)

    if result.returncode != 0:
        raise RuntimeError(f"명령어 실행 실패: {' '.join(command)}")


# [코드리뷰] process_pdf
# 역할: PDF 한 개에 대해 "추출 -> 청킹" 2단계를 순서대로 실행한다.
# 동작: 1단계로 extract_pdf_text.py를 호출해 <이름>_pages.json을 생성하고,
#   2단계로 그 pages.json 경로를 인자로 chunk_policy.py를 호출한다.
#   sys.executable을 사용해 현재 실행 중인 파이썬(가상환경 포함)과 동일한 인터프리터로
#   하위 스크립트를 실행하도록 보장한다.
def process_pdf(pdf_path: Path) -> None:
    pages_json = EXTRACTED_DIR / f"{pdf_path.stem}_pages.json"

    run_command(
        [
            sys.executable,
            "scripts/extract_pdf_text.py",
            str(pdf_path),
        ]
    )

    run_command(
        [
            sys.executable,
            "scripts/chunk_policy.py",
            str(pages_json),
        ]
    )


# [코드리뷰] main (CLI 진입점)
# 역할: --pdf 인자로 특정 파일 하나만 처리하거나, 인자가 없으면 data/raw_pdfs 안의
#       모든 PDF를 sorted 순서로 순회하며 process_pdf를 호출한다.
# 실행 예: python scripts/run_pipeline.py            (전체 PDF 처리)
#          python scripts/run_pipeline.py --pdf a.pdf (특정 PDF만 처리)
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run PDF extraction and chunking pipeline."
    )
    parser.add_argument(
        "--pdf",
        type=str,
        default=None,
        help="Specific PDF filename in data/raw_pdfs. If omitted, all PDFs are processed.",
    )

    args = parser.parse_args()

    if args.pdf:
        pdf_paths = [RAW_PDF_DIR / args.pdf]
    else:
        pdf_paths = sorted(RAW_PDF_DIR.glob("*.pdf"))

    if not pdf_paths:
        raise FileNotFoundError(f"처리할 PDF가 없습니다: {RAW_PDF_DIR}")

    for pdf_path in pdf_paths:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")

        print("=" * 100)
        print(f"Processing: {pdf_path.name}")
        print("=" * 100)

        process_pdf(pdf_path)


if __name__ == "__main__":
    main()
