import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw_pdfs"
EXTRACTED_DIR = PROJECT_ROOT / "data" / "extracted_text"
CHUNKS_DIR = PROJECT_ROOT / "data" / "chunks"


def run_command(command: list[str]) -> None:
    print(f"\n$ {' '.join(command)}")
    result = subprocess.run(command, cwd=PROJECT_ROOT)

    if result.returncode != 0:
        raise RuntimeError(f"명령어 실행 실패: {' '.join(command)}")


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