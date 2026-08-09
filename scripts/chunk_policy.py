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

# 조항 제목 인식용 정규식들 (제N장/조, 번호목록, 보험 도메인 키워드 등). 위에서부터 순서대로 매칭 시도
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
# "손해"/"사항"은 보험사별 표기 차이다. 현대해상은 "보상하지 않는 사항"을 쓰고 "손해"를 쓰지 않아,
# 손해형 키워드만 있으면 면책 조항이 통째로 누락된다.
EXCLUDED_KEYWORDS = [
    "보상하지 않는 손해",
    "보상하지 않는 사항",
    "보상하지 아니하는 손해",
    "보상하지 아니하는 사항",
    "보험금을 지급하지 않는",
    "면책사항",
    "지급하지 않는 사유",
    "보상하지 않습니다",
    "보상하지 아니합니다",
]
INCLUDED_KEYWORDS = ["보상하는 손해", "보험금의 지급사유", "보험금 지급사유", "지급기준"]
PROCEDURE_KEYWORDS = ["보험금의 청구", "청구서류", "지급절차", "보험금 청구", "서류를 제출"]
DEFINITION_KEYWORDS = ["용어의 정의", "용어 해설", "보험용어"]


# JSON 로드 공통 유틸: 파일 없음/빈 파일/파싱 실패를 각각 다른 에러 메시지로 처리
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


# JSON 저장 공통 유틸
def save_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# 키워드 매칭 시 공백 차이/대소문자 차이로 실패하지 않도록 정규화
def normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


# ── 목차 페이지 감지 ──────────────────────────────────────────

# 줄 수가 적거나 평균 줄 길이가 짧으면 목차/안내 페이지로 판단 (조항 제목 오인식 방지용)
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

# 한 줄이 조항 제목일 가능성이 높은지 판별 (길이/노이즈 키워드/콤마 개수/패턴 매칭 순 필터)
def is_probable_title(line: str) -> bool:
    line = line.strip()
    if not line or len(line) < 2 or len(line) > 120:
        return False
    if any(kw in line for kw in NOISE_TITLE_KEYWORDS):
        return False
    if line.count(",") >= 3:
        return False
    return any(p.match(line) for p in SECTION_TITLE_PATTERNS)


# 제목 줄에서 실제 제목 텍스트만 추출 (매칭된 패턴의 마지막 캡처 그룹 사용)
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

# 제목+본문 앞부분을 보고 조항 성격을 4종(면책/청구절차/용어정의/보장)으로 분류
def detect_coverage_type(title: str, text: str) -> str:
    # match_category와 동일하게 정규화해서 비교한다. 원문 그대로 비교하면 PDF에서 넘어온
    # 줄바꿈이나 불규칙한 공백("보상하지  않는") 때문에 매칭이 조용히 실패한다.
    combined = normalize_for_match(f"{title}\n{text[:500]}")

    def has(keywords: list[str]) -> bool:
        return any(normalize_for_match(kw) in combined for kw in keywords)

    # 우선순위: 면책 -> 청구절차 -> 용어정의 -> 보장(기본값)
    if has(EXCLUDED_KEYWORDS):
        return "excluded"
    if has(PROCEDURE_KEYWORDS):
        return "procedure"
    if has(DEFINITION_KEYWORDS):
        return "definition"
    if has(INCLUDED_KEYWORDS):
        return "included"
    return "included"


# ── 카테고리 매핑 ─────────────────────────────────────────────

# config/category_mapping.json을 검색 가능한 리스트로 변환 (긴 키워드 우선 매칭되도록 정렬)
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
    # 짧은 키워드가 긴 키워드의 부분 문자열이라 먼저 잘못 매칭되는 것을 막기 위해 길이 내림차순
    entries.sort(key=lambda e: len(e["normalized_keyword"]), reverse=True)
    return entries


# 제목+본문 전체에서 키워드 목록을 순서대로 검사해 첫 매칭 카테고리를 반환
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

# 페이지 리스트를 {page, line} 단위로 한 줄씩 펼친다 (조항이 페이지 경계를 넘어가도 순서 유지)
def flatten_pages(pages: list[dict]) -> list[dict]:
    rows = []
    for page in pages:
        page_number = page["page"]
        for line in page["text"].splitlines():
            cleaned = line.strip()
            if cleaned:
                rows.append({"page": page_number, "line": cleaned})
    return rows


