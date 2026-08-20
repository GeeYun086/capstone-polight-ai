"""증권 분석 결과 -> 우리 스키마 변환 회귀 테스트.

증권 전환의 이유가 금액과 가입 여부라, 이 변환이 틀리면 전환의 의미가 사라진다.
약관 기반에서는 limitAmount가 30건 중 3건만 채워졌는데 증권에서는 전부 채워진다.
파싱이 조용히 실패해 None이 되면 그 이점이 그대로 없어진다.

실제 증권(한화손보/마이뱅크)에서 확인한 형식을 고정한다.
"""

import pytest

from app.services.certificate_adapter import parse_amount, to_coverages, to_payloads

CERTIFICATE = {
    "insurer_name": "한화손해보험(주)",
    "coverage_by_age_table": [
        {
            "coverage_category_level_1": "해외의료비 보장",
            "coverage_category_level_2": "",
            "coverage_item_name": "상해",
            "coverage_amount_age_1_14": "US 5만달러",
            "coverage_amount_age_15_80": "US 5만달러",
        },
        {
            "coverage_category_level_1": "사망보장",
            "coverage_category_level_2": "",
            "coverage_item_name": "상해사망",
            "coverage_amount_age_1_14": "-",
            "coverage_amount_age_15_80": "3억원",
        },
        {
            "coverage_category_level_1": "기타보장",
            "coverage_category_level_2": "",
            "coverage_item_name": "식중독입원",
            "coverage_amount_age_1_14": "(정액) 50만원",
            "coverage_amount_age_15_80": "(정액) 50만원",
        },
        {
            "coverage_category_level_1": "국내실손의료비",
            "coverage_category_level_2": "통원 (급여/비급여)",
            "coverage_item_name": "상해",
            "coverage_amount_age_1_14": "10만원",
            "coverage_amount_age_15_80": "10만원",
        },
    ],
    "coverage_description_table": [
        {"benefit_name": "상해/질병 해외 의료비",
         "benefit_description": "여행 중 상해/질병으로 해외에서 의료비 발생시 실제 부담한 의료비 보상"},
        {"benefit_name": "상해 사망/후유장해",
         "benefit_description": "여행중 상해로 사망하거나 장해상태가 되었을 때 보상"},
        {"benefit_name": "상해/질병 국내 통원의료비",
         "benefit_description": "국내에서 통원하여 치료를 받은 경우 국내실손의료보험 기준에 따라 보상"},
    ],
}


# ── 금액 파싱 ────────────────────────────────────────────────
#
# limitAmount는 BIGINT라 정수만 들어간다. 증권은 단위를 글자로 적는다.
@pytest.mark.parametrize(
    "text, expected",
    [
        ("US 5만달러", (50_000, "USD")),
        ("5,000만원", (50_000_000, "KRW")),
        ("3억원", (300_000_000, "KRW")),
        ("1,000만원", (10_000_000, "KRW")),
        ("350만원", (3_500_000, "KRW")),
        ("6만원", (60_000, "KRW")),
        ("10만원", (100_000, "KRW")),
        # 정액 지급이라는 주석이 붙어도 금액은 읽어야 한다
        ("(정액) 50만원", (500_000, "KRW")),
    ],
)
def test_parses_amount_strings(text, expected):
    assert parse_amount(text) == expected


# "-"는 그 연령대에서 보장하지 않는다는 뜻이다. 0원이 아니다.
@pytest.mark.parametrize("text", ["-", "", "미보장", None])
def test_not_covered_marks_yield_no_amount(text):
    assert parse_amount(text) == (None, None)


def test_unreadable_amount_does_not_crash():
    amount, currency = parse_amount("보험가입금액 한도")

    assert amount is None
    assert currency == "KRW"


# ── 연령대 ──────────────────────────────────────────────────
#
# 금액이 연령대별 두 컬럼이라 나이에 따라 가입 여부가 달라진다.
# 어린이는 상해사망이 "-"인데, 이걸 놓치면 보상되지 않는 담보를 보상된다고 답한다.
def test_adult_and_child_read_different_columns():
    adult = {p.title: p for p in to_payloads(CERTIFICATE, age=30)}
    child = {p.title: p for p in to_payloads(CERTIFICATE, age=10)}

    assert adult["사망보장 상해사망"].coverage_status == "COVERED"
    assert adult["사망보장 상해사망"].limit_amount == 300_000_000

    assert child["사망보장 상해사망"].coverage_status == "NOT_COVERED"
    assert child["사망보장 상해사망"].limit_amount is None


def test_child_coverage_is_marked_not_subscribed():
    coverages = {c.name: c for c in to_coverages(CERTIFICATE, age=10)}

    assert coverages["사망보장 상해사망"].subscribed is False
    assert coverages["해외의료비 보장 상해"].subscribed is True


# ── 카드 변환 ───────────────────────────────────────────────


# 같은 이름이 분류를 달리해 여러 번 나온다. 그대로 두면 화면에 "상해" 카드가 겹친다.
def test_title_includes_category_to_disambiguate():
    titles = [p.title for p in to_payloads(CERTIFICATE)]

    assert "해외의료비 보장 상해" in titles
    assert "국내실손의료비 통원 (급여/비급여) 상해" in titles


# limitLabel은 화면에 그대로 나가는 문구다. 정수로는 "US 5만달러"의 통화나
# "(정액)"이라는 지급 방식을 표현할 수 없어 원문을 남긴다.
def test_limit_label_keeps_original_text():
    cards = {p.title: p for p in to_payloads(CERTIFICATE)}

    assert cards["해외의료비 보장 상해"].limit_label == "US 5만달러"
    assert cards["기타보장 식중독입원"].limit_label == "(정액) 50만원"


def test_not_covered_card_is_kept_with_label():
    cards = {p.title: p for p in to_payloads(CERTIFICATE, age=10)}

    assert cards["사망보장 상해사망"].limit_label == "보장하지 않음"


# ── 설명 연결 ───────────────────────────────────────────────
#
# 금액 표와 설명 표의 이름이 어순까지 다르다("해외의료비 보장 / 상해" vs
# "상해/질병 해외 의료비"). 문자열 포함으로 이으면 21건 중 9건만 붙었다.
def test_matches_description_despite_different_word_order():
    cards = {p.title: p for p in to_payloads(CERTIFICATE)}

    assert "실제 부담한 의료비" in cards["해외의료비 보장 상해"].conditions


# 분류를 함께 봐야 국내 통원과 해외 의료비가 구분된다.
def test_uses_category_to_pick_the_right_description():
    cards = {p.title: p for p in to_payloads(CERTIFICATE)}

    assert "통원" in cards["국내실손의료비 통원 (급여/비급여) 상해"].conditions


# 억지로 이으면 엉뚱한 설명이 붙는다. 실제로 "주사료"에 "국내 입원의료비" 설명이 붙었다.
def test_leaves_description_empty_when_no_good_match():
    certificate = {
        "coverage_by_age_table": [
            {"coverage_category_level_1": "기타", "coverage_category_level_2": "",
             "coverage_item_name": "골프용품손해", "coverage_amount_age_15_80": "100만원"}
        ],
        "coverage_description_table": [
            {"benefit_name": "상해/질병 해외 의료비", "benefit_description": "해외 의료비 보상"}
        ],
    }

    assert to_payloads(certificate)[0].conditions is None


def test_empty_certificate_yields_nothing():
    assert to_payloads({}) == []
    assert to_coverages({}) == []
