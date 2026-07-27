"""
[파일 개요 - 코드리뷰]
PDF(보험약관) 원문에서 페이지 단위로 "정제된" 텍스트를 뽑아내는 스크립트.
PyMuPDF(fitz)로 PDF의 텍스트 블록(block) 좌표를 읽어서,
1) 모든 페이지에 반복되는 머리말/꼬리말(회사명, 페이지 번호 등)을 통계적으로 찾아 제거하고
2) 블록들을 같은 줄(row)로 묶은 뒤, 좌우로 배치된 블록(표/레이블-내용 구조)의 읽기 순서를 보정해서
3) 최종적으로 사람이 읽는 순서와 비슷한 문자열로 합친다.
이 스크립트의 출력(*_pages.json)이 chunk_policy.py의 입력이 된다.
파이프라인 순서: extract_pdf_text.py -> chunk_policy.py -> embed_chunks.py
"""

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


# [코드리뷰] collect_repeated_texts
# 역할: 문서 전체를 훑으며 "페이지마다 똑같이 반복되는 문구"(예: 회사 로고 문구, 문서 제목,
#       페이지 번호 등)를 찾아낸다.
# 동작 방식:
#   - 모든 페이지의 텍스트 블록 중, y좌표가 페이지 상단 8%(HEADER_Y_RATIO) 이내면 헤더 후보,
#     하단 8%(FOOTER_Y_RATIO 이후) 이내면 푸터 후보로 分류해 Counter로 등장 횟수를 센다.
#   - 전체 페이지 수의 40%(REPEATED_TEXT_MIN_RATIO) 이상 등장한 텍스트만 "반복 텍스트"로 확정.
# 왜 이렇게 짜여있나: 페이지마다 다른 헤더/푸터(장 제목 등)는 걸러내고, 진짜 고정 반복
#   요소만 잡아내기 위해 "비율 기반 임계값"을 사용한다. 나중에 extract_page_text에서
#   이 set에 포함된 텍스트를 실제로 제거한다.
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


# [코드리뷰] is_page_number_line
# 역할: 한 줄짜리 텍스트가 "페이지 번호 라인"인지 정규식으로 판별한다.
# 예시로 매칭되는 패턴: "- 12 -", "12", "- 12 -  카카오페이손보" 등
#   (숫자 앞뒤로 하이픈/en-dash/em-dash가 있을 수 있고, 뒤에 짧은 문자열이 붙을 수 있음)
# 왜 필요한가: collect_repeated_texts는 "완전히 동일한 문자열"이 반복될 때만 잡아내는데,
#   페이지 번호는 페이지마다 숫자가 달라져서 그 로직으로는 걸러지지 않는다.
#   그래서 별도의 정규식 기반 필터를 둔 것.
def is_page_number_line(text: str) -> bool:
    """페이지 번호 단독 라인 여부 판단."""
    stripped = text.strip()
    # "- 12 -", "12", "- 12 -  카카오페이손보" 등
    return bool(re.match(r"^[-–—]?\s*\d{1,4}\s*[-–—]?\s*\S{0,20}$", stripped))


# [코드리뷰] group_blocks_into_rows
# 역할: PyMuPDF가 반환한 블록(각각 x0,y0,x1,y1,text 좌표 포함)들을,
#       화면에서 "같은 줄(row)"에 있다고 볼 수 있는 것끼리 묶는다.
# 동작 방식:
#   1. 블록들을 y0(위쪽 좌표) 기준 오름차순 정렬.
#   2. 현재 그룹의 최대 y1(그 줄에서 가장 아래로 내려간 지점)보다 새 블록의 y중심이 더 아래에
#      있으면 새로운 row 시작, 아니면(y범위가 겹치면) 같은 row에 추가.
# 왜 필요한가: PDF 블록은 왼쪽->오른쪽 순서가 아니라 내부적으로 배치 순서대로 나올 수 있어서
#   (예: 표에서 2번째 컬럼이 먼저 나오는 경우), 같은 줄에 있는 블록들을 먼저 묶은 뒤
#   가로 위치(x좌표)로 재정렬해야 사람이 읽는 순서가 나온다. (아래 format_row에서 재정렬)
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


# [코드리뷰] format_row
# 역할: 같은 줄(row)에 속한 블록들을 최종 텍스트 한 줄/여러 줄로 합친다.
# 3가지 케이스로 분기:
#   1) 블록이 1개뿐이면 그대로 반환.
#   2) 블록이 정확히 2개이고, 왼쪽 블록이 페이지 폭의 45%(TABLE_LEFT_CELL_MAX_RATIO) 이내에서
#      끝나는 "레이블"처럼 보이면 -> "레이블\n내용" 형태로 합침 (약관의 "조항명 / 본문" 구조 보존용).
#   3) 그 외 2개 이상 블록(N컬럼 표 행) -> 각 블록 내부 줄바꿈을 공백으로 합쳐 한 줄로 만들고,
#      블록 사이는 COLUMN_SEPARATOR(" | ")로 이어붙임. 나중에 이 구분자로 split하면 원래
#      컬럼 값을 그대로 복원할 수 있다 (표 데이터 파싱을 염두에 둔 설계).
# 인터뷰 포인트: 왜 줄바꿈이 아니라 " | "를 쓰는지 -> 셀 안에 원래 있던 줄바꿈과, 컬럼과 컬럼
#   사이의 경계를 구분하기 위함.
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


# [코드리뷰] extract_page_text
# 역할: 한 페이지에 대해 위 헬퍼 함수들을 조합해 최종 정제 텍스트를 만드는 오케스트레이터.
# 처리 순서:
#   1. page.get_text("blocks")로 블록 추출 후, type==0(텍스트)인 블록만 남김(1은 이미지).
#   2. group_blocks_into_rows로 같은 줄끼리 묶음.
#   3. 각 row 안에서 노이즈(너무 짧은 블록, 반복 헤더/푸터, 페이지 번호)를 제거.
#   4. format_row로 줄 단위 텍스트 생성 후, 내부 공백을 정리.
#   5. 모든 줄을 개행으로 이어붙여 페이지 전체 텍스트로 반환.
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


# [코드리뷰] extract_pdf_pages
# 역할: 스크립트의 핵심 진입점 함수. PDF 파일 경로를 받아 페이지별 텍스트 리스트를 반환한다.
# 동작: fitz.open으로 PDF를 열고(암호화된 PDF는 예외 처리), collect_repeated_texts로
#       문서 전체의 반복 텍스트를 1회 계산한 뒤, 각 페이지마다 extract_page_text를 호출.
#       결과는 [{"page": 1, "text": "..."}, ...] 형태의 리스트.
# 참고: table_eval_compare.py에서도 이 함수를 그대로 import해서 재사용한다
#       (기존 파이프라인 로직을 건드리지 않고 비교 실험에 활용).
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


# [코드리뷰] save_pages_json
# 역할: 추출된 페이지 리스트를 JSON 파일로 저장하는 단순 IO 헬퍼.
#       ensure_ascii=False로 한글이 유니코드 escape 없이 그대로 저장되게 함.
def save_pages_json(pages: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)


# [코드리뷰] main (CLI 진입점)
# 역할: argparse로 "PDF 경로"와 "출력 디렉토리"를 받아 추출을 실행하고,
#       결과를 <PDF파일명>_pages.json으로 저장한 뒤 페이지 수/글자 수 등 요약을 출력한다.
# 실행 예: python scripts/extract_pdf_text.py data/raw_pdfs/example.pdf
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
