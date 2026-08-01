from pathlib import Path

from scripts.extract_pdf_text import extract_pdf_pages


# scripts/extract_pdf_text.py의 추출 로직을 그대로 재사용.
# 반환값은 파일로 저장하지 않고 다음 단계(청킹)로 바로 넘길 수 있는 in-memory 리스트.
def extract_pages(pdf_path: Path) -> list[dict]:
    return extract_pdf_pages(pdf_path)
