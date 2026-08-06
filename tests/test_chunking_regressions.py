"""오늘 실제로 발생했던 청킹 결함 3건에 대한 회귀 테스트.

세 결함 모두 에러 없이 조용히 잘못된 결과를 내던 종류라, 테스트가 없으면 재발을 알아채기 어렵다.
"""

from scripts.chunk_policy import (
    MAX_CHUNK_CHARS,
    build_mapping_entries,
    create_chunks,
    detect_coverage_type,
    load_json,
    DEFAULT_MAPPING_PATH,
)
from scripts.extract_pdf_text import sanitize_text


def mapping_entries():
    return build_mapping_entries(load_json(DEFAULT_MAPPING_PATH))


# is_toc_page는 줄 수가 3개 미만이거나 평균 줄 길이가 짧은 페이지를 목차로 보고 제외한다.
# 따라서 테스트 입력도 여러 줄로 만들어야 실제 본문 페이지로 취급된다.
def long_body_page(line_count: int) -> list[dict]:
    body = "\n".join(
        f"{i}. 회사는 피보험자가 여행 중 입은 상해에 대하여 보험금을 지급합니다."
        for i in range(line_count)
    )
    return [{"page": 1, "text": f"제1조(보상하는 손해)\n{body}"}]


# 결함 1: merge_small_chunks가 첫 청크의 coverage_type을 고정해서,
# 작은 보장 청크가 면책 조항을 흡수하면 면책 라벨이 사라졌다 (실측 excluded 13 -> 5).
# 면책 조항이 included로 위장되면 챗봇이 "보상된다"의 근거로 인용한다.
def test_exclusion_survives_small_chunk_merge():
    # 앞 청크를 MIN_CHUNK_CHARS(300) 미만으로 만들어 뒤 면책 조항을 흡수하게 유도한다
    pages = [
        {
            "page": 1,
            "text": (
                "제1조(보상하는 손해)\n"
                "회사는 피보험자가 여행 중 입은 상해를 보상합니다.\n"
                "제2조(보상하지 않는 손해)\n"
                + "회사는 피보험자의 고의로 생긴 손해는 보상하지 않습니다. " * 12
            ),
        }
    ]

    chunks = create_chunks(pages, "test.pdf", mapping_entries())

    assert any(c["coverage_type"] == "excluded" for c in chunks), (
        "병합 후 면책 라벨이 유실됐다 — create_chunks에서 reclassify_coverage_types가 빠졌는지 확인"
    )


# 결함 2: MAX_CHUNK_CHARS가 선언만 되어 있고 적용되지 않아 16,000자 청크가 통과했다.
# text-embedding-3-small 입력 한도(8,191토큰)를 넘으면 임베딩 API 호출이 실패하고,
# embed_chunks.py는 배치로 보내기 때문에 청크 하나가 배치 50개를 함께 죽인다.
def test_no_chunk_exceeds_max_chars():
    chunks = create_chunks(long_body_page(400), "test.pdf", mapping_entries())

    assert chunks, "본문 페이지가 목차로 오인돼 걸러졌다"
    assert sum(c["char_count"] for c in chunks) > MAX_CHUNK_CHARS, "분할이 필요한 크기가 아니다"
    oversized = [c for c in chunks if c["char_count"] > MAX_CHUNK_CHARS]
    assert not oversized, f"{len(oversized)}개 청크가 상한을 넘었다"


def test_split_chunk_ids_stay_unique_and_ascii():
    chunks = create_chunks(long_body_page(400), "test.pdf", mapping_entries())
    ids = [c["chunk_id"] for c in chunks]

    assert len(ids) > 1, "분할이 일어나지 않아 검증 의미가 없다"

    assert len(ids) == len(set(ids)), "분할된 청크의 chunk_id가 중복됐다"
    # chunk_id는 policy_chunks의 PK가 되고 Spring의 coverage_item_sources가 FK로 참조한다
    for chunk_id in ids:
        assert chunk_id.isascii(), f"chunk_id에 비ASCII 문자가 있다: {chunk_id}"


# 결함 3: 일부 PDF가 본문에 제어문자(U+0001)를 섞어 내보내고, 제어문자는 공백이 아니라
# str.split()이나 \s 정규식에 걸리지 않아 키워드 매칭이 조용히 실패했다.
# (hyundai_travel_2022는 추출 텍스트의 18.4%가 U+0001이었고 면책 조항이 0개로 잡혔다.)
def test_sanitize_removes_control_chars_but_keeps_layout():
    raw = "보상하지\x01 않는\x01 사항\x01\n제4조\t내용\x07"

    cleaned = sanitize_text(raw)

    assert "\x01" not in cleaned
    assert "\x07" not in cleaned
    assert cleaned == "보상하지 않는 사항\n제4조\t내용"
    assert "\n" in cleaned and "\t" in cleaned, "개행/탭은 레이아웃 정보라 남겨야 한다"


# 결함 3-b: EXCLUDED_KEYWORDS에 "보상하지 않는 손해"만 있어서, "사항"을 쓰는 보험사
# (현대해상)의 면책 조항이 통째로 누락됐다.
def test_detects_exclusion_for_both_wordings():
    assert detect_coverage_type("제4조(보상하지 않는 사항)", "회사가 보상하지 않는 사항은") == "excluded"
    assert detect_coverage_type("제4조(보상하지 않는 손해)", "회사가 보상하지 않는 손해는") == "excluded"


# 불규칙한 공백/줄바꿈에도 매칭돼야 한다. detect_coverage_type이 원문을 그대로 비교하면
# PDF에서 넘어온 줄바꿈 때문에 실패한다.
def test_detects_exclusion_across_irregular_whitespace():
    assert detect_coverage_type("보상하지\n않는  사항", "본문") == "excluded"