# 청킹 핵심 로직: 제목 줄을 만날 때마다 이전까지 모은 텍스트를 청크로 확정(flush)한다
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

    # 지금까지 모은 줄들을 카테고리/coverage_type 태깅해서 청크 하나로 확정
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

    # 한 줄씩 순회: 제목 줄이면 이전 내용을 flush하고 새 청크 시작, 아니면 그냥 누적
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


# MIN_CHUNK_CHARS 미만인 청크를 다음 청크와 합친다 (카테고리가 다르면 병합하지 않음)
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

        # 너무 작고 다음 chunk가 존재하면 병합 시도 (조건 만족하는 동안 계속 흡수)
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

            # 병합된 청크의 제목은 내용을 더 많이 차지한 쪽 것을 쓴다.
            # 약관은 각 특별약관이 "제N조(준용규정) 이 특별약관에 정하지 않은 사항은
            # 보통약관을 따릅니다."라는 짧은 조항으로 끝나는데, 이게 MIN_CHUNK_CHARS 미만이라
            # 다음 특약의 첫 조항을 흡수한다. 앞 제목을 그대로 두면 본문은 "항공기 지연 보상"인데
            # 제목은 "준용규정"이 되고, build_embed_text가 제목을 본문 앞에 붙여 임베딩하므로
            # 검색 품질이 직접 나빠진다. (실측: 항공기 지연 질의가 top-8에서 사라졌다)
            # clause_path(상위 특약명)도 같이 따라가야 한다. 앞의 짧은 준용규정이 다음 특약의
            # 첫 조항을 흡수하면서 이전 특약명을 유지하면, 임베딩에 엉뚱한 특약명이 붙는다.
            # (실측: 항공기 지연 조항에 "특정전염병 특별약관"이 붙어 검색에서 밀렸다)
            dominant = current if current["char_count"] >= nxt["char_count"] else nxt
            dominant_title = dominant["section_title"]
            dominant_path = dominant.get("clause_path") or current.get("clause_path") or nxt.get("clause_path")

            current = {
                **current,
                "section_title": dominant_title,
                "clause_path": dominant_path,
                "text": merged_text,
                "char_count": len(merged_text),
                "page_end": nxt["page_end"],
                "matched_category": current["matched_category"] or nxt["matched_category"],
                "secondary_category": current["secondary_category"] or nxt["secondary_category"],
                "matched_keyword": current["matched_keyword"] or nxt["matched_keyword"],
                # coverage_type은 여기서 정하지 않는다. 병합 전 원본 청크 기준으로 판정된 값이라
                # 흡수된 청크(예: 면책 조항)의 성격이 사라진다. 병합이 끝난 뒤
                # reclassify_coverage_types()가 합쳐진 본문 전체를 보고 다시 판정한다.
            }
            i += 1

        merged.append(current)
        i += 1

    return merged


# MAX_CHUNK_CHARS를 넘는 청크를 줄 경계에서 분할한다.
# 상수는 선언돼 있었지만 실제로 적용되는 곳이 없어 최대 16,000자 청크가 그대로 통과했다.
# 임베딩 모델 입력 한도(text-embedding-3-small = 8,191토큰)를 넘으면 API 호출 자체가 실패하고,
# embed_chunks.py는 배치 단위로 보내기 때문에 청크 하나가 배치 50개를 같이 죽인다.
# 한 청크에 여러 주제가 섞이면 벡터가 희석돼 검색 정확도도 떨어진다.
def split_large_chunks(chunks: list[dict]) -> list[dict]:
    result: list[dict] = []

    for chunk in chunks:
        if chunk["char_count"] <= MAX_CHUNK_CHARS:
            result.append(chunk)
            continue

        # 1차: 줄 경계에서 자른다 (조항 내용이 줄 중간에서 끊기지 않도록)
        parts: list[list[str]] = [[]]
        length = 0
        for line in chunk["text"].splitlines():
            if length and length + len(line) + 1 > MAX_CHUNK_CHARS:
                parts.append([])
                length = 0
            parts[-1].append(line)
            length += len(line) + 1

        # 2차: 한 줄 자체가 한도를 넘는 경우(표 행이 " | "로 길게 이어진 경우 등)
        # 줄 경계가 없으므로 글자 수로 강제 분할해 한도를 반드시 지킨다.
        texts: list[str] = []
        for part_lines in parts:
            text = "\n".join(part_lines).strip()
            if not text:
                continue
            while len(text) > MAX_CHUNK_CHARS:
                texts.append(text[:MAX_CHUNK_CHARS])
                text = text[MAX_CHUNK_CHARS:]
            if text:
                texts.append(text)

        # 첫 조각은 원래 chunk_id를 유지하고 이어지는 조각에만 _2, _3 접미사를 붙인다.
        # chunk_id는 policy_chunks의 PK가 되므로 ASCII 유지와 고유성이 중요하다.
        for i, text in enumerate(texts, start=1):
            result.append(
                {
                    **chunk,
                    "chunk_id": chunk["chunk_id"] if i == 1 else f"{chunk['chunk_id']}_{i}",
                    "text": text,
                    "char_count": len(text),
                }
            )

    return result


