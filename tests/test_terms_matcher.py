"""증권 -> 약관 매칭 회귀 테스트.

매칭 실패가 서비스 중단이 되면 안 된다. 보장 카드는 증권에서 나오므로 약관이
없어도 화면은 떠야 하고, 약관이 필요한 상세·챗봇만 기능이 줄어야 한다.

그래서 이 모듈은 어떤 입력에도 예외를 내지 않고 NONE을 돌려주는 것이 계약이다.
"""

import pytest

from app.services.terms_matcher import find_terms, load_registry

REGISTRY = [
    {"id": "db_travel", "insurer": "DB손해보험", "product": "프로미 해외여행보험Ⅰ",
     "aliases": ["프로미 해외여행보험1"], "revision": None},
    {"id": "hyundai_2022", "insurer": "현대해상", "product": "해외여행보험",
     "aliases": [], "revision": "2022-07-18"},
    {"id": "hyundai_2025", "insurer": "현대해상", "product": "다이렉트 해외여행보험",
     "aliases": [], "revision": "2025-06-30"},
]


def test_exact_match():
    match = find_terms("DB손해보험", "프로미 해외여행보험Ⅰ", registry=REGISTRY)

    assert match.terms_id == "db_travel"
    assert match.level == "EXACT"
    assert match.notice is None, "정확히 맞았는데 사용자에게 경고가 나간다"


# 증권은 회사명을 여러 방식으로 적는다. 전부 같은 회사로 봐야 한다.
@pytest.mark.parametrize("insurer", ["DB손해보험", "DB손해보험(주)", "DB손보", "DB 손해보험 주식회사"])
def test_insurer_name_variations(insurer):
    match = find_terms(insurer, "프로미 해외여행보험Ⅰ", registry=REGISTRY)

    assert match.terms_id == "db_travel"


# 로마숫자를 아라비아로 적는 증권이 있다.
def test_matches_via_alias():
    match = find_terms("DB손해보험", "프로미 해외여행보험1", registry=REGISTRY)

    assert match.terms_id == "db_travel"


# 같은 상품의 다른 개정판만 있으면 쓰되, 조항이 다를 수 있음을 알려야 한다.
# 조용히 다른 판으로 답하면 사용자가 틀린 조건을 믿게 된다.
def test_revision_mismatch_is_flagged():
    match = find_terms("현대해상", "해외여행보험", revision="2019-01-01", registry=REGISTRY)

    assert match.terms_id == "hyundai_2022"
    assert match.level == "REVISION"
    assert "개정판" in match.notice


def test_exact_revision_wins_over_latest():
    match = find_terms("현대해상", "해외여행보험", revision="2022-07-18", registry=REGISTRY)

    assert match.terms_id == "hyundai_2022"
    assert match.level == "EXACT"


# 증권에 개정일이 없는 경우가 흔하다. 그때마다 경고하면 경고가 무뎌진다.
def test_missing_revision_is_not_a_warning():
    match = find_terms("현대해상", "해외여행보험", revision=None, registry=REGISTRY)

    assert match.level == "EXACT"
    assert match.notice is None


# 보험사는 맞는데 상품이 없으면 표준 조항이라도 있는 편이 낫다.
def test_falls_back_to_same_insurer_other_product():
    match = find_terms("현대해상", "우리아이 여행보험", registry=REGISTRY)

    assert match.level == "INSURER"
    assert match.is_usable
    assert "다른 상품" in match.notice


# 그 보험사 약관이 아예 없으면 증권만으로 답한다. 예외를 내면 안 된다.
def test_unknown_insurer_returns_none_not_error():
    match = find_terms("캐롯손해보험", "스마트 해외여행보험", registry=REGISTRY)

    assert match.level == "NONE"
    assert match.is_usable is False
    assert "보유하고 있지 않아" in match.notice


@pytest.mark.parametrize("insurer, product", [("", ""), ("   ", "해외여행보험")])
def test_empty_input_does_not_crash(insurer, product):
    assert find_terms(insurer, product, registry=REGISTRY).level == "NONE"


# 정확히 일치하는 상품이 부분 일치하는 상품을 이겨야 한다.
# "해외여행보험"은 "다이렉트 해외여행보험"에도 포함되므로, 둘을 같은 점수로 두면
# 어느 약관이 뽑히는지가 레지스트리에 적은 순서로 정해진다. 상품 하나를 추가했을 뿐인데
# 기존 증권의 매칭이 조용히 바뀐다.
def test_exact_product_name_beats_partial_containment():
    reversed_registry = list(reversed(REGISTRY))

    for registry in (REGISTRY, reversed_registry):
        match = find_terms("현대해상", "해외여행보험", registry=registry)
        assert match.terms_id == "hyundai_2022", "레지스트리 순서에 따라 결과가 달라진다"


# 제휴 판매에서는 증권에 인수사가 따로 표기된다.
#
# 실제로 마이뱅크가 파는 상품의 증권에는 "한화손해보험(주)"가 적혀 있는데
# 약관은 캐롯 해외여행보험이었다. 회사명만 비교하면 NONE이 나와, 담보 13종이
# 전부 대응하는 약관을 두고도 못 쓴다.
def test_matches_via_underwriter_alias():
    registry = [
        {"id": "carrot", "insurer": "캐롯손해보험", "product": "캐롯 해외여행보험",
         "aliases": [], "revision": "2025-09-01",
         "underwriter_aliases": ["한화손해보험", "한화손해보험(주)"]},
    ]

    match = find_terms("한화손해보험(주)", "해외여행보험", registry=registry)

    assert match.terms_id == "carrot"
    assert match.is_usable


# 별칭을 넓게 잡아 아무 회사나 걸리면 다른 회사 약관으로 보상 조건을 답하게 된다.
def test_underwriter_alias_does_not_match_unrelated_insurer():
    registry = [
        {"id": "carrot", "insurer": "캐롯손해보험", "product": "캐롯 해외여행보험",
         "aliases": [], "revision": None, "underwriter_aliases": ["한화손해보험"]},
    ]

    assert find_terms("삼성화재", "해외여행보험", registry=registry).level == "NONE"


# 별칭이 없는 기존 항목이 깨지면 안 된다.
def test_entries_without_underwriter_alias_still_work():
    assert find_terms("DB손해보험", "프로미 해외여행보험Ⅰ", registry=REGISTRY).terms_id == "db_travel"


# 레지스트리의 id는 실제 청크 파일과 이어져야 한다.
# 여기가 어긋나면 매칭은 성공했는데 검색 결과가 0건이 되고, 원인을 찾기 어렵다.
def test_registry_ids_point_to_real_chunk_files():
    from pathlib import Path

    chunks_dir = Path(__file__).resolve().parents[1] / "data" / "chunks"
    missing = [
        entry["id"]
        for entry in load_registry()
        if not (chunks_dir / f'{entry["id"]}_chunks.json').exists()
    ]

    assert not missing, f"청크 파일이 없는 약관: {missing}"
