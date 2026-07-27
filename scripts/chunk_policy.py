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


def normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


# ── 목차 페이지 감지 ──────────────────────────────────────────

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

def is_probable_title(line: str) -> bool:
    line = line.strip()
    if not line or len(line) < 2 or len(line) > 120:
        return False
    if any(kw in line for kw in NOISE_TITLE_KEYWORDS):
        return False
    if line.count(",") >= 3:
        return False
    return any(p.match(line) for p in SECTION_TITLE_PATTERNS)


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

def flatten_pages(pages: list[dict]) -> list[dict]:
    rows = []
    for page in pages:
        page_number = page["page"]
        for line in page["text"].splitlines():
            cleaned = line.strip()
            if cleaned:
                rows.append({"page": page_number, "line": cleaned})
    return rows


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