# 병합이 끝난 본문 전체를 다시 보고 coverage_type을 재판정한다.
# detect_coverage_type은 병합 전 원본 청크마다 실행되기 때문에, 작은 included 청크가
# 뒤따르는 면책 조항을 흡수하면 결과 청크가 면책 본문을 담고도 included로 남는다.
# 면책 조항이 보장 조항으로 위장되면 RAG가 "보상된다"의 근거로 인용하게 되므로,
# link_exclusion_pairs보다 먼저 실행해야 한다 (페어링이 coverage_type에 의존).
def reclassify_coverage_types(chunks: list[dict]) -> list[dict]:
    for chunk in chunks:
        chunk["coverage_type"] = detect_coverage_type(chunk["section_title"], chunk["text"])
    return chunks


# "보상하는 손해" 청크와 바로 뒤 "보상하지 않는 손해" 청크를 related_chunk_id로 상호 연결
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


# ── Upstage 요소 기반 청킹 ────────────────────────────────────

# 조항이 시작되는 요소인지 판단한다.
# Upstage는 제N조를 heading으로 분류하지 않고 paragraph로 주지만, 레이아웃 분석으로
# 조항마다 요소를 나눠주기 때문에 "요소의 첫 줄이 제N조/제N관으로 시작하는가"만 보면 된다.
# pymupdf 경로처럼 문서 전체를 줄 단위로 훑으며 제목을 추측할 필요가 없다.
CLAUSE_START_RE = re.compile(r"^제\s*\d+\s*[조관장절]")

# 특별약관 이름을 나타내는 줄. 예: "해외여행중 식중독 특별약관"
#
# Upstage의 category만으로는 구분되지 않는다. 같은 특약명이 heading1로 오기도 하고
# paragraph로 오기도 하며, 반대로 "제2조(준용규정)"이 heading1로 오기도 한다.
# 반면 "~특별약관/보통약관으로 끝나는 짧은 줄"은 약관 문서에서 안정적인 신호다.
SECTION_NAME_RE = re.compile(r"^.{2,40}(특별약관|보통약관)$")


def _section_name(element: dict) -> str | None:
    text = element["text"].strip()
    if not text:
        return None
    first = text.splitlines()[0].strip()
    if CLAUSE_START_RE.match(first):
        return None
    return first if SECTION_NAME_RE.match(first) else None


def _is_clause_start(element: dict) -> bool:
    text = element["text"].strip()
    if not text:
        return False
    if CLAUSE_START_RE.match(text.splitlines()[0]):
        return True
    # 표나 목록 중간에서 새 조항이 시작되는 일은 없으므로 heading 계열만 추가로 인정한다
    return element["category"] in ("heading1", "heading2", "heading3")


# 요소 묶음에서 조항 제목을 뽑는다. 첫 줄이 곧 제목이며,
# pymupdf 경로에서 "제5조("가 잘려 "보험금을 지급하지 않는 사유)"로 남던 문제가 여기서 사라진다.
def _title_from_elements(group: list[dict]) -> str:
    for element in group:
        text = element["text"].strip()
        if text:
            return text.splitlines()[0].strip()[:200]
    return "문서 시작"


