"""에이전트 출력이 흔들려도 죽지 않고, 조용히 틀리지 않는지에 대한 회귀 테스트.

에이전트 출력은 LLM이 만든 JSON이다. 프롬프트로 형식을 지시해도 보장되지 않고,
Studio에서 스키마를 손대면 코드 변경 없이 즉시 반영된다(UPSTAGE_AGENT_CONFIG_ID를
비워 최신 설정을 쓴다). 그래서 세 가지를 고정한다.

  타입 방어     배열 자리에 객체·null, 금액에 숫자가 와도 죽지 않는다
  조용한 오답   단일 금액으로 환산할 수 없는 표현을 금액으로 단정하지 않는다
  부분 실패     금액이 몇 건 채워졌는지 남긴다

세 번째가 가장 눈에 안 띈다. 표를 통째로 못 읽으면 담보 0건으로 실패해 알아챌 수
있지만, 절반만 읽히면 화면에 카드가 뜨고 아무도 빈 금액을 모른다.
"""

import logging

import pytest

from app.services.certificate_adapter import (
    coverage_names,
    parse_amount,
    to_coverages,
    to_payloads,
)


def one_row(**overrides):
    row = {
        "coverage_category_level_1": "해외의료비 보장",
        "coverage_item_name": "상해",
        "coverage_amount_age_15_80": "5,000만원",
    }
    row.update(overrides)
    return {"coverage_by_age_table": [row], "coverage_description_table": []}


# ── 타입 방어 ────────────────────────────────────────────────


# 프롬프트로 "문자열로 주라"고 지시해도 숫자가 올 수 있다. 그때 죽으면
# 담보를 하나도 못 건진다.
def test_numeric_amount_does_not_crash():
    payloads = to_payloads(one_row(coverage_amount_age_15_80=50_000_000))

    assert payloads[0].limit_amount == 50_000_000
    assert payloads[0].coverage_status == "COVERED"


def test_numeric_coverage_name_does_not_crash():
    payloads = to_payloads(one_row(coverage_item_name=12345))

    assert payloads[0].title.endswith("12345")


# 배열 자리에 객체 하나가 오는 일이 있다. 그대로 순회하면 키(문자열)를 행으로
# 취급해 AttributeError로 죽는다.
def test_table_as_single_object_is_treated_as_one_row():
    certificate = one_row()
    certificate["coverage_by_age_table"] = certificate["coverage_by_age_table"][0]

    payloads = to_payloads(certificate)

    assert len(payloads) == 1
    assert payloads[0].limit_amount == 50_000_000


def test_table_as_null_or_string_is_empty_not_a_crash():
    for broken in (None, "없음", 0):
        certificate = {"coverage_by_age_table": broken, "coverage_description_table": []}
        assert to_payloads(certificate) == []
        assert to_coverages(certificate) == []
        assert coverage_names(certificate) == []


def test_non_dict_rows_are_dropped():
    certificate = one_row()
    certificate["coverage_by_age_table"] = [
        certificate["coverage_by_age_table"][0],
        "상해 5,000만원",
        None,
    ]

    payloads = to_payloads(certificate)

    assert len(payloads) == 1


# ── 조용한 오답 ──────────────────────────────────────────────


# "1일당 10만원, 최대 30일"을 그대로 파싱하면 100000이 나온다. 실제 한도는
# 300만원이다. 자신 있게 틀리는 것이 비우는 것보다 나쁘다.
def test_per_day_limit_is_not_read_as_a_total():
    amount, currency = parse_amount("1일당 10만원, 최대 30일")

    assert amount is None
    assert currency == "KRW"


def test_ratio_limit_is_not_read_as_an_amount():
    assert parse_amount("실손 80% 보상")[0] is None
    assert parse_amount("가입금액의 10% 한도")[0] is None
    assert parse_amount("회당 30만원")[0] is None


# 환산이 안 되는 것과 미보장은 다르다. 여기서 NOT_COVERED를 찍으면 가입한
# 담보가 화면에서 "미보장"으로 뜨고, 챗봇이 "가입하지 않으셨습니다"라고 답한다.
def test_unconvertible_limit_is_still_covered():
    item = to_payloads(one_row(coverage_amount_age_15_80="1일당 10만원, 최대 30일"))[0]

    assert item.coverage_status == "COVERED"
    assert item.limit_amount is None
    # 화면에는 원문이 그대로 나간다
    assert item.limit_label == "1일당 10만원, 최대 30일"


