"""
[파일 개요 - 코드리뷰]
extract_pdf_text.py가 만든 "페이지 단위 텍스트"(*_pages.json)를 받아서,
RAG(검색증강생성)에 쓸 수 있는 "조항(section) 단위 청크(chunk)"로 재구성하는 스크립트.

핵심 흐름 (create_chunks 함수 참고):
  1. create_raw_chunks : 목차 페이지를 건너뛰고, "제목처럼 보이는 줄"을 기준으로
     텍스트를 조항 단위로 자른다. 각 조각에 카테고리(약관 분류)와 coverage_type(보장/면책 등)을 태깅.
  2. merge_small_chunks : 너무 짧게 잘린 청크는 다음 청크와 합쳐서 최소 크기를 보장.
  3. link_exclusion_pairs : "보상하는 손해" 청크와 바로 뒤따르는 "보상하지 않는 손해" 청크를
     서로 참조하도록 연결(related_chunk_id) - 나중에 검색 시 함께 보여주기 위함.

파이프라인 순서: extract_pdf_text.py -> chunk_policy.py -> embed_chunks.py
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MAPPING_PATH = PROJECT_ROOT / "config" / "category_mapping.json"
DEFAULT_INPUT_DIR = PROJECT_ROOT / "data" / "extracted_text"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "chunks"

# 목차 페이지 판단 기준: 평균 줄 길이가 이 값 미만이면 목차로 간주
TOC_AVG_LINE_LEN = 28

# 이 글자 수 미만인 chunk는 다음 chunk에 병합
MIN_CHUNK_CHARS = 300

# 최대 chunk 크기 (이 이상이면 조항 경계에서 분리)
MAX_CHUNK_CHARS = 2000

# [코드리뷰] 조항 제목을 인식하기 위한 정규식 목록.
# "제N장/절", "제N조", "1. ...", "가. ...", 보험 도메인 키워드가 포함된 줄, 정형화된
# 보험 문구(보상하는 손해 등)를 각각 매칭한다. 위에서부터 순서대로 시도(is_probable_title,
# extract_title에서 재사용)하며, 매칭되는 첫 패턴을 사용한다.
SECTION_TITLE_PATTERNS = [
    re.compile(r"^(제\s*\d+\s*[장절])\s+(.+)$"),
    re.compile(r"^(제\s*\d+\s*조)\s*[\(\[]?(.{2,80}?)[\)\]]?$"),
    re.compile(r"^\d{1,3}\.\s+(.{2,100})$"),
    re.compile(r"^[가-힣]\.\s+(.{2,100})$"),
    re.compile(
        r"^(.{2,100}(보통약관|특별약관|실손의료비|배상책임|휴대품손해|수하물|항공기|지연|결항|구조송환|여행중단|여행취소).*)$"
    ),
    re.compile(
        r"^(보상하는 손해|보상하지 않는 손해|보상하지 아니하는 손해|보험금의 지급사유|보험금을 지급하지 않는 사유|보험금 청구|보험금 지급|용어의 정의)$"
    ),
]

NOISE_TITLE_KEYWORDS = ["목차", "개인정보", "상품요약서", "가입자 유의사항", "주요내용 요약서"]

# coverage_type 감지 키워드
EXCLUDED_KEYWORDS = ["보상하지 않는 손해", "보상하지 아니하는 손해", "보험금을 지급하지 않는", "면책사항", "지급하지 않는 사유"]
INCLUDED_KEYWORDS = ["보상하는 손해", "보험금의 지급사유", "보험금 지급사유", "지급기준"]
PROCEDURE_KEYWORDS = ["보험금의 청구", "청구서류", "지급절차", "보험금 청구", "서류를 제출"]
DEFINITION_KEYWORDS = ["용어의 정의", "용어 해설", "보험용어"]


# [코드리뷰] load_json / save_json
# 역할: 이 프로젝트 전반에서 반복되는 JSON 입출력을 감싼 공통 유틸.
# load_json은 파일 부재/빈 파일/JSON 파싱 실패를 각각 다른 예외 메시지로 알려준다
# (디버깅 시 "왜 실패했는지"를 바로 알 수 있게 하기 위함).
def load_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"JSON 파일이 비어 있습니다: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 형식이 올바르지 않습니다: {path}") from e


def save_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# [코드리뷰] normalize_for_match
# 역할: 문자열 비교(카테고리 키워드 매칭 등)를 할 때 공백을 전부 제거하고 소문자로
#       바꿔서, "띄어쓰기 차이"나 "대소문자 차이"로 매칭이 실패하지 않게 한다.
def normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


# ── 목차 페이지 감지 ──────────────────────────────────────────

# [코드리뷰] is_toc_page
# 역할: 어떤 페이지가 "목차/안내" 페이지인지 휴리스틱으로 판단한다.
# 판단 기준: 줄 수가 3줄 미만이거나, 줄당 평균 글자 수가 28자(TOC_AVG_LINE_LEN) 미만이면 목차로 간주.
# 왜 필요한가: 목차 페이지는 "제1장 여행보험..... 3" 같은 한 줄짜리 항목들로 이루어져 있어서,
#   그대로 두면 아래 create_raw_chunks의 "제목 감지" 로직이 이 항목들을 실제 조항 제목으로
#   오인해 의미 없는 청크를 대량 생성한다. 그래서 청킹 전에 미리 걸러낸다.
def is_toc_page(page: dict) -> bool:
    """
    평균 줄 길이가 짧은 페이지는 목차/안내 페이지로 판단한다.
    목차 페이지의 조항 제목 한 줄짜리들이 chunk로 만들어지는 것을 방지한다.
    """
    lines = [l.strip() for l in page["text"].splitlines() if l.strip()]
    if len(lines) < 3:
        return True
    avg_len = sum(len(l) for l in lines) / len(lines)
    return avg_len < TOC_AVG_LINE_LEN


# ── 제목 탐지 ─────────────────────────────────────────────────

# [코드리뷰] is_probable_title
# 역할: 한 줄이 "조항 제목"일 가능성이 높은지 판별하는 필터.
# 체크 순서: 길이가 2~120자 범위인지 -> 노이즈 키워드(목차 등)를 포함하지 않는지 ->
#   콤마가 3개 이상이면 제목이 아니라 본문 문장일 가능성이 높으므로 제외 ->
#   마지막으로 SECTION_TITLE_PATTERNS 중 하나라도 매칭되는지.
def is_probable_title(line: str) -> bool:
    line = line.strip()
    if not line or len(line) < 2 or len(line) > 120:
        return False
    if any(kw in line for kw in NOISE_TITLE_KEYWORDS):
        return False
    if line.count(",") >= 3:
        return False
    return any(p.match(line) for p in SECTION_TITLE_PATTERNS)


# [코드리뷰] extract_title
# 역할: 제목 줄에서 "제1조 (보험금의 지급사유)" 같은 원문 중 실제 제목 텍스트만 뽑아낸다.
# 동작: 매칭된 정규식의 캡처 그룹들 중 마지막 그룹(가장 구체적인 제목 부분)을 사용.
#   그룹이 하나도 없으면(패턴 자체가 안 잡히면) 원본 줄을 그대로 반환.
def extract_title(line: str) -> str:
    line = line.strip()
    for pattern in SECTION_TITLE_PATTERNS:
        match = pattern.match(line)
        if not match:
            continue
        groups = [g for g in match.groups() if g]
        if not groups:
            return line
        return groups[-1].strip()
    return line


# ── coverage_type 감지 ────────────────────────────────────────

# [코드리뷰] detect_coverage_type
# 역할: 청크의 제목+본문 앞부분(500자)을 보고 이 조항이 보험 도메인에서 어떤 성격인지
#       4가지 중 하나로 분류한다: excluded(면책) / procedure(청구절차) / definition(용어정의) / included(보장).
# 우선순위가 있는 이유: 한 조항 안에 "보상하는 손해"와 "보상하지 않는 손해"가 같이 언급될 수도
#   있어서, 더 결정적인 신호(면책 키워드)를 먼저 체크한다. 아무 키워드도 안 걸리면 기본값은
#   included(보장 항목)로 처리 - 약관 문서 특성상 대부분의 조항이 보장 내용이기 때문.
def detect_coverage_type(title: str, text: str) -> str:
    combined = f"{title}\n{text[:500]}"
    if any(kw in combined for kw in EXCLUDED_KEYWORDS):
        return "excluded"
    if any(kw in combined for kw in PROCEDURE_KEYWORDS):
        return "procedure"
    if any(kw in combined for kw in DEFINITION_KEYWORDS):
        return "definition"
    if any(kw in combined for kw in INCLUDED_KEYWORDS):
        return "included"
    return "included"


# ── 카테고리 매핑 ─────────────────────────────────────────────

# [코드리뷰] build_mapping_entries
# 역할: config/category_mapping.json(키워드 -> 카테고리 매핑 원본)을 검색에 쓰기 좋은
#       리스트 형태로 변환한다.
# 처리: 매핑 값이 문자열이면 primary_category만 있는 것으로, dict이면 primary/secondary
#   카테고리를 함께 갖는 것으로 해석. 각 항목에 normalized_keyword(공백제거+소문자)를
#   미리 계산해두고, 키워드 길이가 긴 것부터 정렬한다.
# 왜 길이 내림차순 정렬인가: match_category에서 "가장 먼저 매칭되는 것"을 채택하는데,
#   짧은 키워드가 긴 키워드의 부분 문자열인 경우(예: "여행"이 "여행자보험"의 일부) 짧은
#   키워드가 먼저 걸려서 부정확하게 매칭되는 것을 방지하기 위해 구체적인(긴) 키워드를 우선한다.
def build_mapping_entries(mapping: dict) -> list[dict]:
    entries = []
    for keyword, value in mapping.items():
        if isinstance(value, str):
            primary_category = value
            secondary_category = None
        elif isinstance(value, dict):
            primary_category = value.get("primary_category")
            secondary_category = value.get("secondary_category")
        else:
            continue
        if not primary_category:
            continue
        entries.append(
            {
                "keyword": keyword,
                "normalized_keyword": normalize_for_match(keyword),
                "primary_category": primary_category,
                "secondary_category": secondary_category,
            }
        )
    entries.sort(key=lambda e: len(e["normalized_keyword"]), reverse=True)
    return entries


# [코드리뷰] match_category
# 역할: 청크의 제목+본문 전체를 정규화한 문자열에서, build_mapping_entries가 만든
#       키워드 목록을 순서대로(긴 것부터) 검사해 처음 매칭되는 카테고리를 반환한다.
# 매칭 안 되면 matched_category=None으로 채워서 반환 (나중에 parse_coverage.py에서
# "unmatched chunk"로 집계됨).
def match_category(title: str, text: str, mapping_entries: list[dict]) -> dict:
    # 제목 + 전체 텍스트를 모두 검색 대상으로 사용 (기존: 앞 1000자만)
    normalized = normalize_for_match(f"{title}\n{text}")
    for entry in mapping_entries:
        if entry["normalized_keyword"] in normalized:
            return {
                "matched_category": entry["primary_category"],
                "secondary_category": entry["secondary_category"],
                "matched_keyword": entry["keyword"],
            }
    return {
        "matched_category": None,
        "secondary_category": None,
        "matched_keyword": None,
    }


# ── 청킹 ─────────────────────────────────────────────────────

# [코드리뷰] flatten_pages
# 역할: [{page:1, text:"줄1\n줄2..."}, ...] 형태의 페이지 리스트를,
#       [{page:1, line:"줄1"}, {page:1, line:"줄2"}, ...] 형태로 한 줄씩 펼친다(flatten).
# 목적: 이후 create_raw_chunks에서 "페이지 경계를 넘나드는" 조항도 줄 단위로 순서대로
#   처리할 수 있게 하기 위함(조항이 페이지를 걸쳐 이어지는 경우가 흔함).
def flatten_pages(pages: list[dict]) -> list[dict]:
    rows = []
    for page in pages:
        page_number = page["page"]
        for line in page["text"].splitlines():
            cleaned = line.strip()
            if cleaned:
                rows.append({"page": page_number, "line": cleaned})
    return rows


# [코드리뷰] create_raw_chunks  (청킹의 핵심 로직)
# 역할: 목차 페이지를 제외한 모든 줄을 순서대로 훑으면서, "제목으로 보이는 줄"을 만날
#       때마다 이전까지 모은 텍스트를 하나의 청크로 확정(flush)하고 새 청크를 시작한다.
# 동작 상세:
#   - is_probable_title(line)이 True면: 지금까지 모은 current_lines를 flush()로 청크화하고,
#     새 제목을 current_title로 세팅 후 새로 텍스트를 모으기 시작.
#   - False면: 그냥 current_lines에 누적.
#   - flush() 내부에서 카테고리 매칭(match_category)과 coverage_type 판별(detect_coverage_type)을
#     수행해서 청크 딕셔너리를 구성 (chunk_id, 페이지 범위, 제목, 본문, 글자수 등 포함).
#   - nonlocal 클로저로 chunk_index/current_lines 등을 flush 함수 안에서 갱신한다.
# 인터뷰 포인트: 이 함수는 "상태 기계(state machine)" 패턴이다 - 한 줄씩 읽으며
#   현재 청크의 상태(제목, 누적 텍스트)를 유지하다가 제목이 나오면 상태를 리셋한다.
def create_raw_chunks(
    pages: list[dict],
    source_file: str,
    mapping_entries: list[dict],
) -> list[dict]:
    """
    조항 단위로 raw chunk를 만든다.
    목차 페이지는 건너뛴다.
    """
    # 목차 페이지 제외
    content_pages = [p for p in pages if not is_toc_page(p)]

    rows = flatten_pages(content_pages)
    if not rows:
        return []

    chunks = []
    current_title = "문서 시작"
    current_lines: list[str] = []
    current_page_start = rows[0]["page"]
    chunk_index = 1

    def flush(page_end: int) -> None:
        nonlocal chunk_index, current_lines, current_title, current_page_start
        text = "\n".join(current_lines).strip()
        if not text:
            return

        category_result = match_category(current_title, text, mapping_entries)
        coverage_type = detect_coverage_type(current_title, text)

        chunks.append(
            {
                "chunk_id": f"{Path(source_file).stem}_{chunk_index:04d}",
                "source_file": source_file,
                "page_start": current_page_start,
                "page_end": page_end,
                "section_title": current_title,
                "coverage_type": coverage_type,
                "text": text,
                "char_count": len(text),
                "matched_category": category_result["matched_category"],
                "secondary_category": category_result["secondary_category"],
                "matched_keyword": category_result["matched_keyword"],
                "related_chunk_id": None,
            }
        )
        chunk_index += 1
        current_lines = []

    for row in rows:
        line = row["line"]
        page = row["page"]

        if is_probable_title(line):
            if current_lines:
                flush(page_end=page)
            current_title = extract_title(line)
            current_page_start = page
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        flush(page_end=rows[-1]["page"])

    return chunks


# [코드리뷰] merge_small_chunks
# 역할: create_raw_chunks가 만든 청크 중 MIN_CHUNK_CHARS(300자) 미만인 것을 바로 다음
#       청크와 합쳐서, 너무 잘게 쪼개진 청크(임베딩 품질을 해칠 수 있음)를 줄인다.
# 병합 조건: 두 청크의 matched_category가 같거나, 둘 중 하나라도 None이면 병합 허용
#   (서로 다른 카테고리끼리는 절대 합치지 않음 - 의미가 섞이는 것을 방지).
# 동작: while 루프로 "현재 청크가 여전히 작고 다음 청크가 있으면" 계속 병합을 시도하는
#   이중 루프 구조. 텍스트/페이지범위/카테고리 정보를 병합하며 이어붙인다.
def merge_small_chunks(chunks: list[dict]) -> list[dict]:
    """
    MIN_CHUNK_CHARS 미만인 chunk를 다음 chunk에 병합한다.
    같은 matched_category이거나 둘 다 None인 경우에만 병합한다.
    """
    if not chunks:
        return []

    merged = []
    i = 0
    while i < len(chunks):
        current = dict(chunks[i])

        # 너무 작고 다음 chunk가 존재하면 병합 시도
        while (
            current["char_count"] < MIN_CHUNK_CHARS
            and i + 1 < len(chunks)
        ):
            nxt = chunks[i + 1]
            same_category = (
                current["matched_category"] == nxt["matched_category"]
                or current["matched_category"] is None
                or nxt["matched_category"] is None
            )
            if not same_category:
                break

            merged_text = current["text"] + "\n" + nxt["text"]
            current = {
                **current,
                "text": merged_text,
                "char_count": len(merged_text),
                "page_end": nxt["page_end"],
                "matched_category": current["matched_category"] or nxt["matched_category"],
                "secondary_category": current["secondary_category"] or nxt["secondary_category"],
                "matched_keyword": current["matched_keyword"] or nxt["matched_keyword"],
                "coverage_type": current["coverage_type"],
            }
            i += 1

        merged.append(current)
        i += 1

    return merged


# [코드리뷰] link_exclusion_pairs
# 역할: 약관 문서 특유의 패턴("보상하는 손해" 조항 바로 뒤에 "보상하지 않는 손해" 조항이
#       따라오는 구조)을 이용해, 두 청크를 related_chunk_id로 서로 연결한다.
# 조건: 현재 청크가 included, 바로 다음 청크가 excluded이고, 두 청크의 matched_category가
#   같아야만 연결한다(카테고리가 다르면 서로 무관한 조항일 수 있으므로).
# 활용 목적: 나중에 "이 보장이 되나요?"라는 질의에 답할 때, 보장 내용과 면책 내용을
#   함께 보여줘야 정확한 답변이 가능하기 때문에 미리 연결해두는 것.
def link_exclusion_pairs(chunks: list[dict]) -> list[dict]:
    """
    "보상하는 손해(included)" chunk 바로 뒤에
    "보상하지 않는 손해(excluded)" chunk가 오면 related_chunk_id로 서로 연결한다.
    같은 matched_category일 때만 연결한다.
    """
    for i, chunk in enumerate(chunks):
        if chunk["coverage_type"] != "included":
            continue
        if i + 1 >= len(chunks):
            continue

        nxt = chunks[i + 1]
        if nxt["coverage_type"] != "excluded":
            continue

        same_cat = (
            chunk["matched_category"] is not None
            and chunk["matched_category"] == nxt["matched_category"]
        )
        if same_cat:
            chunk["related_chunk_id"] = nxt["chunk_id"]
            nxt["related_chunk_id"] = chunk["chunk_id"]

    return chunks


# [코드리뷰] create_chunks
# 역할: 위 3단계(raw 생성 -> 소형 청크 병합 -> 면책쌍 연결)를 순서대로 호출하는
#       공개 API 함수. main()과 table_eval 등 다른 스크립트에서 이 함수 하나만 호출하면 됨.
def create_chunks(
    pages: list[dict],
    source_file: str,
    mapping_entries: list[dict],
) -> list[dict]:
    raw = create_raw_chunks(pages, source_file, mapping_entries)
    merged = merge_small_chunks(raw)
    linked = link_exclusion_pairs(merged)
    return linked


# ── main ──────────────────────────────────────────────────────

# [코드리뷰] main (CLI 진입점)
# 역할: pages.json 경로, 카테고리 매핑 파일, 출력 디렉토리를 인자로 받아 청킹을 실행하고
#       결과를 <이름>_chunks.json으로 저장한 뒤, 매칭률/카테고리 분포 등 통계를 콘솔에 출력한다.
# 실행 예: python scripts/chunk_policy.py data/extracted_text/example_pages.json
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create section-based chunks from extracted travel insurance PDF pages."
    )
    parser.add_argument("pages_json", type=str)
    parser.add_argument("--mapping", type=str, default=str(DEFAULT_MAPPING_PATH))
    parser.add_argument("--output-dir", type=str, default=str(DEFAULT_OUTPUT_DIR))

    args = parser.parse_args()

    pages_path = Path(args.pages_json)
    mapping_path = Path(args.mapping)
    output_dir = Path(args.output_dir)

    pages = load_json(pages_path)
    mapping = load_json(mapping_path)
    mapping_entries = build_mapping_entries(mapping)

    source_file = pages_path.name.replace("_pages.json", ".pdf")
    chunks = create_chunks(pages, source_file, mapping_entries)

    output_path = output_dir / pages_path.name.replace("_pages.json", "_chunks.json")
    save_json(chunks, output_path)

    total = len(chunks)
    matched = sum(1 for c in chunks if c["matched_category"])
    tiny = sum(1 for c in chunks if c["char_count"] < 100)
    linked = sum(1 for c in chunks if c["related_chunk_id"])
    categories = sorted({c["matched_category"] for c in chunks if c["matched_category"]})
    coverage_types = {t: sum(1 for c in chunks if c["coverage_type"] == t)
                      for t in ["included", "excluded", "procedure", "definition"]}

    print(f"Input       : {pages_path}")
    print(f"Chunks      : {total}")
    print(f"Matched     : {matched} ({matched/total:.1%})")
    print(f"Tiny (<100) : {tiny}")
    print(f"Linked pairs: {linked}")
    print(f"Categories  : {categories}")
    print(f"CoverageType: {coverage_types}")
    print(f"Saved       : {output_path}")


if __name__ == "__main__":
    main()