# policy_chunks.source_content_type(NOT NULL)에 넣을 값.
# 표가 섞여 있으면 표로 본다 - 표 여부가 이후 금액 파싱에서 의미가 있기 때문이다.
def _content_type(group: list[dict]) -> str:
    categories = {e["category"] for e in group}
    if "table" in categories:
        return "table"
    if categories & {"heading1", "heading2", "heading3"}:
        return "heading"
    if categories == {"list"}:
        return "list"
    return "paragraph"


# Upstage 요소를 조항 단위로 재조립한다.
#
# 요소를 그대로 청크로 쓰지 않는 이유: 한 조항이 문단·목록 여러 요소로 쪼개져 나온다
# (실측 db_travel p5~10에서 요소 49개 대 조항 15개). 그대로 임베딩하면 조각이 너무 잘아
# 검색 단위로 쓸 수 없다.
def create_raw_chunks_from_elements(
    elements: list[dict],
    source_file: str,
    mapping_entries: list[dict],
) -> list[dict]:
    # 머리말/꼬리말은 페이지마다 반복되는 노이즈라 제외한다
    body = [e for e in elements if e["category"] not in ("header", "footer") and e["text"].strip()]
    if not body:
        return []

    # 현재 어느 특별약관 안에 있는지 추적한다.
    # 조항 제목("제1조(보험금의 지급사유)")은 특약마다 반복돼 변별력이 없어서,
    # 임베딩할 때 상위 특약명을 붙여줘야 검색이 구분된다.
    # (실측: 항공기 지연 질의가 제목만으로는 top-8 밖으로 밀렸다)
    groups: list[list[dict]] = [[]]
    paths: list[str] = [""]
    current_path = ""

    for element in body:
        name = _section_name(element)
        if name:
            current_path = name

        if _is_clause_start(element) and groups[-1]:
            groups.append([])
            paths.append(current_path)
        elif not groups[-1]:
            paths[-1] = current_path

        groups[-1].append(element)

    chunks = []
    for index, (group, clause_path) in enumerate(zip(groups, paths), start=1):
        text = "\n".join(e["text"].strip() for e in group if e["text"].strip()).strip()
        if not text:
            continue

        title = _title_from_elements(group)
        category_result = match_category(f"{clause_path}\n{title}", text, mapping_entries)

        chunks.append(
            {
                "chunk_id": f"{Path(source_file).stem}_{index:04d}",
                "source_file": source_file,
                "page_start": min(e["page"] for e in group),
                "page_end": max(e["page"] for e in group),
                "section_title": title,
                "clause_path": clause_path,
                "source_content_type": _content_type(group),
                "coverage_type": detect_coverage_type(title, text),
                "text": text,
                "char_count": len(text),
                "matched_category": category_result["matched_category"],
                "secondary_category": category_result["secondary_category"],
                "matched_keyword": category_result["matched_keyword"],
                "related_chunk_id": None,
            }
        )

    return chunks


# Upstage 요소로부터 최종 청크를 만든다. 재조립 이후 단계(병합/재판정/분할/면책 연결)는
# pymupdf 경로와 완전히 동일한 도메인 로직을 그대로 태운다.
def create_chunks_from_elements(
    elements: list[dict],
    source_file: str,
    mapping_entries: list[dict],
) -> list[dict]:
    raw = create_raw_chunks_from_elements(elements, source_file, mapping_entries)
    merged = merge_small_chunks(raw)
    retyped = reclassify_coverage_types(merged)
    split = split_large_chunks(retyped)
    return link_exclusion_pairs(split)


# raw 생성 -> 소형 병합 -> coverage_type 재판정 -> 대형 분할 -> 면책쌍 연결 순서로 실행하는 공개 API.
# 재판정을 분할보다 먼저 하는 이유: 조항 전체를 보고 성격을 정한 뒤 조각들이 그 라벨을 물려받게 해서,
# 같은 조항의 조각들이 서로 다른 coverage_type을 갖는 일을 막는다.
def create_chunks(
    pages: list[dict],
    source_file: str,
    mapping_entries: list[dict],
) -> list[dict]:
    raw = create_raw_chunks(pages, source_file, mapping_entries)
    merged = merge_small_chunks(raw)
    retyped = reclassify_coverage_types(merged)
    split = split_large_chunks(retyped)
    linked = link_exclusion_pairs(split)
    return linked


# ── main ──────────────────────────────────────────────────────

# CLI 진입점: pages.json + 매핑 파일을 받아 청킹 실행, <이름>_chunks.json 저장, 통계 출력
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