def test_unconvertible_limit_is_still_subscribed_for_the_chatbot():
    coverage = to_coverages(one_row(coverage_amount_age_15_80="실손 80% 보상"))[0]

    assert coverage.subscribed is True
    assert coverage.limit_amount is None


# 미보장은 그대로 미보장이어야 한다. 위 완화가 여기까지 번지면 안 된다.
def test_not_covered_mark_stays_not_covered():
    item = to_payloads(one_row(coverage_amount_age_15_80="-"))[0]

    assert item.coverage_status == "NOT_COVERED"
    assert item.limit_label == "보장하지 않음"


# 정상 금액은 그대로 읽혀야 한다. 방어 코드가 기존 동작을 건드리면 안 된다.
def test_normal_amounts_are_unaffected():
    assert parse_amount("5,000만원") == (50_000_000, "KRW")
    assert parse_amount("3억원") == (300_000_000, "KRW")
    assert parse_amount("US 5만달러") == (50_000, "USD")
    assert parse_amount("(정액) 50만원") == (500_000, "KRW")


# 통화 표기를 걷어내는 순서 때문에 금액을 놓치던 버그.
#
# "US|USD" 순서면 "USD"에서 "US"만 지워져 "D"가 남고, 숫자 변환이 실패해 금액이
# 통째로 사라진다. 통화는 USD로 잡히는데 한도가 비어 화면에 "미상"으로 뜬다.
# 에이전트가 인쇄 문자열을 그대로 보존하면 이 형태가 실제로 들어온다.
@pytest.mark.parametrize(
    "text",
    ["50,000 USD", "USD 50,000", "US$ 50,000", "US 5만달러", "5만 USD"],
)
def test_dollar_notations_all_yield_the_amount(text):
    assert parse_amount(text) == (50_000, "USD")


# 단위가 없는 숫자는 원화로 본다. 지금 에이전트 출력이 이 형태다("100,000,000").
def test_bare_number_is_read_as_krw():
    assert parse_amount("100,000,000") == (100_000_000, "KRW")


# 한도가 없다는 표현은 금액을 비우되 미보장으로 만들지 않는다.
@pytest.mark.parametrize("text", ["무제한", "한도 없음", "보험가입금액 한도"])
def test_unlimited_is_covered_without_amount(text):
    amount, currency = parse_amount(text)

    assert amount is None
    assert currency == "KRW"
    assert to_payloads(one_row(coverage_amount_age_15_80=text))[0].coverage_status == "COVERED"


# ── 부분 실패 ────────────────────────────────────────────────


def test_low_amount_fill_is_warned(caplog):
    certificate = {
        "coverage_by_age_table": [
            {"coverage_item_name": "상해", "coverage_amount_age_15_80": "5,000만원"},
            {"coverage_item_name": "질병", "coverage_amount_age_15_80": "실손 80%"},
            {"coverage_item_name": "휴대품", "coverage_amount_age_15_80": "회당 20만원"},
        ],
        "coverage_description_table": [],
    }

    with caplog.at_level(logging.WARNING):
        to_payloads(certificate)

    assert "금액이 1건" in caplog.text


def test_full_amount_fill_is_not_warned(caplog):
    with caplog.at_level(logging.WARNING):
        to_payloads(one_row())

    assert not caplog.text


# 에이전트가 통화를 ₩ 기호로 붙이는 경우. "원"만 걷어내면 이 금액을 통째로
# 놓친다. 실측에서 현대 증권 26건 중 11건이 ₩ 표기였다.
@pytest.mark.parametrize(
    "text,expected",
    [("₩200,000", 200_000), ("₩100,000,000", 100_000_000), ("₩1,400,000", 1_400_000)],
)
def test_won_symbol_is_read(text, expected):
    assert parse_amount(text) == (expected, "KRW")


# 나이 0은 나이가 아니라 "못 뽑았다"는 뜻이다. 그대로 쓰면 1~14세 컬럼을 읽어
# 성인 담보를 미보장으로 만든다. 실측에서 한화 증권이 age=0으로 왔다.
def test_age_zero_is_treated_as_adult():
    from app.services.certificate_adapter import subscriber_age

    certificate = {
        "insured_person_age": 0,
        "coverage_by_age_table": [
            {
                "coverage_item_name": "상해사망",
                "coverage_amount_age_1_14": "-",
                "coverage_amount_age_15_80": "1억원",
            }
        ],
    }

    assert subscriber_age(certificate) == 30
    item = to_payloads(certificate)[0]
    assert item.coverage_status == "COVERED"
    assert item.limit_amount == 100_000_000
