import argparse
import json
import re
from collections import Counter
from pathlib import Path

import fitz


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "extracted_text"

# 표 레이아웃 감지: 페이지 너비의 이 비율보다 좁은 x1을 가진 블록을 '좌측 셀'로 판단
TABLE_LEFT_CELL_MAX_RATIO = 0.45

# 반복 헤더/푸터 감지: 전체 페이지 중 이 비율 이상 동일 텍스트가 상/하단에 나타나면 제거
REPEATED_TEXT_MIN_RATIO = 0.4

# 헤더/푸터로 판단할 y 범위 (페이지 높이 대비 비율)
HEADER_Y_RATIO = 0.08
FOOTER_Y_RATIO = 0.92

# 너무 짧은 블록(노이즈) 최소 글자 수
MIN_BLOCK_CHARS = 2

# N블록(컬럼) 테이블 행에서 컬럼 사이를 구분하는 구분자.
# 셀 내부 줄바꿈(같은 컬럼 안에서 여러 줄로 감싸진 경우)과 컬럼 경계를 구분할 수 있도록,
# 줄바꿈이 아닌 별도 구분자를 사용한다.
COLUMN_SEPARATOR = " | "


def collect_repeated_texts(doc: fitz.Document) -> set[str]:
    """
    전체 문서에서 상단/하단에 반복 등장하는 텍스트를 수집한다.
    페이지 번호, 문서명 헤더 등이 대상이다.
    """
    total_pages = len(doc)
    header_counter: Counter = Counter()
    footer_counter: Counter = Counter()

    for page in doc:
        page_h = page.rect.height
        for b in page.get_text("blocks"):
            x0, y0, x1, y1, text, *_ = b
            text = text.strip()
            if not text or len(text) < MIN_BLOCK_CHARS:
                continue
            normalized = " ".join(text.split())
            if y1 < page_h * HEADER_Y_RATIO:
                header_counter[normalized] += 1
            elif y0 > page_h * FOOTER_Y_RATIO:
                footer_counter[normalized] += 1

    threshold = total_pages * REPEATED_TEXT_MIN_RATIO
    repeated = set()
    for text, count in header_counter.items():
        if count >= threshold:
            repeated.add(text)
    for text, count in footer_counter.items():
        if count >= threshold:
            repeated.add(text)

    return repeated


def is_page_number_line(text: str) -> bool:
    """페이지 번호 단독 라인 여부 판단."""
    stripped = text.strip()
    # "- 12 -", "12", "- 12 -  카카오페이손보" 등
    return bool(re.match(r"^[-–—]?\s*\d{1,4}\s*[-–—]?\s*\S{0,20}$", stripped))


def group_blocks_into_rows(blocks: list) -> list[list]:
    """
    블록들을 y 범위가 겹치는 행(row) 단위로 묶는다.
    각 row는 블록 리스트이며, y 오름차순으로 정렬된다.
    """
    sorted_by_y = sorted(blocks, key=lambda b: b[1])
    rows: list[list] = []
    current_row: list = []
    current_y_max: float = -1

    for b in sorted_by_y:
        y0, y1 = b[1], b[3]
        y_center = (y0 + y1) / 2
        if current_y_max < 0 or y_center > current_y_max:
            if current_row:
                rows.append(current_row)
            current_row = [b]
            current_y_max = y1
        else:
            current_row.append(b)
            current_y_max = max(current_y_max, y1)

    if current_row:
        rows.append(current_row)
    return rows


def format_row(row: list, page_width: float) -> str:
    """
    한 행(row)의 블록들을 텍스트로 변환한다.

    - 블록이 1개: 그냥 텍스트 반환
    - 블록이 2개이고 좌측 레이블 + 우측 내용 패턴이면 "레이블\n내용" 형식으로 합침
      (문서 본문의 "제목 / 본문" 구조를 보존하기 위한 기존 동작, 표가 아닌 일반 조항에도 쓰임)
    - 그 외 2개 이상의 블록(N컬럼 표 행): 각 블록을 한 줄로 합친 뒤 COLUMN_SEPARATOR로 연결.
      셀 내부의 줄바꿈(같은 컬럼이 여러 줄로 감싸진 경우)과 컬럼 경계를 구분하기 위해
      컬럼 사이는 개행이 아닌 별도 구분자를 사용한다 - 이렇게 하면 한 표 행이 한 줄로 나오고,
      COLUMN_SEPARATOR로 split하면 컬럼별 값을 그대로 복원할 수 있다.
    """
    if len(row) == 1:
        return row[0][4].strip()

    label_threshold = page_width * TABLE_LEFT_CELL_MAX_RATIO
    sorted_row = sorted(row, key=lambda b: b[0])

    # 좌측 레이블 + 우측 내용 패턴 감지 (2블록 전용)
    if len(sorted_row) == 2:
        left, right = sorted_row
        left_x1 = left[2]
        right_x0 = right[0]
        left_text = left[4].strip()
        right_text = right[4].strip()
        if left_x1 <= label_threshold and right_x0 > label_threshold * 0.7:
            # 레이블이 짧고(한 단어~짧은 구) 내용이 길면 인라인 합침
            if left_text and right_text:
                clean_label = " ".join(left_text.split())
                clean_right = right_text
                return f"{clean_label}\n{clean_right}"

    # N블록(컬럼) 표 행: 블록마다 내부 줄바꿈을 한 줄로 합치고, 컬럼 구분자로 연결
    columns = []
    for b in sorted_row:
        raw = b[4].strip()
        if not raw:
            continue
        columns.append(" ".join(raw.split()))

    return COLUMN_SEPARATOR.join(columns)


def extract_page_text(
    page: fitz.Page,
    repeated_texts: set[str],
) -> str:
    """
    단일 페이지에서 정제된 텍스트를 추출한다.

    - 반복 헤더/푸터 제거
    - 페이지 번호 라인 제거
    - 표 레이아웃 블록 순서 보정
    - 빈 블록 / 노이즈 제거
    """
    page_w = page.rect.width
    page_h = page.rect.height
    blocks = page.get_text("blocks")

    # 텍스트 블록만 필터 (type=0 이 텍스트, type=1 이 이미지)
    text_blocks = [b for b in blocks if b[6] == 0]

    rows = group_blocks_into_rows(text_blocks)

    lines = []
    for row in rows:
        # 행 내 각 블록에서 노이즈 필터 적용 후 유효 블록만 남김
        clean_row = []
        for b in row:
            raw_text = b[4].strip()
            if not raw_text or len(raw_text) < MIN_BLOCK_CHARS:
                continue
            normalized = " ".join(raw_text.split())
            if normalized in repeated_texts:
                continue
            if is_page_number_line(raw_text):
                continue
            clean_row.append(b)

        if not clean_row:
            continue

        row_text = format_row(clean_row, page_w)
        if not row_text.strip():
            continue

        # 블록 내 줄바꿈 정리
        cleaned_lines = []
        for line in row_text.splitlines():
            cleaned = " ".join(line.strip().split())
            if cleaned:
                cleaned_lines.append(cleaned)

        if cleaned_lines:
            lines.append("\n".join(cleaned_lines))

    return "\n".join(lines)


def extract_pdf_pages(pdf_path: Path) -> list[dict]:
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")

    with fitz.open(pdf_path) as doc:
        if doc.is_encrypted:
            raise ValueError(f"암호화된 PDF는 처리할 수 없습니다: {pdf_path.name}")

        repeated_texts = collect_repeated_texts(doc)

        pages = []
        for page_index, page in enumerate(doc, start=1):
            text = extract_page_text(page, repeated_texts)
            pages.append(
                {
                    "page": page_index,
                    "text": text,
                }
            )

    return pages


def save_pages_json(pages: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract page-level text from a travel insurance PDF."
    )
    parser.add_argument(
        "pdf_path",
        type=str,
        help="Path to input PDF file.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
    )

    args = parser.parse_args()
    pdf_path = Path(args.pdf_path)
    output_dir = Path(args.output_dir)

    pages = extract_pdf_pages(pdf_path)

    output_path = output_dir / f"{pdf_path.stem}_pages.json"
    save_pages_json(pages, output_path)

    total_chars = sum(len(p["text"]) for p in pages)
    non_empty = sum(1 for p in pages if p["text"].strip())

    print(f"PDF     : {pdf_path.name}")
    print(f"Pages   : {len(pages)} (non-empty: {non_empty})")
    print(f"Chars   : {total_chars}")
    print(f"Saved   : {output_path}")


if __name__ == "__main__":
    main()
